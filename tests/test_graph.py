"""S7a 图谱数据层验收：校验闸门 / 查询原语 / 序列化 / 增量替换。

不变量：
- 三道校验闸门：关系白名单、端点存在、出处非空——非法输入 ValueError
- 实体对齐：别名 resolve 到同一 id（图谱一等能力）
- path 无向 BFS：跳数内最短链路，超跳数如实 None（不编造）
- merge_note 原子替换：旧边失效、节点保留、出处程序侧强制
- 序列化往返：save→load 后图等价（邻接索引重建）
"""

import pytest

from facta.knowledge.graph import GraphStore


def _demo_graph() -> GraphStore:
    """最小教学图：Agent→RAG→Embedding→向量数据库 的概念链 + 孤岛。"""
    g = GraphStore()
    g.merge_note(
        "Agent.md",
        nodes=[{"name": "Agent", "type": "概念"}, {"name": "RAG"}],
        edges=[{"source": "Agent", "target": "RAG", "relation": "依赖"}],
    )
    g.merge_note(
        "RAG.md",
        nodes=[
            {"name": "RAG", "aliases": ("检索增强生成",)},
            {"name": "Embedding"},
            {"name": "向量数据库"},
        ],
        edges=[
            {"source": "RAG", "target": "Embedding", "relation": "依赖"},
            {"source": "Embedding", "target": "向量数据库", "relation": "依赖"},
        ],
    )
    g.merge_note("孤岛.md", nodes=[{"name": "PHP"}], edges=[])
    return g


# ---------- 校验闸门 ----------


def test_add_edge_rejects_unknown_relation():
    g = GraphStore()
    g.add_node("A")
    g.add_node("B")
    with pytest.raises(ValueError, match="白名单"):
        g.add_edge("A", "B", "导致了", "x.md")   # 白名单外关系


def test_add_edge_rejects_missing_endpoint():
    g = GraphStore()
    g.add_node("A")
    with pytest.raises(ValueError, match="不在图中"):
        g.add_edge("A", "幽灵", "依赖", "x.md")


def test_add_edge_rejects_empty_source_note():
    g = GraphStore()
    g.add_node("A")
    g.add_node("B")
    with pytest.raises(ValueError, match="出处"):
        g.add_edge("A", "B", "依赖", "  ")   # 出处空白 = 拒收


def test_add_node_merges_aliases_idempotently():
    # 重名节点幂等：两篇笔记都抽到同一实体 → 别名并集、类型保留
    g = GraphStore()
    g.add_node("RAG", "概念", ("检索增强生成",))
    merged = g.add_node("RAG", "技术", ("RAG技术",))
    assert merged.type == "概念"          # 先到的类型不被覆盖
    # 断言顺序而非 set 相等：graph.json 是跟踪文件，别名换序 = 每次重建
    # 都出噪声 diff（曾用 set 合并，顺序随机）。老别名在前，新别名追加末尾。
    assert merged.aliases == ("检索增强生成", "RAG技术")


# ---------- 实体对齐（resolve） ----------


def test_resolve_via_alias():
    g = _demo_graph()
    assert g.resolve("RAG") == "RAG"
    assert g.resolve("检索增强生成") == "RAG"   # 别名归一到同一实体
    assert g.resolve("不存在的东西") is None


# ---------- 查询原语 ----------


def test_neighbors_with_relation_filter():
    g = _demo_graph()
    all_edges = g.neighbors("RAG")
    assert len(all_edges) == 2                       # Agent 依赖 RAG、RAG 依赖 Embedding
    deps = g.neighbors("RAG", relation="依赖")
    assert len(deps) == 2
    contrast = g.neighbors("RAG", relation="对比")
    assert contrast == []                            # 过滤后空集如实返回


def test_path_finds_shortest_chain():
    g = _demo_graph()
    trail = g.path("Agent", "向量数据库")
    assert trail is not None
    assert len(trail) == 3                           # Agent→RAG→Embedding→向量数据库
    assert [e.relation for e in trail] == ["依赖", "依赖", "依赖"]


def test_path_unlimited_direction():
    # 无向遍历：从「被依赖方」也能走到「依赖方」
    g = _demo_graph()
    trail = g.path("向量数据库", "Agent")
    assert trail is not None and len(trail) == 3


def test_path_respects_max_hops():
    g = _demo_graph()
    assert g.path("Agent", "向量数据库", max_hops=2) is None   # 3 跳的路 2 跳到不了
    assert g.path("Agent", "向量数据库", max_hops=3) is not None


def test_path_disconnected_returns_none():
    g = _demo_graph()
    assert g.path("Agent", "PHP") is None   # 孤岛不连通，如实 None


