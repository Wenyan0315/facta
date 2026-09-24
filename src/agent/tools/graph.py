"""S7a 图谱工具族：query_graph（查询）+ sync_graph（抽取更新）。

query_graph 三个原语（一等公民款式）：
- neighbors：概念的直接关系（1 跳，可按关系类型过滤）
- path：两概念间的最短链路（BFS ≤3 跳，无向）——「A 和 B 什么关系」
- overview：全图统计（概念数/关系分布/孤岛/枢纽）——「我学过什么全貌」

sync_graph（界面可操作拍板，2026-09-23）：图谱更新不能只是启动时的
隐式行为——用户在对话里新建/修改笔记后，说「把它加进图谱」即可触发
增量抽取（指纹差集自动只抽改动篇）。force=True 清空指纹全量重抽
（换更强模型后想全部重抽的真实场景）。不设确认缝：幂等重建、不碰
用户数据、不可逆三判据全不沾（内部花钱与 search_and_summarize 同
判据，审计可见）。落盘与回报在这层（管线只管抽取，落盘归工具——
与启动路径 assemble 分工不同但同一份管线）。

实现注记：schema 参数名用 from/to（对模型自然），但 from 是 Python
关键字——func 收 **kwargs 再取键（registry.execute 的 func(**args)
调用约定天然支持）。
"""

from __future__ import annotations

from agent.knowledge.extract import sync_graph as _run_sync
from agent.knowledge.graph import GRAPH_LOCK, RELATIONS, GraphStore
from agent.paths import GRAPH_PATH
from agent.tools.context import ToolContext
from agent.tools.registry import Tool, ToolRegistry


def _render_edge(edge) -> str:
    return f"{edge.source} --{edge.relation}--> {edge.target}（出处：{edge.source_note}）"


def _entity_hint(store: GraphStore) -> str:
    """给「实体不存在」错误附已知实体示例——错误文案即指路提示。"""
    return ", ".join(list(store.nodes)[:20])


