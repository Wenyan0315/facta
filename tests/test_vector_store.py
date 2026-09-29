"""M7 存储层验收：双实现（InMemory / Chroma）都守同一份「分数契约」。

分数契约：query 返回「余弦相似度」，越大越像。
Chroma 内部是「距离」（= 1 - 相似度），换算封装在实现里——
这批测试盯的就是：换算有没有错、持久化是不是真的。
"""

import pytest

from facta.knowledge.vector_store import (
    ChromaVectorStore,
    InMemoryVectorStore,
    SearchHit,
)


def _sample_data():
    """两条正交向量：「PHP」向量 [1,0] 与「Python」向量 [0,1]。"""
    return {
        "ids": ["n1", "n2"],
        "chunks": ["PHP 是最好的语言", "Python 是脚本语言"],
        "vectors": [[1.0, 0.0], [0.0, 1.0]],
        "metadatas": [
            {"source": "php.md", "hash": "h1"},
            {"source": "py.md", "hash": "h2"},
        ],
    }


def test_inmemory_roundtrip():
    store = InMemoryVectorStore()
    d = _sample_data()
    store.upsert(d["ids"], d["chunks"], d["vectors"], d["metadatas"])
    assert store.count() == 2
    # 分数契约第一血：完全相同的向量 → 相似度 1.0
    hits = store.query([1.0, 0.0], top_k=1)
    assert hits[0].chunk == "PHP 是最好的语言"
    assert hits[0].score == pytest.approx(1.0)
    # 溯源契约：query 必须带回 upsert 时写的面单（S4 评审 #R5）
    assert hits[0].source == "php.md"
    assert isinstance(hits[0], SearchHit)
    assert store.get_all()["n1"]["source"] == "php.md"
    store.delete(["n1"])
    assert store.count() == 1
    store.clear()
    assert store.count() == 0


def test_query_top_k_beyond_count_ok():
    store = InMemoryVectorStore()
    d = _sample_data()
    store.upsert(d["ids"], d["chunks"], d["vectors"], d["metadatas"])
    # top_k 超过库存不该崩，有多少给多少
    assert len(store.query([1.0, 1.0], top_k=10)) == 2


def test_chroma_score_conversion_and_persistence(tmp_path):
    pytest.importorskip("chromadb")   # rag 可选依赖没装就跳过，不炸全绿
    store = ChromaVectorStore(tmp_path / "db")
    d = _sample_data()
    store.upsert(d["ids"], d["chunks"], d["vectors"], d["metadatas"])

    results = store.query([1.0, 0.0], top_k=1)
    assert results[0].chunk == "PHP 是最好的语言"
    # 距离 → 相似度换算：Chroma 内部吐 0.0 距离，这里必须变回 1.0 相似度
    assert results[0].score == pytest.approx(1.0, abs=0.01)
    # 溯源契约对工业版同样成立（S4 评审 #R5）
    assert results[0].source == "php.md"

    # 关库重开：数据还在——这是「重启不重算」成立的物理前提
    store2 = ChromaVectorStore(tmp_path / "db")
    assert store2.count() == 2
    assert store2.query([0.0, 1.0], top_k=1)[0].chunk == "Python 是脚本语言"


def test_chroma_clear(tmp_path):
    pytest.importorskip("chromadb")
    store = ChromaVectorStore(tmp_path / "db")
    d = _sample_data()
    store.upsert(d["ids"], d["chunks"], d["vectors"], d["metadatas"])
    store.clear()
    assert store.count() == 0
    assert store.get_all() == {}
    assert store.query([1.0, 0.0], 1) == []


def test_inmemory_vs_chroma_same_ranking(tmp_path):
    pytest.importorskip("chromadb")
    d = _sample_data()
    mem = InMemoryVectorStore()
    chroma = ChromaVectorStore(tmp_path / "db")
    mem.upsert(d["ids"], d["chunks"], d["vectors"], d["metadatas"])
    chroma.upsert(d["ids"], d["chunks"], d["vectors"], d["metadatas"])
    query_vector = [0.6, 0.8]
    # 两个实现给出相同的排序——双实现行为一致是「换件不换衣服」的证明
    assert [h.chunk for h in mem.query(query_vector, 2)] == [
        h.chunk for h in chroma.query(query_vector, 2)
    ]
    # 溯源面单也要一致：工业版不比教学版少带信息
    assert [h.source for h in mem.query(query_vector, 2)] == [
        h.source for h in chroma.query(query_vector, 2)
    ]
