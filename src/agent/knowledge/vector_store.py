"""向量存取层（M7）：把「向量住哪、怎么查」从 KnowledgeBase 拆出来，成为独立接口。

三种能力分工（对照项目里的快递面单类比）：
- query   相似度检索——唯一算距离的入口，search 用它
- get_all 全量面单（id → metadata）——同步做差集比对用，不碰相似度计算
- 维护    upsert / delete / clear / count

两个实现（跟 Embedder/LLM 完全同一套路：接口 + 构造注入）：
- InMemoryVectorStore  教学版：纯内存字典 + 暴力余弦，测试/evals/词袋用，不依赖 chromadb
- ChromaVectorStore    工业版：落盘磁盘目录，按 id 增量增删，重启不丢、不重算

分数契约（写在接口上，哪个实现违背、测试就抓哪个）：
query 返回的分数必须是「余弦相似度」，越大越像。
InMemory 原生就是余弦；Chroma 的 cosine 空间返回的是「距离」（= 1 - 余弦相似度），
所以要在实现里换算。换算封装在实现内部——上层永远只看到「越大越像」这一个语义。
"""

import math
from abc import ABC, abstractmethod
from pathlib import Path


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度：两个向量夹角的余弦，越接近 1 越相似。

    从 knowledge_base.py 搬过来：M7 后「算距离」变成 store 层的活，
    数学函数跟着它唯一的使用者走。
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorStore(ABC):
    """向量存取接口：上层（KnowledgeBase / 同步器）只认它，不关心底层。"""

    @abstractmethod
    def upsert(
        self,
        ids: list[str],
        chunks: list[str],
        vectors: list[list[float]],
        metadatas: list[dict],
    ) -> None:
        """写入或覆盖：id 相同即覆盖（幂等），四个列表一一对应。"""

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """按 id 删除，id 不存在则静默跳过。"""

    @abstractmethod
    def query(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        """返回与查询向量最像的 top_k 个 (块文本, 相似度)，相似度越大越像。"""

    @abstractmethod
    def get_all(self) -> dict[str, dict]:
        """拉全库面单：id → metadata。同步差集比对用，不做相似度计算。"""

    @abstractmethod
    def count(self) -> int:
        """库里一共有几条（块数，不是文件数）。"""

    @abstractmethod
    def clear(self) -> None:
        """清空整个库。

        给「词袋退化路径」用：词袋向量维度 = 词表长度，词表一变所有
        旧向量作废，只能整库重来（见 sync.py 的增量能力分支）。
        """


class InMemoryVectorStore(VectorStore):
    """教学版：三张字典存 id → 文本 / 向量 / 面单，查询暴力遍历算余弦。

    测试、evals、词袋模式用它运行，不需要装 chromadb。
    """

    def __init__(self) -> None:
        self._chunks: dict[str, str] = {}
        self._vectors: dict[str, list[float]] = {}
        self._metas: dict[str, dict] = {}

    def upsert(self, ids, chunks, vectors, metadatas):
        for id_, chunk, vec, meta in zip(ids, chunks, vectors, metadatas):
            self._chunks[id_] = chunk
            self._vectors[id_] = vec
            self._metas[id_] = dict(meta)

    def delete(self, ids):
        for id_ in ids:
            self._chunks.pop(id_, None)
            self._vectors.pop(id_, None)
            self._metas.pop(id_, None)

    def query(self, vector, top_k):
        scored = [
            (id_, cosine_similarity(vector, vec))
            for id_, vec in self._vectors.items()
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [(self._chunks[i], s) for i, s in scored[:top_k]]

    def get_all(self):
        return {id_: dict(meta) for id_, meta in self._metas.items()}

    def count(self):
        return len(self._chunks)

    def clear(self):
        self._chunks.clear()
        self._vectors.clear()
        self._metas.clear()


class ChromaVectorStore(VectorStore):
    """工业版：Chroma 落盘，向量和面单都住磁盘，重启不丢、不重算。

    两个实现细节（都是有教训的坑，别动）：
    1. hnsw:space=cosine —— Chroma 默认 L2 距离（越小越像），跟我们的
       「相似度越大越像」契约相反、量纲也不同；不显式指定，min_score
       校准值（BGE 0.55）就全部作废。
    2. 延迟导入 chromadb —— 可选依赖（pyproject 的 rag 组），只用
       InMemory 的人不该被要求装它。和 OpenAICompatibleEmbedder
       延迟 import openai 是同一招。
    """

    def __init__(self, path: Path, collection: str = "notes") -> None:
        from chromadb import PersistentClient
        from chromadb.config import Settings

        self._client = PersistentClient(
            path=str(path),
            settings=Settings(anonymized_telemetry=False),  # 本地学习项目不上报统计
        )
        self._col = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, ids, chunks, vectors, metadatas):
        self._col.upsert(
            ids=ids,
            documents=chunks,
            embeddings=vectors,
            metadatas=metadatas,
        )

    def delete(self, ids):
        if ids:
            self._col.delete(ids=ids)

    def query(self, vector, top_k):
        n = min(top_k, self._col.count())
        if n == 0:
            return []
        res = self._col.query(
            query_embeddings=[vector],
            n_results=n,
            include=["documents", "distances"],
        )
        docs = res["documents"][0] or []
        dists = res["distances"][0] or []
        # cosine 空间下 Chroma 吐的是「距离」= 1 - 余弦相似度，换算回相似度
        return [(doc, 1.0 - dist) for doc, dist in zip(docs, dists)]

    def get_all(self):
        res = self._col.get(include=["metadatas"])
        return {id_: meta or {} for id_, meta in zip(res["ids"], res["metadatas"])}

    def count(self):
        return self._col.count()

    def clear(self):
        ids = self._col.get()["ids"]
        if ids:
            self._col.delete(ids=ids)