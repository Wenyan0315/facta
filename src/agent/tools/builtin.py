"""内置工具集：第一批"手"。

选这两个的理由：
  get_current_time —— 教科书级演示：模型绝对不可能知道、必须靠工具的典型
  list_notes       —— 串联已有资产：让 agent 能"看到"你的 data/notes 知识库
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agent.core.llm import Message
from agent.tools.registry import Tool, ToolRegistry

# 空参数工具的 JSON Schema：类型是 object、没有属性
_EMPTY_PARAMS = {"type": "object", "properties": {}, "required": []}

# 带参数工具的 schema：每个参数声明类型 + 说明，required 列出必填项
_READ_NOTE_PARAMS = {
    "type": "object",
    "properties": {
        "filename": {"type": "string", "description": "笔记文件名，例如 RAG.md"}
    },
    "required": ["filename"],
}
_WRITE_NOTE_PARAMS = {
    "type": "object",
    "properties": {
        "filename": {"type": "string", "description": "新笔记的文件名，须以 .md 结尾，且不能与已有文件重名，例如 政策复查自动化.md"},
        "content": {"type": "string", "description": "笔记正文内容，Markdown 格式"},
    },
    "required": ["filename", "content"],
}
_SEARCH_NOTES_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索词。应根据用户问题提炼，而非照抄原话；用户说'继续'等指代性话语时，需结合上文改写成完整的查询。"}
    },
    "required": ["query"],
}
_SEARCH_AND_SUMMARIZE_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索词，提炼自用户问题。当用户明确要求'总结''概括''只说重点'时优先用本工具而非 search_notes。"}
    },
    "required": ["query"],
}
_SEARCH_HISTORY_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索关键词：必须是消息正文里会出现的词（名字、数字、话题词，如'小温''幸运数字''PHP'），不能是'第一句''开头说了什么'这类抽象概念（谁嘴里都不会说这些词，搜了必落空）。问'一开始说了什么'时，用摘要里的特征词搜索，再取位置编号最小的命中。"}
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

NOTES_DIR = Path("data/notes")

def get_current_time() -> str:
    """返回当前日期、时间和星期。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")


def list_notes() -> str:
    """列出知识库 data/notes/ 下的所有笔记文件名。"""
    files = sorted(NOTES_DIR.glob("*.md"))
    if not files:
        return "知识库里还没有任何笔记。"
    return "知识库笔记清单：\n" + "\n".join(f"- {f.name}" for f in files)

