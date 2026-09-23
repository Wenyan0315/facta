"""S7a 图谱管线验收：抽取解析 / 增量同步 / query_graph 工具端到端。

抽取层用 ScriptedLLM 测（不碰真模型）：解析容错、结构校验、失败语义。
同步层测指纹差集三路（增/删/不变）+ 教学路径 + 删除安全阀。
工具层测三原语的错误串指路（M5 反馈环：错误文案即提示词）。
"""

import json

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.knowledge.extract import extract_note, sync_graph
from agent.knowledge.graph import GraphStore
from agent.tools.context import ToolContext
from agent.tools.graph import register_graph_tools
from agent.tools.registry import ToolRegistry


def _extract_llm(payload) -> ScriptedLLM:
    return ScriptedLLM([Message(role="assistant", content=json.dumps(payload, ensure_ascii=False))])


def _tool_registry(store: GraphStore, notes_dir=None, llm=None) -> ToolRegistry:
    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=notes_dir, graph=store, llm=llm)   # type: ignore[arg-type]
    register_graph_tools(registry, ctx)
    return registry


# ---------- 抽取解析 ----------


def test_extract_note_parses_clean_json():
    llm = _extract_llm({
        "nodes": [{"name": "RAG", "type": "概念", "aliases": ["检索增强生成"]}],
        "edges": [{"source": "RAG", "target": "Embedding", "relation": "依赖"}],
    })
    result, err = extract_note(llm, "RAG.md", "RAG 是检索增强生成……", [])
    assert err == ""
    nodes, edges = result
    assert nodes[0]["name"] == "RAG"
    assert edges[0]["relation"] == "依赖"


def test_extract_note_strips_code_fences():
    # 模型坏习惯：围栏包裹——容错解析（consolidate 同款）
    fenced = '```json\n{"nodes": [{"name": "Agent"}], "edges": []}\n```'
    llm = ScriptedLLM([Message(role="assistant", content=fenced)])
    result, err = extract_note(llm, "x.md", "正文", [])
    assert err == ""
    assert result[0][0]["name"] == "Agent"


def test_extract_note_rejects_bad_json():
    llm = ScriptedLLM([Message(role="assistant", content="我觉得这篇讲的是RAG")])
    result, err = extract_note(llm, "x.md", "正文", [])
    assert result is None and "JSON" in err


def test_extract_note_rejects_wrong_shape():
    llm = _extract_llm({"nodes": "不是数组", "edges": []})
    result, err = extract_note(llm, "x.md", "正文", [])
    assert result is None and "结构" in err


def test_extract_note_llm_failure_returns_error():
    class BoomLLM(ScriptedLLM):
        def generate(self, messages, tools=None):
            raise RuntimeError("超时")

    result, err = extract_note(BoomLLM([]), "x.md", "正文", [])
    assert result is None and "失败" in err


# ---------- 增量同步 ----------


def _write_note(notes_dir, name: str, text: str) -> None:
    (notes_dir / name).write_text(text, encoding="utf-8")