def test_path_same_node_returns_empty():
    g = _demo_graph()
    assert g.path("RAG", "RAG") == []


def test_overview_reports_stats_and_islands():
    g = _demo_graph()
    stats = g.overview()
    assert stats["nodes"] == 5                    # Agent/RAG/Embedding/向量数据库/PHP
    assert stats["edges"] == 3
    assert stats["islands"] == ["PHP"]           # 孤岛如实报告
    assert stats["hubs"][0]["name"] in ("RAG", "Embedding")   # 中间概念度数最高
    assert stats["relations"]["依赖"] == 3


# ---------- related_notes（043 检索分诊的图导航原语） ----------


def test_related_notes_anchors_and_expands():
    # query 锚定 RAG → BFS 2 跳沿途 source_note 层序去重限量
    g = _demo_graph()
    assert g.related_notes("RAG", hops=2, limit=2) == ["Agent.md", "RAG.md"]


def test_related_notes_via_alias():
    # 别名锚定：query 用「检索增强生成」也能落到 RAG
    g = _demo_graph()
    assert "RAG.md" in g.related_notes("检索增强生成是什么")


def test_related_notes_no_anchor_returns_empty():
    g = _demo_graph()
    assert g.related_notes("量子力学") == []


def test_related_notes_island_anchor_returns_empty():
    # 孤岛锚点（PHP 无边）→ 导航空集：L1 零成本的来源
    g = _demo_graph()
    assert g.related_notes("PHP") == []


def test_related_notes_limit():
    g = _demo_graph()
    assert len(g.related_notes("RAG", hops=2, limit=1)) == 1


# ---------- merge_note 原子替换（增量同步核心语义） ----------


def test_merge_note_replaces_old_edges_keeps_nodes():
    # 笔记改版重抽：旧边全失效、节点保留（别篇还引用着）
    g = _demo_graph()
    before = g.neighbors("RAG")
    assert len(before) == 2

    # RAG.md 改版：不再提 Embedding，改提 向量数据库
    g.merge_note(
        "RAG.md",
        nodes=[{"name": "RAG"}, {"name": "向量数据库"}],
        edges=[{"source": "RAG", "target": "向量数据库", "relation": "依赖"}],
    )
    # Embedding 节点活着（Agent.md 的边不经过它，但它作为实体仍在图里）
    assert g.resolve("Embedding") is not None
    # 旧 RAG.md 的边没了（RAG→Embedding 失效），新边在
    all_rag = g.neighbors("RAG")
    assert len(all_rag) == 2                       # Agent 依赖 RAG（Agent.md 的）+ RAG 依赖 向量数据库（新的）
    assert g.path("RAG", "Embedding") is None     # 旧链路断了


def test_merge_note_skips_dirty_entries():
    # 单条脏数据（白名单外关系/缺字段）跳过不炸整篇同步
    g = GraphStore()
    g.merge_note("x.md", nodes=[
        {"name": "A"},
        {"bad": "node"},                          # 缺 name
    ], edges=[
        {"source": "A", "target": "A", "relation": "依赖"},   # 合法自环（暂不禁：对比关系可自指两实体）
        {"source": "A", "target": "A", "relation": "玄学"},   # 白名单外 → 跳过
    ])
    assert set(g.nodes) == {"A"}
    assert len(g._edges) == 1


def test_source_note_is_program_filled():
    # 出处由 merge_note 程序侧强制填——edges 入参里没有 source_note 字段
    g = GraphStore()
    g.merge_note("出处篇.md", nodes=[{"name": "A"}, {"name": "B"}],
                 edges=[{"source": "A", "target": "B", "relation": "引用"}])
    edge = g.neighbors("A")[0]
    assert edge.source_note == "出处篇.md"


# ---------- 序列化往返 ----------


def test_save_load_roundtrip(tmp_path):
    g = _demo_graph()
    g.note_hashes["Agent.md"] = "abc123"
    p = tmp_path / "graph.json"
    g.save(p)

    loaded = GraphStore.load(p)
    assert set(loaded.nodes) == set(g.nodes)
    assert len(loaded._edges) == len(g._edges)
    assert loaded.note_hashes == {"Agent.md": "abc123"}
    # 邻接索引重建可用：查询原语照常工作
    trail = loaded.path("Agent", "向量数据库")
    assert trail is not None and len(trail) == 3
    # 别名也往返
    assert loaded.resolve("检索增强生成") == "RAG"


def test_load_missing_or_corrupt_file_returns_empty(tmp_path):
    assert GraphStore.load(tmp_path / "nope.json").nodes == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{ broken", encoding="utf-8")
    assert GraphStore.load(bad).nodes == {}
