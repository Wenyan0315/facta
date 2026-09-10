"""知识库：RAG 的检索部分（切块 → 向量化 → 相似度 → 检索）。

向量化有同一接口（Embedder）的两种实现（和 llm.py 的 LLM 接口同一个套路）：
- BagOfWordsEmbedder：词袋（教学版）——只看字面重合，不联网
- SiliconFlowEmbedder：BGE-M3 真语义向量（工业路线）——懂语义，需联网 + key

两者接口一致：都是「文字 -> 向量 -> 余弦相似度」，
KnowledgeBase 不关心底层是哪种，构造时注入即可（依赖注入）。
"""

from __future__ import annotations

import math
import os
import re
from abc import ABC, abstractmethod
from collections import Counter

from agent.knowledge.loader import load_notes


def tokenize(text: str) -> list[str]:
    """把文字切成词：中文逐字、英文按单词，统一转小写。"""
    text = text.lower()
    # [a-z0-9]+ 匹配英文单词，[\u4e00-\u9fff] 匹配单个汉字
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text)


def chunk_text(text: str, size: int = 200, overlap: int = 50) -> list[str]:
    """把长文本按固定字符数切成若干块，去掉空白块。

    overlap 表示相邻两块重叠的字符数，默认 50。
    """
    step = size - overlap
    if step <= 0:
        raise ValueError("overlap 不能 >= size")
    chunks = [text[i : i + size] for i in range(0, len(text), step)]
    return [c for c in chunks if c.strip()]


def build_vocab(documents: list[str]) -> list[str]:
    """从所有文档里收集去重后的词表。"""
    vocab: set[str] = set()
    for doc in documents:
        vocab.update(tokenize(doc))
    return sorted(vocab)