def test_sync_graph_extracts_new_notes(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    _write_note(notes, "RAG.md", "RAG 依赖 Embedding。")
    store = GraphStore()
    llm = _extract_llm({
        "nodes": [{"name": "RAG"}, {"name": "Embedding"}],
        "edges": [{"source": "RAG", "target": "Embedding", "relation": "依赖"}],
    })
    report = sync_graph(store, notes, llm)
    assert report.extracted == 1 and report.unchanged == 0
    assert store.path("RAG", "Embedding") is not None
    assert store.note_hashes["RAG.md"]


def test_sync_graph_unchanged_notes_zero_cost(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    _write_note(notes, "RAG.md", "内容稳定")
    store = GraphStore()
    llm = _extract_llm({"nodes": [{"name": "RAG"}], "edges": []})
    sync_graph(store, notes, llm)
    # 第二次同步：指纹相同 → llm 不该被叫（脚本弹完会兜底回纯文本——
    # 若被叫，抽取输出解析失败 → failed>0，断言即红）
    report = sync_graph(store, notes, llm)
    assert report.unchanged == 1 and report.extracted == 0 and report.failed == 0


def test_sync_graph_changed_note_replaces_edges(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    _write_note(notes, "RAG.md", "RAG 依赖 Embedding。")
    store = GraphStore()
    sync_graph(store, notes, _extract_llm({
        "nodes": [{"name": "RAG"}, {"name": "Embedding"}],
        "edges": [{"source": "RAG", "target": "Embedding", "relation": "依赖"}],
    }))
    # 笔记改版：不再提 Embedding，改提 向量数据库
    _write_note(notes, "RAG.md", "RAG 依赖 向量数据库。")
    report = sync_graph(store, notes, _extract_llm({
        "nodes": [{"name": "RAG"}, {"name": "向量数据库"}],
        "edges": [{"source": "RAG", "target": "向量数据库", "relation": "依赖"}],
    }))
    assert report.extracted == 1
    assert store.path("RAG", "Embedding") is None        # 旧边失效
    assert store.path("RAG", "向量数据库") is not None    # 新边在
    assert store.resolve("Embedding") is not None          # 节点保留（不连坐）


def test_sync_graph_removed_note_clears_edges_keeps_nodes(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    _write_note(notes, "RAG.md", "RAG 依赖 Embedding。")
    _write_note(notes, "留守.md", "留守内容")   # 留一篇幸存者（scan_notes 空目录守卫——M7 同款防线）
    store = GraphStore()
    sync_graph(store, notes, _extract_llm({
        "nodes": [{"name": "RAG"}, {"name": "Embedding"}],
        "edges": [{"source": "RAG", "target": "Embedding", "relation": "依赖"}],
    }))
    (notes / "RAG.md").unlink()
    report = sync_graph(store, notes, _extract_llm(
        {"nodes": [{"name": "留守"}], "edges": []}))
    assert report.removed == 1
    assert store.path("RAG", "Embedding") is None
    assert set(store.nodes) == {"RAG", "Embedding", "留守"}   # 节点保留（不连坐）


def test_sync_graph_teaching_path_skips_without_hashes(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    _write_note(notes, "RAG.md", "内容")
    store = GraphStore()
    report = sync_graph(store, notes, None)   # llm=None：教学路径
    assert report.skipped == 1 and report.extracted == 0
    assert "RAG.md" not in store.note_hashes   # 指纹不记——切真模型自动补抽


def test_sync_graph_single_failure_doesnt_block_others(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    _write_note(notes, "A.md", "A 内容")
    _write_note(notes, "B.md", "B 内容")
    # 第一次 generate 输出坏 JSON（A 失败），第二次输出合法（B 成功）
    llm = ScriptedLLM([
        Message(role="assistant", content="坏输出"),
        Message(role="assistant", content=json.dumps(
            {"nodes": [{"name": "B"}], "edges": []}, ensure_ascii=False)),
    ])
    store = GraphStore()
    report = sync_graph(store, notes, llm)
    assert report.failed == 1 and report.extracted == 1
    assert "B" in store.nodes and "A.md" not in store.note_hashes


# ---------- query_graph 工具（错误串指路） ----------


def _demo_store() -> GraphStore:
    g = GraphStore()
    g.merge_note("RAG.md",
                 nodes=[{"name": "RAG", "aliases": ["检索增强生成"]},
                        {"name": "Embedding"}, {"name": "向量数据库"}],
                 edges=[{"source": "RAG", "target": "Embedding", "relation": "依赖"},
                        {"source": "Embedding", "target": "向量数据库", "relation": "依赖"}])
    g.merge_note("孤岛.md", nodes=[{"name": "PHP"}], edges=[])
    return g


def test_query_graph_neighbors_with_alias():
    registry = _tool_registry(_demo_store())
    out = registry.execute("query_graph", json.dumps(
        {"action": "neighbors", "entity": "检索增强生成"}))
    assert "RAG" in out and "Embedding" in out and "出处" in out


def test_query_graph_neighbors_unknown_entity_guides():
    registry = _tool_registry(_demo_store())
    out = registry.execute("query_graph", json.dumps(
        {"action": "neighbors", "entity": "不存在"}))
    assert "没有" in out and "RAG" in out   # 错误串带可用实体示例（指路）


def test_query_graph_path_renders_chain():
    registry = _tool_registry(_demo_store())
    out = registry.execute("query_graph", json.dumps(
        {"action": "path", "from": "RAG", "to": "向量数据库"}))
    assert "RAG --依赖--> Embedding" in out and "Embedding --依赖--> 向量数据库" in out


def test_query_graph_path_disconnected_honest():
    registry = _tool_registry(_demo_store())
    out = registry.execute("query_graph", json.dumps(
        {"action": "path", "from": "RAG", "to": "PHP"}))
    assert "没有连通" in out   # 孤岛如实报告，不编造


def test_query_graph_overview_stats():
    registry = _tool_registry(_demo_store())
    out = registry.execute("query_graph", json.dumps({"action": "overview"}))
    assert "4 个" in out and "孤岛" in out and "PHP" in out


def test_query_graph_empty_store_honest():
    registry = _tool_registry(GraphStore())
    out = registry.execute("query_graph", json.dumps({"action": "overview"}))
    assert "为空" in out and "search_notes" in out   # 指路替代工具


def test_query_graph_unknown_action_guides():
    registry = _tool_registry(_demo_store())
    out = registry.execute("query_graph", json.dumps({"action": "查询"}))
    assert "neighbors" in out and "path" in out and "overview" in out


def test_query_graph_missing_param_guides():
    registry = _tool_registry(_demo_store())
    out = registry.execute("query_graph", json.dumps({"action": "neighbors"}))
    assert "entity" in out   # 缺参指路


# ---------- sync_graph 工具（界面可操作） ----------


def _sync_env(tmp_path, monkeypatch):
    """装配带抽取通道的图谱工具环境。

    GRAPH_PATH 的 patch 必须用 pytest monkeypatch（作用域=单测试，结束自动
    还原）——曾试过 generator+finally 手工还原：list() 耗尽 generator 时
    patch 就撤了，工具落盘会打到真项目 data/graph.json（S6a「patch 消费方
    模块」血案的变体——patch 的生命周期与使用窗口必须对齐）。
    """
    import agent.tools.graph as graph_tools_mod
    monkeypatch.setattr(graph_tools_mod, "GRAPH_PATH", tmp_path / "graph.json")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "A.md").write_text("A 依赖 B。", encoding="utf-8")
    store = GraphStore()
    payload = {
        "nodes": [{"name": "A"}, {"name": "B"}],
        "edges": [{"source": "A", "target": "B", "relation": "依赖"}],
    }
    # 两条同款脚本：增量一次 + force 重抽一次（ScriptedLLM 弹完会兜底纯文本
    # → 解析失败——force 测试的两次 sync 各要一条合法 JSON）
    llm = ScriptedLLM([
        Message(role="assistant", content=json.dumps(payload, ensure_ascii=False)),
        Message(role="assistant", content=json.dumps(payload, ensure_ascii=False)),
    ])
    registry = _tool_registry(store, notes_dir=notes, llm=llm)
    return registry, store, tmp_path / "graph.json"


def test_sync_graph_tool_extracts_and_saves(tmp_path, monkeypatch):
    registry, store, graph_file = _sync_env(tmp_path, monkeypatch)
    out = registry.execute("sync_graph", "{}")
    assert "抽取 1 篇" in out and "2 个概念" in out
    assert graph_file.exists()                      # 落盘（知识资产）
    import json as _json
    data = _json.loads(graph_file.read_text(encoding="utf-8"))
    assert data["nodes"] and data["edges"]


def test_sync_graph_tool_zero_change_hint(tmp_path, monkeypatch):
    registry, store, _ = _sync_env(tmp_path, monkeypatch)
    registry.execute("sync_graph", "{}")            # 第一次：抽取
    out = registry.execute("sync_graph", "{}")      # 第二次：指纹相同零抽取
    assert "已是最新" in out and "零抽取" in out


def test_sync_graph_tool_force_full_reextract(tmp_path, monkeypatch):
    registry, store, _ = _sync_env(tmp_path, monkeypatch)
    registry.execute("sync_graph", "{}")
    # force=True：清指纹全量重抽（换更强模型的场景）
    out = registry.execute("sync_graph", '{"force": true}')
    assert "抽取 1 篇" in out


def test_sync_graph_not_registered_without_llm(tmp_path):
    # 条件注册：ctx.llm 缺席 → sync_graph 不上菜单（query_graph 仍在）
    notes = tmp_path / "notes"
    notes.mkdir()
    registry = _tool_registry(GraphStore(), notes_dir=notes, llm=None)
    assert "query_graph" in registry.names()
    assert "sync_graph" not in registry.names()


def test_sync_graph_forbidden_to_subagent():
    from agent.tools.spawn import _FORBIDDEN
    assert "sync_graph" in _FORBIDDEN   # 子 agent 不动共享图谱


def test_write_note_hints_sync_graph(tmp_path):
    # 闭环引导：写完笔记提示图谱更新入口（make_plan 回灌同款）
    from agent.tools.builtin import register_builtin
    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=tmp_path)
    register_builtin(registry, ctx)
    out = registry.execute("write_note", json.dumps(
        {"filename": "新概念.md", "content": "新概念是……"}))
    assert "sync_graph" in out
