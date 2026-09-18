"""笔记工具族（builtin 拆分，S4a 清账）：list/read/write_note + search_notes/search_and_summarize。

知识库（data/notes/）是语义资产——与 S4 files.py（项目工作区代码）分工：
这边管"学到的东西"，那边管"项目本身"。
"""

from __future__ import annotations

from agent.core.types import Message
from agent.tools.context import ToolContext
from agent.tools.registry import Tool, ToolRegistry

_EMPTY_PARAMS = {"type": "object", "properties": {}, "required": []}

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


def register_note_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """笔记五件注册。闭包抓 ctx（kb/llm/notes_dir）——List identity trap 同款纪律。"""

    def list_notes() -> str:
        """列出知识库目录下的所有笔记文件名。"""
        files = sorted(ctx.notes_dir.glob("*.md"))
        if not files:
            return "知识库里还没有任何笔记。"
        return "知识库笔记清单：\n" + "\n".join(f"- {f.name}" for f in files)

    def read_notes(filename: str) -> str:
        """读取知识库目录下的指定笔记文件。"""
        try:
            with open(ctx.notes_dir / filename, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return f"知识库里没有 {filename} 这个笔记。"

    def write_note(filename: str, content: str) -> str:
        """把一篇笔记写入知识库目录，带安全检查 + 查重闸门。"""
        # 安全清单：agent 第一次能改文件系统，每一道都不能省
        path = (ctx.notes_dir / filename).resolve()
        if not path.is_relative_to(ctx.notes_dir.resolve()):
            return "拒绝：文件名越界"    # resolve 会消掉 ../，所以必须先 resolve 再判断
        if not filename.endswith(".md"):
            return "拒绝：只允许写入 .md 文件"
        if "/" in filename:
            return "拒绝：暂不支持子目录"
        # 查重闸门：内容与已有笔记高度相似则拒绝（治理第 1 层）。
        # S4 评审 #R5 起 search 自带 source——重复时能报出「撞了哪篇」。
        if ctx.kb is not None:
            hits = ctx.kb.search(content, top_k=3, min_score=0.85)
            if hits:
                return (
                    f"拒绝：与已有笔记《{hits[0].source}》高度重复"
                    "（相似内容开头：「" + hits[0].chunk[:30] + "…」），"
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
        assert ctx.kb is not None   # 注册守卫（见下方 if ctx.kb is not None）保证非 None
        results = ctx.kb.search(query, top_k=3)   # 不传 min_score → 用 embedder 自带阈值
        if not results:
            return "知识库中没有检索到相关内容。"
        # 命中 → 给模型看的编号文本。
        # 分数+来源都展示给模型：分数让它判断检索质量（偏低=可换关键词再查），
        # 来源让它引用资料时能说清出处（RAG 溯源，S4 评审 #R5）。
        return "\n".join(
            f"{i+1}. {hit.chunk}（出处：{hit.source}，相关度 {hit.score:.2f}）"
            for i, hit in enumerate(results)
        )

    def search_and_summarize(query: str) -> str:
        """检索 + 二次摘要：工具内部再调一次 LLM（Sub-agent 模式的原型）。"""
        assert ctx.kb is not None and ctx.llm is not None   # 双依赖注册守卫保证
        results = ctx.kb.search(query, top_k=3)
        if not results:
            return "知识库中没有检索到相关内容，无法生成摘要。"
        context = "\n".join(
            f"- {hit.chunk}（出处：{hit.source}）" for hit in results
        )

        # 关键安全设计：内部这次调用【绝不传 tools】——
        # ① 传了就可能出现"工具调工具"的无限递归（套娃）；
        # ② 这只是一次性摘要任务，不需要它有任何行动能力
        reply = ctx.llm.generate(
            [Message(role="user", content=(
                f"请用最多三句话总结以下资料，只提炼关键结论，不要复述原文：\n{context}"
            ))],
        )
        return f"摘要：{reply.content}\n\n原始片段：\n{context}"

    registry.register(Tool(
        name="list_notes",
        description="列出个人知识库（data/notes 目录）里现有的全部笔记文件名清单。",
        parameters=_EMPTY_PARAMS,
        func=list_notes,
        is_readonly=True,
    ))
    registry.register(Tool(
        name="read_notes",
        description="读取个人知识库（data/notes 目录）里的指定笔记文件的完整内容。"
        "文件名可先用 list_notes 查询。当用户想看某篇笔记的详细内容时使用。",
        parameters=_READ_NOTE_PARAMS,
        func=read_notes,
        is_readonly=True,
    ))
    registry.register(Tool(
        name="write_note",
        description="新建一篇笔记写入个人知识库（data/notes 目录）。"
        "当用户想保存、记录某个知识点或结论到知识库时使用。"
        "注意：①不能覆盖已有文件；②若新内容与库中已有笔记高度重复会被拒绝，"
        "此时应先读原文、把新信息合并进去，而不是另建新文件。",
        parameters=_WRITE_NOTE_PARAMS,
        func=write_note,
    ))
    if ctx.kb is not None:   # 条件注册：kb 是检索工具的核心依赖
        registry.register(Tool(
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
            is_readonly=True,
        ))
        if ctx.llm is not None:   # 复合工具需要双依赖：检索(kb)+摘要(llm)，缺一不上菜单
            registry.register(Tool(
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
                is_readonly=True,
            ))