def read_notes(filename: str) -> str:
    """读取知识库 data/notes/ 下的指定笔记文件。"""
    try:
        with open(f"{NOTES_DIR}/{filename}", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"知识库里没有 {filename} 这个笔记。"

def register_builtin(registry: ToolRegistry, kb=None, llm=None, history: list[Message] | None = None) -> None:
    """把内置工具登记进注册表。description 认真写——模型靠它决定何时用工具。

    kb / llm / history 通过闭包注入给需要它们的工具（工具函数签名必须与
    schema 一致，不能加参数，所以让它们生在 register_builtin 里，直接引用
    外层变量）。history 特别注意：传的是列表对象本身（不是副本）——
    run_chat 在这个列表上原地 append，工具才能实时看到全部历史。
    """
    def write_note(filename: str, content: str) -> str:
        """把一篇笔记写入知识库 data/notes/，带安全检查 + 查重闸门。"""
        # 安全清单：agent 第一次能改文件系统，每一道都不能省
        path = (NOTES_DIR / filename).resolve()
        if not path.is_relative_to(NOTES_DIR.resolve()):
            return "拒绝：文件名越界"    # resolve 会消掉 ../，所以必须先 resolve 再判断
        if not filename.endswith(".md"):
            return "拒绝：只允许写入 .md 文件"
        if "/" in filename:
            return "拒绝：暂不支持子目录"
        # 查重闸门：内容与已有笔记高度相似则拒绝（治理第 1 层）。
        # KB 只存文本块不记"块来自哪个文件"（溯源缺口，M8 图谱补），
        # 所以用最相似块的开头片段代替文件名。
        if kb is not None:
            hits = kb.search(content, top_k=3, min_score=0.85)
            if hits:
                top_snippet = hits[0][0][:30]
                return (
                    f"拒绝：与已有笔记高度重复（相似内容开头：「{top_snippet}…」），"
                    "建议先读原文合并，而不是新建重复笔记"
                )
        if path.exists():
            return f"已存在同名笔记 {filename}，如需修改请先读取原文，或换一个文件名"
        try:
            path.write_text(content, encoding="utf-8")
            return f"已写入 {filename}（{len(content)} 字）"
        except OSError as e:
            return f"写入失败：{e}"

    def search_notes(query: str) -> str:
        """在个人知识库中语义检索，返回最相关的笔记片段。"""
        results = kb.search(query, top_k=3)   # 不传 min_score → 用 embedder 自带阈值
        if not results:
            return "知识库中没有检索到相关内容。"
        # (片段, 分数) 列表 → 给模型看的编号文本。
        # 分数展示给模型：让它能自行判断检索质量——分数偏低意味着"资料可能不够相关"，
        # 它可以换关键词再查一次，或者放弃检索直接回答。
        return "\n".join(
            f"{i+1}. {snippet}（相关度 {score:.2f}）"
            for i, (snippet, score) in enumerate(results)
        )

    def search_and_summarize(query: str) -> str:
        """检索 + 二次摘要：工具内部再调一次 LLM（Sub-agent 模式的原型）。"""
        # 检索部分：和 search_notes 同源（这就是"复合工具=小管线"）
        results = kb.search(query, top_k=3)
        if not results:
            return "知识库中没有检索到相关内容，无法生成摘要。"
        context = "\n".join(f"- {snippet}" for snippet, _ in results)

        # 关键安全设计：内部这次调用【绝不传 tools】——
        # ① 传了就可能出现"工具调工具"的无限递归（套娃）；
        # ② 这只是一次性摘要任务，不需要它有任何行动能力；
        # ③ 外层 agent_loop 的循环不需要担心递归，因为它的"点菜"会被
        #    我们执行并回填，而这里我们是"直接要一段文字"。
        # 思考题①的答案：description 里明确说了"会做摘要、返回总结"——
        # 外层模型知道拿到的不是原始片段，就不会重复自己总结一遍。
        reply = llm.generate(
            [Message(role="user", content=(
                f"请用最多三句话总结以下资料，只提炼关键结论，不要复述原文：\n{context}"
            ))],
        )   # ← 不传第二个参数 tools，默认 None
        return f"摘要：{reply.content}\n\n原始片段：\n{context}"

    def search_history(query: str) -> str:
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

    def read_history(start: int, count: int = 5) -> str:
        """按位置编号读取当前会话的对话原文（与 search_history 互补）。

        search_history 回答"谁说过 X"（内容→位置，LIKE）；本工具回答
        "第 N 条是什么"（位置→内容，ORDER BY id LIMIT）——问"第一句"
        直接读 start=1，不必猜关键词，五轮验收翻车的三跳推理就此拆除。
        编号 0-based，与 search_history 完全一致（#0=system 人设），
        两工具编号体系若不一致，模型交叉对照必然错位。
        """
        # 入参校验：错参返回"教模型怎么改"的提示，而不是崩
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



    registry.register(
        Tool(
            name="get_current_time",
            description="获取当前的日期、时间和星期。当用户询问现在几点、今天几号、今天星期几时使用。",
            parameters=_EMPTY_PARAMS,
            func=get_current_time,
        )
    )
    registry.register(
        Tool(
            name="list_notes",
            description="列出个人知识库（data/notes 目录）里现有的全部笔记文件名清单。",
            parameters=_EMPTY_PARAMS,
            func=list_notes,
        )
    )
    registry.register(
        Tool(
            name="read_notes",
            description="读取个人知识库（data/notes 目录）里的指定笔记文件的完整内容。"
            "文件名可先用 list_notes 查询。当用户想看某篇笔记的详细内容时使用。",
            parameters=_READ_NOTE_PARAMS,
            func=read_notes,
        )
    )
    registry.register(
        Tool(
            name="write_note",
            description="新建一篇笔记写入个人知识库（data/notes 目录）。"
            "当用户想保存、记录某个知识点或结论到知识库时使用。"
            "注意：①不能覆盖已有文件；②若新内容与库中已有笔记高度重复会被拒绝，"
            "此时应先读原文、把新信息合并进去，而不是另建新文件。",
            parameters=_WRITE_NOTE_PARAMS,
            func=write_note,
        )
    )
    if kb is not None:   # 条件注册：kb 是核心依赖（无 kb 此工具无意义），菜单不放这道菜
        registry.register(
            Tool(
                name="search_notes",
                description=(
                    "在个人知识库（学习笔记、踩坑记录、项目决策）中语义检索，"
                    "返回最相关的笔记片段及其相关度分数。"
                    "当用户的问题可能涉及知识库内容时使用——例如询问学过的概念、"
                    "踩过的坑、做过的决策。"
                    "闲聊、寒暄、与个人知识无关的常识问题不必检索。"
                    "查询词应自行提炼：用户说'继续''那再讲讲'等指代性话语时，"
                    "要结合对话上文改写成完整、明确的查询词再检索。"
                    "若返回的相关度分数普遍偏低，可换更精准的关键词重试一次。"
                ),
                parameters=_SEARCH_NOTES_PARAMS,
                func=search_notes,
            )
        )
        if llm is not None:   # 复合工具需要双依赖：检索(kb)+摘要(llm)，缺一不上菜单
            registry.register(
                Tool(
                    name="search_and_summarize",
                    description=(
                        "在个人知识库中检索并返回【已总结好的摘要】（附带原始片段）。"
                        "本工具内部已完成提炼总结，你无需再对结果做二次总结——"
                        "直接基于摘要回答用户即可，需要展开细节时再参考原始片段。"
                        "当用户明确要求'总结''概括''只讲重点''简单说说'时，"
                        "优先用本工具而不是 search_notes；"
                        "当用户需要笔记原文细节时，仍应使用 search_notes 或 read_notes。"
                    ),
                    parameters=_SEARCH_AND_SUMMARIZE_PARAMS,
                    func=search_and_summarize,
                )
            )
    if history is not None:   # 条件注册：会话记忆是核心依赖（无 history 此工具无意义）
        registry.register(
            Tool(
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
                func=search_history,
            )
        )
        registry.register(
            Tool(
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
                func=read_history,
            )
        )