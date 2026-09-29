"""M7 同步验收：把设计里的不变量逐条钉成断言。

覆盖：首轮全增 / 第二轮幂等白嫖 / 改·删·增三路 / 改名免费 / 删除安全阀 /
重启零重算 / 词袋退化全量重建 / 空目录中止。
"""

import pytest

from facta.knowledge.knowledge_base import (
    BagOfWordsEmbedder,
    Embedder,
    KnowledgeBase,
)
from facta.knowledge.sync import sync_notes


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


class CountingEmbedder(Embedder):
    """离线增量假 embedder：固定 4 维向量（长度, 'a' 计数, 0, 0），记录 embed 块数。

    向量由文本长度和 'a' 出现次数唯一决定，测试里可以用精确向量当查询探针。
    supports_incremental = True → 走同步的增量分支，不碰网络。
    """

    supports_incremental = True
    default_min_score = 0.0

    def __init__(self) -> None:
        self.embed_calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += len(texts)
        return [[float(len(t)), float(t.count("a")), 0.0, 0.0] for t in texts]


def test_first_sync_adds_everything(tmp_path):
    _write(tmp_path, "php.md", "PHP 是最好的语言。")
    _write(tmp_path, "python.md", "Python 是脚本语言。")
    kb = KnowledgeBase(CountingEmbedder())
    report = sync_notes(kb, tmp_path)
    assert report.added == 2
    assert kb.store.count() == 2  # 每篇一个块


def test_second_sync_is_idempotent(tmp_path):
    _write(tmp_path, "php.md", "AAAA")
    kb = KnowledgeBase(CountingEmbedder())
    sync_notes(kb, tmp_path)
    calls_after_first = kb.embedder.embed_calls

    report = sync_notes(kb, tmp_path)
    assert (report.added, report.removed) == (0, 0)
    assert report.unchanged == 1
    assert kb.embedder.embed_calls == calls_after_first  # 第二轮白嫖：零 embed


def test_modified_note_replaces_chunks(tmp_path):
    p = _write(tmp_path, "php.md", "AAAA")  # 向量 [4,4,0,0]
    kb = KnowledgeBase(CountingEmbedder())
    sync_notes(kb, tmp_path)
    p.write_text("BBBBBB", encoding="utf-8")  # 向量 [6,0,0,0] → 新指纹

    report = sync_notes(kb, tmp_path)
    assert report.removed == 1 and report.added == 1
    assert kb.store.count() == 1  # 删旧 + 增新，块数不变
    # 用精确向量当探针：旧块必须死亡、新块必须活着
    assert kb.store.query([4.0, 4.0, 0.0, 0.0], 1)[0].chunk != "AAAA"
    assert kb.store.query([6.0, 0.0, 0.0, 0.0], 1)[0].chunk == "BBBBBB"


def test_deleted_note_removes_chunks(tmp_path):
    _write(tmp_path, "php.md", "AAAA")
    p2 = _write(tmp_path, "py.md", "BB")
    kb = KnowledgeBase(CountingEmbedder())
    sync_notes(kb, tmp_path)
    p2.unlink()

    report = sync_notes(kb, tmp_path)
    assert report.removed == 1
    assert kb.store.count() == 1


def test_rename_is_free(tmp_path):
    p = _write(tmp_path, "php.md", "AAAA")
    kb = KnowledgeBase(CountingEmbedder())
    sync_notes(kb, tmp_path)
    calls_after_first = kb.embedder.embed_calls
    p.rename(tmp_path / "php-renamed.md")  # 只改名，内容不变

    report = sync_notes(kb, tmp_path)
    assert (report.added, report.removed) == (0, 0)  # 指纹没变 → 交集
    assert kb.embedder.embed_calls == calls_after_first


def test_mass_deletion_guard(tmp_path):
    # 4 篇不同内容的笔记删 3 篇：3 篇 ≥ 下限、占比 75% > 20% → 触发安全阀
    # （内容必须互不相同——同内容会共用一个指纹，正是去重特性的作用）
    for name in ("a.md", "b.md", "c.md", "d.md"):
        _write(tmp_path, name, f"笔记{name}的内容")
    kb = KnowledgeBase(CountingEmbedder())
    sync_notes(kb, tmp_path)
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).unlink()

    with pytest.raises(RuntimeError, match="安全阀|同时消失"):
        sync_notes(kb, tmp_path)


def test_small_deletion_passes_guard(tmp_path):
    # 对照：删 1 篇不满下限 → 正常通过，不被安全阀误伤
    _write(tmp_path, "a.md", "AAAA")
    p2 = _write(tmp_path, "b.md", "BB")
    kb = KnowledgeBase(CountingEmbedder())
    sync_notes(kb, tmp_path)
    p2.unlink()
    report = sync_notes(kb, tmp_path)
    assert report.removed == 1


def test_restart_no_reembed(tmp_path):
    pytest.importorskip("chromadb")
    from facta.knowledge.vector_store import ChromaVectorStore

    _write(tmp_path, "php.md", "AAAA")
    db_dir = tmp_path / "db"
    kb = KnowledgeBase(CountingEmbedder(), ChromaVectorStore(db_dir))
    sync_notes(kb, tmp_path)
    count_after_first = kb.store.count()

    # 模拟重启：全新 KB + 指向同一目录的新 store
    kb2 = KnowledgeBase(CountingEmbedder(), ChromaVectorStore(db_dir))
    report = sync_notes(kb2, tmp_path)
    assert (report.added, report.removed) == (0, 0)
    assert kb2.embedder.embed_calls == 0  # 重启零 embed：M7 省钱的验收核心
    assert kb2.store.count() == count_after_first


def test_bow_falls_back_to_full_rebuild(tmp_path):
    _write(tmp_path, "php.md", "PHP 是最好的语言")
    _write(tmp_path, "py.md", "Python 是脚本语言")
    kb = KnowledgeBase(BagOfWordsEmbedder())  # 不支持增量 → 清库全量重建
    r1 = sync_notes(kb, tmp_path)
    r2 = sync_notes(kb, tmp_path)
    assert r1.added == 2 and r1.removed == 0
    assert r2.added == 2 and r2.removed == 0  # 重建路径每轮都算「全增」
    assert kb.store.count() == 2
    # 词袋必须真能用：搜 PHP 排第一的应是 PHP 那条（fit 没被漏掉）
    top = kb.search("PHP", top_k=1, min_score=0.0)
    assert top[0].chunk == "PHP 是最好的语言"
    assert top[0].source == "php.md"   # 溯源不变量：命中必须自带出处（S4 评审 #R5）


def test_empty_dir_raises(tmp_path):
    kb = KnowledgeBase(CountingEmbedder())
    with pytest.raises(FileNotFoundError):
        sync_notes(kb, tmp_path)  # 空目录 = 环境事故，不给「清空全库」的机会