def text_to_vector(text: str, vocab: list[str]) -> list[float]:
    """把文字映射成「词频向量」：每个词在向量里占一维，值是出现次数。"""
    counts = Counter(tokenize(text))
    return [float(counts.get(word, 0)) for word in vocab]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度：两个向量夹角的余弦，越接近 1 越相似。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class Embedder(ABC):
    """向量化接口：把一批文字变成一批向量（顺序一一对应）。

    和 llm.py 的 LLM 接口同一个设计套路：
    上层（KnowledgeBase）只认接口，不关心底层是数数还是神经网络。
    """

    # 该向量化方式的"及格线"：相似度低于它视为不相关。
    # 分数量纲由实现决定（词袋的 0.35 和 BGE 的 0.45 不是一回事），
    # 所以阈值跟着实现走，而不是散落在调用方硬编码。
    default_min_score: float = 0.0

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文字映射成一批向量，返回顺序与输入一致。"""

    def fit(self, corpus: list[str]) -> None:
        """（可选）先看一遍全部语料。

        词袋需要先建词表才能定维度；神经网络版不需要，默认空实现——
        用"接口里的默认方法"抹平两种实现的差异，上层代码就不用 if/else 了。
        """


class BagOfWordsEmbedder(Embedder):
    """词袋向量化（教学版）：字面重合 = 相关。"""

    # M3.5 基线调出来的经验值（见 evals/retrieval_eval.py 的历史结论）
    default_min_score = 0.35

    def __init__(self) -> None:
        self._vocab: list[str] = []

    def fit(self, corpus: list[str]) -> None:
        self._vocab = build_vocab(corpus)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [text_to_vector(t, self._vocab) for t in texts]


class OpenAICompatibleEmbedder(Embedder):
    """真语义向量的统一实现：一切 OpenAI 兼容的 embedding 服务都用这一个类。

    和 llm.py 的 OpenAICompatibleLLM 完全对称：
    类不含任何供应商名字，差异全部收进 EMBED_PROVIDERS 配置表——
    加一家供应商 = 配置表加一行，不再写新类。

    min_score 跟着"模型"走而不是跟着供应商走：
    换 embedding 模型，分数量纲就变，阈值必须重新校准。
    """

    def __init__(self, prefix: str, base_url: str, model: str, min_score: float) -> None:
        from openai import OpenAI  # 延迟导入：用词袋时不需要装/加载 openai

        api_key = os.environ.get(f"{prefix}_API_KEY", "")
        if not api_key:
            raise RuntimeError(f"缺少 {prefix}_API_KEY：请先在 .env 里配置")
        self._client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get(f"{prefix}_BASE_URL", base_url),
        )
        # 注意环境变量名是 _EMBED_MODEL 而不是 _MODEL：
        # 同一家供应商的聊天模型和 embedding 模型是两个独立开关，
        # 共用 _MODEL 会互相覆盖（想换聊天模型，结果把 embedding 也换了）
        self._model = os.environ.get(f"{prefix}_EMBED_MODEL", model)
        self.default_min_score = min_score  # 实例属性，盖掉接口的类属性 0.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._model, input=texts)
        # API 保证返回顺序与输入一致
        return [item.embedding for item in resp.data]


# embedding 供应商配置表：加一家 = 加一行（和 llm.py 的 PROVIDERS 对称）。
# 环境变量约定：{PREFIX}_API_KEY / {PREFIX}_BASE_URL / {PREFIX}_EMBED_MODEL
EMBED_PROVIDERS: dict[str, dict[str, str | float]] = {
    "siliconflow": {
        "prefix": "SILICONFLOW",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "BAAI/bge-m3",
        # 校准依据（2026-09-05 探针）：相关问题 0.645~0.784，垃圾问题最高 0.491，
        # 0.55 卡在沟中间。这个值属于 BGE-M3 这个模型——换模型必须重跑校准。
        "min_score": 0.55,
    },
}


def get_embedder(name: str = "bow") -> Embedder:
    """工厂函数：按名字返回向量化实现。

    "bow" -> 词袋（教学版，离线可用）
    "siliconflow"（及 EMBED_PROVIDERS 里任何一家）-> 真语义向量
    """
    if name == "bow":
        return BagOfWordsEmbedder()
    if name in EMBED_PROVIDERS:
        cfg = EMBED_PROVIDERS[name]
        return OpenAICompatibleEmbedder(
            cfg["prefix"], cfg["base_url"], cfg["model"], float(cfg["min_score"])
        )
    raise ValueError(f"未知的向量化方式: {name}")


class KnowledgeBase:
    """极简向量知识库：存文本块，按相似度检索。

    向量化方式由构造时注入的 embedder 决定：
        KnowledgeBase()                            # 词袋（教学版，离线可用）
        KnowledgeBase(get_embedder("siliconflow")) # BGE-M3 语义检索
    """

    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder = embedder or BagOfWordsEmbedder()
        self._chunks: list[str] = []
        self._vectors: list[list[float]] = []

    def add_document(self, text: str, chunk_size: int = 200, overlap: int = 50) -> None:
        """把一篇文档切块后加入知识库，可指定 chunk_size 与 overlap。"""
        for chunk in chunk_text(text, chunk_size, overlap):
            self._chunks.append(chunk)
        # 词袋必须全量重建（词表变了所有向量维度都变）；
        # 神经网络版理论上可以只算新增块（M7 向量库做增量+持久化，这里从简）。
        self._rebuild()

    def _rebuild(self) -> None:
        self._embedder.fit(self._chunks)
        self._vectors = self._embedder.embed(self._chunks)

    def search(
        self, query: str, top_k: int = 3, min_score: float | None = None
    ) -> list[tuple[str, float]]:
        """返回与问题最相似的 top_k 个文本块，以及各自相似度。

        min_score：低于该分数的结果直接丢弃（防止 top_k 硬凑垃圾结果）。
        不传（None）时用当前 embedder 的默认及格线——分数量纲跟着实现走。
        """
        if min_score is None:
            min_score = self._embedder.default_min_score
        qvec = self._embedder.embed([query])[0]
        scored = [
            (chunk, cosine_similarity(qvec, vec))
            for chunk, vec in zip(self._chunks, self._vectors)
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [item for item in scored[:top_k] if item[1] >= min_score]


def demo() -> None:
    from pathlib import Path   # P1-3：demo 离线脚本自带路径，不走 ctx

    kb = KnowledgeBase()
    for note in load_notes(Path("data/notes")):
        kb.add_document(note)

    question = "PHP是什么？"
    print(f"问题：{question}\n")

    results = kb.search(question, top_k=3)
    print("检索到的最相关片段：")
    for i, (chunk, score) in enumerate(results, 1):
        print(f"  [{i}] score={score:.3f}  {chunk}")

    # 组装「增强后的 prompt」：把检索到的片段拼进上下文
    context = "\n".join(f"- {chunk}" for chunk, _ in results)
    print("\n===== 发送给模型的『增强 prompt』(节选) =====")
    print("system: 你是学习助手，请只依据下面资料回答。")
    print("资料：")
    print(context)
    print(f"user: {question}")


if __name__ == "__main__":
    demo()