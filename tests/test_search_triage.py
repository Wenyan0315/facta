"""043 检索分诊验收：search_notes 图谱导航追加（跨笔记兜底，L1 零成本）。

不变量：
- 孤岛锚点（图无边）→ 导航空集，返回不含「图谱导航」段（L1 零成本）
- 链/枢纽锚点 → 追加「图谱导航」段 + 邻笔记块（L2 结构性增量）
- graph 未装配（ctx.graph=None）→ 静默跳过（教学组合照旧）
"""

import json

from agent.knowledge.graph import GraphStore
from agent.knowledge.knowledge_base import BagOfWordsEmbedder, KnowledgeBase
from agent.knowledge.sync import sync_notes
from agent.tools.context import ToolContext
from agent.tools.notes import register_note_tools
from agent.tools.registry import ToolRegistry


def _registry(tmp_path, *, with_graph=True):
    notes = tmp_path / "notes"
    notes.mkdir()
    for name, content in {
        "Agent.md": "Agent 是能自主调用工具、规划任务来完成目标的大模型应用。",
        "RAG.md": "RAG 是检索增强生成的缩写，先检索相关资料再生成。",
        "Embedding.md": "Embedding 是把文字转换成向量。",
        "向量数据库.md": "向量数据库专门用来存储和检索向量。",
        "PHP.md": "PHP 是一种脚本语言。",
    }.items():
        (notes / name).write_text(content, encoding="utf-8")

    kb = KnowledgeBase(BagOfWordsEmbedder())
    sync_notes(kb, notes)

    graph = None
    if with_graph:
        graph = GraphStore()
        graph.merge_note(
            "Agent.md",
            nodes=[{"name": "Agent"}, {"name": "RAG"}],
            edges=[{"source": "Agent", "target": "RAG", "relation": "依赖"}],
        )
        graph.merge_note(
            "RAG.md",
            nodes=[{"name": "RAG"}, {"name": "Embedding"}, {"name": "向量数据库"}],
            edges=[
                {"source": "RAG", "target": "Embedding", "relation": "依赖"},
                {"source": "Embedding", "target": "向量数据库", "relation": "依赖"},
            ],
        )
        graph.merge_note("PHP.md", nodes=[{"name": "PHP"}], edges=[])

    ctx = ToolContext(notes_dir=notes, kb=kb, graph=graph)
    registry = ToolRegistry()
    register_note_tools(registry, ctx)
    return registry


def _search(registry, query):
    return registry.execute("search_notes", json.dumps({"query": query}))


def test_island_anchor_skips_graph_nav(tmp_path):
    # 孤岛锚点 PHP：图无边 → 导航空集 → 零追加（L1 零成本的来源）
    out = _search(_registry(tmp_path), "PHP 是什么")
    assert "【图谱导航】" not in out
    assert "PHP 是一种脚本语言" in out   # base 命中仍在


def test_chain_anchor_appends_graph_nav(tmp_path):
    # 锚定 RAG（非孤岛）→ 2 跳带出 Agent.md（Agent→RAG 边）→ 追加其块
    out = _search(_registry(tmp_path), "RAG 是什么")
    assert "【图谱导航】" in out
    assert "Agent 是能自主调用工具" in out   # 邻笔记块被导航带出（base 不命中它）
    assert "RAG 是检索增强生成" in out        # base 命中仍在


def test_no_graph_silently_skips(tmp_path):
    # graph 未装配：照旧纯向量返回，不导航、不加段、不炸（教学组合降级路径）
    out = _search(_registry(tmp_path, with_graph=False), "RAG 是什么")
    assert "【图谱导航】" not in out   # 词袋可能命中也可能空，两种都不该带导航段
