"""历史检索工具（builtin 拆分，S4a 清账）：search_history / read_history。

编号体系跨工具一致是硬契约（同一数据的多个视图必须共享坐标系）。
"""

from __future__ import annotations

from facta.tools.context import ToolContext
from facta.tools.registry import Tool, ToolRegistry

_SEARCH_HISTORY_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索关键词：必须是消息正文里会出现的词（名字、数字、话题词，如'小温''幸运数字''PHP'），不能是'第一句''开头说了什么'这类抽象概念（谁嘴里也不会说这些词，搜了必落空）。问'一开始说了什么'时，用摘要里的特征词搜索，再取位置编号最小的命中。"}
    },
    "required": ["query"],
}
_READ_HISTORY_PARAMS = {
    "type": "object",
    "properties": {
        "start": {"type": "integer", "description": "起始位置编号（从 0 开始：#0 是 system 人设，#1 通常是用户的第一句话）。从该条开始（含）向后读。"},
        "count": {"type": "integer", "description": "读取条数，默认 5，最多 20（防止一次灌爆上下文）。"},
    },
    "required": ["start"],
}


def _search_history(history, query: str) -> str:
    """在当前会话的完整底片（含已被摘要压缩的旧消息）中检索对话原话。

    关键词子串匹配，不是语义检索：历史每轮都在长，语义检索要每轮
    重新 embedding（贵且慢）；提炼关键词的智能活交给调用方模型。
    只搜 user/assistant——system 是人设（非历史），tool 结果是
    检索产物（可重新生成），都不是"对话原话"的靶子。
    """
    q = query.strip().lower()
    if not q:
        return "检索词为空，请提供关键词。"
    hits = [
        (i, m) for i, m in enumerate(history)
        if m.role in ("user", "assistant") and m.content and q in m.content.lower()
    ]   # m.content 为 None 的工具轮点菜消息被 and m.content 自然滤掉
    if not hits:
        return "历史中没有检索到包含该关键词的原话。"
    # 给分母：模型只看到 #30 不知道早晚，加上"共 N 条"才能做位置核验
    # （问"第一句"却命中 #30/34 → 自曝关键词搜偏了，应换词重搜）
    lines = [f"命中 {len(hits)} 条（历史共 {len(history)} 条；#编号越小消息越早）："]
    for i, m in hits[:10]:
        lines.append(f"#{i} [{m.role}] {m.content}")
    if len(hits) > 10:
        lines.append("（命中较多，仅显示前 10 条，可换更具体的关键词缩小范围）")
    return "\n".join(lines)


def _read_history(history, start: int, count: int = 5) -> str:
    """按位置编号读取当前会话的对话原文（与 search_history 互补）。

    search_history 回答"谁说过 X"（内容→位置，LIKE）；本工具回答
    "第 N 条是什么"（位置→内容，ORDER BY id LIMIT）——问"第一句"
    直接读 start=1，不必猜关键词。
    """
    if start < 0:
        return "起始编号从 0 开始（#0 是 system 人设，#1 通常是用户的第一句话）。"
    if not 1 <= count <= 20:
        return "count 需在 1~20 之间（默认 5）。"
    if start >= len(history):
        return f"起始编号超出范围：历史共 {len(history)} 条（编号 0~{len(history)-1}）。"

    lines = [f"历史共 {len(history)} 条（编号与 search_history 一致，越小越早），读取 #{start} 起的 {count} 条："]
    for i in range(start, min(start + count, len(history))):
        m = history[i]
        tag = ""
        if m.tool_calls:   # 工具轮点菜消息：content 常为 None，标注它点了什么菜
            tag = f"（点菜：{'、'.join(t['name'] for t in m.tool_calls)}）"
        content = m.content or "（无文字内容）"
        # 按角色分配上下文预算：user/assistant 是逐字原话（本工具的使命），全量给；
        # tool 结果是可重生的检索产物，截断 300 字防止一次读 20 条灌爆上下文
        if m.role == "tool" and len(content) > 300:
            content = content[:300] + "…（已截断）"
        lines.append(f"#{i} [{m.role}]{tag} {content}")
    return "\n".join(lines)


def register_history_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """条件注册：history 缺席不上菜单（闭包抓列表对象本身——List identity trap）。"""
    if ctx.history is None:
        return
    registry.register(Tool(
        name="search_history",
        description=(
            "在当前会话的完整对话历史中按关键词检索【逐字原话】。"
            "历史过长时旧消息会被压缩为摘要，摘要只保留要点、会丢失原始措辞——"
            "当用户询问早前对话的确切原话、具体数字、'当时怎么说的'时使用。"
            "匹配规则：关键词子串精确匹配——搜的必须是消息正文里实际出现的词"
            "（名字、数字、话题词），不能是'第一句''开头'这类抽象概念，搜概念必落空。"
            "检索策略：从摘要和用户问题中提取特征词做关键词（是提取词，"
            "不是照抄问题原句）；一次未命中就换词重试，不要轻易放弃。"
            "命中结果带位置编号和历史总数，编号越小消息越早——"
            "问'第一句''一开始说了什么'时，取编号最小的命中；"
            "拿到结果先核验位置与问题是否自洽：若问'第一句'却命中的编号偏大"
            "（如 #30/共34），说明关键词搜偏了，应换摘要里更早内容的特征词重搜。"
            "确实检索不到时应如实告知——既不要凭印象编造原话，"
            "也不要把'这轮对话'偷换概念成'这一回合'来回避问题。"
        ),
        parameters=_SEARCH_HISTORY_PARAMS,
        func=lambda query: _search_history(ctx.history, query),
        is_readonly=True,
    ))
    registry.register(Tool(
        name="read_history",
        description=(
            "按位置编号读取当前会话的对话原文，与 search_history 互补："
            "那个按【内容】搜（'谁说过 X'），这个按【位置】读（'第 N 条是什么'）。"
            "当用户问'第一句话说了什么''最早的对话''开头聊了什么'这类位置问题时"
            "直接使用——读 start=1 即用户最早的原话，不需要猜关键词、"
            "也不需要先经过 search_history。"
            "编号从 0 开始且与 search_history 的编号完全一致"
            "（#0 是 system 人设，#1 通常是用户的第一句话，编号越小越早），"
            "历史总数见返回头。想看某条命中的上下文时也可用它读前后几条。"
        ),
        parameters=_READ_HISTORY_PARAMS,
        func=lambda start, count=5: _read_history(ctx.history, int(start), int(count)),
        is_readonly=True,
    ))
