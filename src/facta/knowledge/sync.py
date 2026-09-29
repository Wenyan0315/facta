"""M7 增量同步：让向量库与笔记目录对齐，只为真正变了的内容花 embedding 的钱。

身份 = 内容指纹（sha256 前 16 位）——只认内容，不认文件名、不认修改时间：
- 改名：指纹不变 → 交集 → 零成本（用文件名当 id 会误判「删+增」的败笔）
- touch：内容没变 → 交集 → 零成本（用 mtime 当判据会误判「改」）
- 真改了：旧指纹消失、新指纹出现 → 删+增，这正是该花的钱

三路差集：
  增 = 磁盘指纹 - 库里指纹 → 切块 → embed（花钱的唯一环节）→ upsert
  删 = 库里指纹 - 磁盘指纹 → delete（带安全阀）
  不变 = 交集 → 不重新 embed——注意：算指纹必须读每篇全文（哈希身份的
         固有成本，本地磁盘读很便宜）；真正省的从来是 embed 不是读盘

两道护栏（对应「删除语义过重」的坑）：
1. 空目录由 scan_notes 直接抛错——目录暂时不见了 ≠ 清空知识库的许可
2. 待删量占比超 DELETE_GUARD_RATIO 直接中止——防误移动/挂载失败清空全库

词袋退化路径：词袋向量维度 = 词表长度，新词进来旧向量全部作废，增量对
词袋不成立。同步检测 embedder.supports_incremental 为假时，走「清库 +
全量重建」，行为等同 M7 之前（启动全量重算），只是存取换到新接口。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from facta.knowledge.knowledge_base import KnowledgeBase, chunk_text
from facta.knowledge.loader import scan_notes

# 删除安全阀：消失的文件 ≥ GUARD_MIN_FILES 篇 **且** 占比 > GUARD_RATIO 才拦。
# 纯比例阈值在小库里是噪声（删 1/2 篇就 50%/100%），必须带绝对下限（同「SQLite
# 迁移触发信号」一课的教训：阈值无下限 = 正常操作被误伤）。
GUARD_MIN_FILES = 3
DELETE_GUARD_RATIO = 0.2


@dataclass
class SyncReport:
    """一次同步的账本，按「内容指纹」计数（同内容的多篇文件算一篇）。"""

    added: int = 0
    removed: int = 0
    unchanged: int = 0

    def total(self) -> int:
        return self.added + self.removed + self.unchanged


def file_hash(text: str) -> str:
    """内容指纹：sha256 截前 16 位（64bit）。教学规模下碰撞概率可忽略。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def sync_notes(kb: KnowledgeBase, notes_dir: Path | str) -> SyncReport:
    """把 notes_dir 与向量库对齐。只给「新指纹」的内容花 embed 的钱。"""
    store = kb.store
    embedder = kb.embedder
    incremental = getattr(embedder, "supports_incremental", False)

    # 1. 扫磁盘 → {指纹: (文件名, 原文)}；空内容文件无块可存，不进库
    #    去重与标签：同内容多文件共用指纹，source 记第一个文件名；
    #    该文件删除后若同内容幸存，source 标签不回写（人读标签可能陈旧，
    #    检索运算只看 hash，正确性不受影响——已知边界）
    disk: dict[str, tuple[str, str]] = {}
    for name, text in scan_notes(notes_dir):
        if text.strip():
            disk.setdefault(file_hash(text), (name, text))

    stored = store.get_all()  # id → {source, hash}
    stored_hashes = {meta["hash"] for meta in stored.values()}
    disk_hashes = set(disk)

    to_remove_hashes = stored_hashes - disk_hashes
    unchanged = disk_hashes & stored_hashes

    doomed_ids = [
        id_ for id_, meta in stored.items() if meta["hash"] in to_remove_hashes
    ]

    if incremental:
        will_add = disk_hashes - stored_hashes
    else:
        # 词袋退化：先让 embedder 过目全部语料建词表（fit），再全量重建——
        # 不 fit 就 embed，词表是空的，所有向量全是零向量（旧 _rebuild 也是两步）
        store.clear()
        doomed_ids = []
        will_add = disk_hashes
        all_chunks = [c for _, text in disk.values() for c in chunk_text(text)]
        embedder.fit(all_chunks)

    # 2. 安全阀：按「文件消失」判，修改不算（改内容是合法替换，钱该花）。
    #    挡的是环境事故式的批量消失（目录误移动/同步盘抽风）；小打小闹不拦。
    current_names = {name for name, _ in disk.values()}
    vanished = {
        meta["source"]
        for meta in stored.values()
        if meta["hash"] in to_remove_hashes
    } - current_names
    if len(vanished) >= GUARD_MIN_FILES and (
        len(vanished) / max(len(stored_hashes), 1) > DELETE_GUARD_RATIO
    ):
        raise RuntimeError(
            f"疑似笔记目录异常：{len(vanished)} 篇笔记同时消失"
            f"（占比 {len(vanished) / max(len(stored_hashes), 1):.0%}），已中止同步。"
            "请先确认笔记目录状态；若确属正常大清理，请处理后重试。"
        )

    # 3. 增：只为待增指纹切块 + embed（花钱的唯一环节）+ upsert
    for fp in will_add:
        name, text = disk[fp]
        chunks = chunk_text(text)
        vectors = embedder.embed(chunks)
        store.upsert(
            ids=[f"{fp}:{i}" for i in range(len(chunks))],
            chunks=chunks,
            vectors=vectors,
            metadatas=[{"source": name, "hash": fp} for _ in chunks],
        )

    # 4. 删（非原子：先增后删，两步间崩溃会短暂新旧块共存——下次同步自愈。
    #    刻意选「先增后删」而非「先删后增」：宁可短暂重复，不要窗口期内容缺失）
    store.delete(doomed_ids)

    return SyncReport(
        added=len(will_add),
        removed=len(to_remove_hashes) if incremental else 0,
        # 词袋重建路径每篇都被清掉重加，unchanged 归 0 保持账本自洽
        unchanged=len(unchanged) if incremental else 0,
    )