def register_graph_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """注册 query_graph。ctx.graph 缺席 = 不上菜单（条件注册惯例）。"""
    if ctx.graph is None:
        return
    store = ctx.graph

    def _neighbors(args: dict) -> str:
        name = str(args.get("entity", "")).strip()
        relation = str(args.get("relation", "")).strip()
        if not name:
            return "错误：neighbors 需要 entity 参数——查哪个概念的关系？"
        if relation and relation not in RELATIONS:
            return f"错误：未知关系「{relation}」，可过滤：{'/'.join(RELATIONS)}（不传则查全部）"
        nid = store.resolve(name)
        if nid is None:
            return f"图中没有「{name}」这个实体。可用实体示例：{_entity_hint(store)}"
        edges = store.neighbors(nid, relation=relation or None)
        if not edges:
            rel_hint = f"（已过滤关系={relation}）" if relation else ""
            return f"「{nid}」在图中是孤岛节点，没有任何关系边{rel_hint}。"
        return f"「{nid}」的直接关系（{len(edges)} 条）：\n" + "\n".join(
            _render_edge(e) for e in edges
        )

    def _path(args: dict) -> str:
        a = str(args.get("from", "")).strip()
        b = str(args.get("to", "")).strip()
        if not a or not b:
            return "错误：path 需要 from 和 to 两个参数——查哪两个概念之间的关系链路？"
        missing = [x for x in (a, b) if store.resolve(x) is None]
        if missing:
            return f"图中没有这些实体：{'、'.join(missing)}。可用实体示例：{_entity_hint(store)}"
        trail = store.path(a, b)
        if trail is None:
            return (
                f"「{store.resolve(a)}」和「{store.resolve(b)}」在 3 跳内没有连通的"
                "关系链路（可能确实无关联，或链路超出跳数上限）。"
            )
        if not trail:
            return f"「{a}」和「{b}」是同一个实体。"
        cur = store.resolve(a)
        parts = []
        for e in trail:
            nxt = e.target if e.source == cur else e.source
            parts.append(f"{cur} --{e.relation}--> {nxt}")
            cur = nxt
        return (
            f"「{store.resolve(a)}」到「{store.resolve(b)}」的最短关系链"
            f"（{len(trail)} 跳）：\n" + " 且 ".join(parts)
        )

    def _overview(args: dict) -> str:
        stats = store.overview()
        lines = [
            f"概念实体：{stats['nodes']} 个",
            f"关系边：{stats['edges']} 条（"
            + "、".join(f"{r} {c}" for r, c in stats["relations"].items() if c)
            + "）",
        ]
        if stats["islands"]:
            lines.append(f"孤岛概念（无任何关系）：{'、'.join(stats['islands'])}")
        if stats["hubs"]:
            lines.append(
                "枢纽概念（连接最多）："
                + "、".join(f"{h['name']}（{h['degree']} 条）" for h in stats["hubs"])
            )
        return "知识图谱全貌：\n" + "\n".join(lines)

    def _query_graph(**args) -> str:
        if not store.nodes:
            return (
                "知识图谱当前为空（可能处于教学模式未抽取，或笔记刚清空）。"
                "无法回答关系类问题；语义检索仍可用 search_notes。"
            )
        action = str(args.get("action", "")).strip()
        if action == "neighbors":
            return _neighbors(args)
        if action == "path":
            return _path(args)
        if action == "overview":
            return _overview(args)
        return (
            f"错误：未知 action「{action}」。合法值：neighbors（查某概念的直接关系）/"
            "path（查两概念间链路）/overview（全图统计）"
        )

    registry.register(Tool(
        name="query_graph",
        description=(
            "查询知识图谱——回答概念间的【精确关系】（向量检索管模糊相似，"
            "本工具管结构化关系）。三种查询：neighbors=某概念的直接关系"
            "（可选 relation 过滤：依赖/包含/对比/引用）；path=两个概念之间"
            "的关系链路（多跳）；overview=全部已学概念的全貌统计。"
            "问「A 和 B 什么关系」「A 依赖哪些概念」「我学过哪些东西」时用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["neighbors", "path", "overview"],
                    "description": "查询类型",
                },
                "entity": {"type": "string", "description": "neighbors 的目标概念名（支持别名）"},
                "relation": {
                    "type": "string",
                    "enum": list(RELATIONS),
                    "description": "neighbors 的关系类型过滤（可选，不传查全部）",
                },
                "from": {"type": "string", "description": "path 的起点概念名"},
                "to": {"type": "string", "description": "path 的终点概念名"},
            },
            "required": ["action"],
        },
        func=_query_graph,
        is_readonly=True,   # L0 只读：纯查询无副作用
    ))

    # ---- sync_graph：界面可操作的图谱更新（用户拍板 2026-09-23）----
    # ctx.llm 缺席 = 不上菜单（教学组合无抽取通道，spawn 同款条件注册）
    if ctx.llm is None:
        return
    extract_llm = ctx.llm   # 局部窄化：闭包捕获局部变量（mypy 闭包窄化惯例）

    def _sync(force: bool = False) -> str:
        with GRAPH_LOCK:   # 图级串行：与面板「重建图谱」端点互斥（S7b 并发协议）
            if force:
                # 全量重抽：清空指纹（不动图——merge_note 的原子替换保证旧边
                # 在各篇重抽时逐篇失效；中途失败也不会留下半张空图）
                store.note_hashes.clear()
            report = _run_sync(store, ctx.notes_dir, extract_llm)
            if report.extracted or report.removed:
                store.save(GRAPH_PATH)
        if report.extracted == 0 and report.skipped == 0 and report.failed == 0:
            return (
                f"图谱已是最新（{report.unchanged} 篇笔记未变，零抽取）。"
                "笔记改动后再调本工具，或 force=true 全量重抽。"
            )
        parts = [f"抽取 {report.extracted} 篇 / 不变 {report.unchanged} 篇"]
        if report.removed:
            parts.append(f"删除 {report.removed} 篇")
        if report.failed:
            parts.append(f"失败 {report.failed} 篇（已跳过，不影响其他篇）")
        stats = store.overview()
        tail = f"。当前图谱：{stats['nodes']} 个概念 / {stats['edges']} 条关系边"
        return "图谱同步完成：" + "，".join(parts) + tail

    registry.register(Tool(
        name="sync_graph",
        description=(
            "更新知识图谱：把笔记目录的改动增量抽取进图谱（指纹差集——"
            "只抽新建/修改过的笔记，没变的零成本）。用户新建或修改笔记后"
            "说「更新图谱」「把它加进图谱」时用本工具。force=true 清空"
            "指纹全量重抽（换了更强的模型想重建全图时用）。抽取由内部"
            "模型完成，结果落盘 data/graph.json。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "是否全量重抽（默认 false 增量）",
                },
            },
            "required": [],
        },
        func=_sync,
        is_readonly=False,   # L1：改图谱（幂等重建，不碰用户笔记原文）
    ))
