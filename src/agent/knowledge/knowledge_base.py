"""知识库：RAG 的检索部分（切块 → 向量化 → 相似度 → 检索）。

向量化有同一接口（Embedder）的两种实现（和 llm.py 的 LLM 接口同一个套路）：
- BagOfWordsEmbedder：词袋（教学版）——只看字面重合，不联网
- SiliconFlowEmbedder：BGE-M3 真语义向量（工业路线）——懂语义，需联网 + key

两者接口一致：都是「文字 -> 向量 -> 余弦相似度」，
KnowledgeBase 不关心底层是哪种，构造时注入即可（依赖注入）。
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections import Counter

from agent.knowledge.vector_store import InMemoryVectorStore, VectorStore


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


class Embedder(ABC):
    """向量化接口：把一批文字变成一批向量（顺序一一对应）。

    和 llm.py 的 LLM 接口同一个设计套路：
    上层（KnowledgeBase）只认接口，不关心底层是数数还是神经网络。
    """

    # 该向量化方式的"及格线"：相似度低于它视为不相关。
    # 分数量纲由实现决定（词袋的 0.35 和 BGE 的 0.45 不是一回事），
    # 所以阈值跟着实现走，而不是散落在调用方硬编码。
    default_min_score: float = 0.0

    # M7：该向量化方式支不支持「增量更新」。词袋向量维度 = 词表长度，
    # 词表一变所有旧向量作废（不支持）；神经网络版固定维度，可以增量。
    supports_incremental: bool = False

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

    supports_incremental = True   # 固定维度，新块可单独 embed（M7 增量同步的前提）

    def __init__(
        self,
        prefix: str,
        base_url: str,
        model: str,
        min_score: float,
        price: float = 0.0,
        ledger=None,
    ) -> None:
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
        # M7.5：embedding 也进同一本账（可选注入；evals/测试不传即不计）
        self._price = price
        self._ledger = ledger

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._model, input=texts)
        if self._ledger is not None:
            # 中转商不返回 usage 时按文本条数兜底估算（宁可少记不可炸）
            tokens = resp.usage.prompt_tokens if resp.usage else len(texts)
            self._ledger.record_embed(tokens, tokens / 1e6 * self._price)
        # API 保证返回顺序与输入一致
        return [item.embedding for item in resp.data]


# embedding 供应商配置表：加一家 = 加一行（和 llm.py 的 PROVIDERS 对称）。
# 环境变量约定：{PREFIX}_API_KEY / {PREFIX}_BASE_URL / {PREFIX}_EMBED_MODEL
# price：M7.5 记账价目（¥/百万 tokens）——BGE-M3 硅基流动当前免费额度，示例值
EMBED_PROVIDERS: dict[str, dict[str, str | float]] = {
    "siliconflow": {
        "prefix": "SILICONFLOW",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "BAAI/bge-m3",
        # 校准依据（2026-09-05 探针）：相关问题 0.645~0.784，垃圾问题最高 0.491，
        # 0.55 卡在沟中间。这个值属于 BGE-M3 这个模型——换模型必须重跑校准。
        "min_score": 0.55,
        "price": 0.0,
    },
}


def get_embedder(name: str = "bow", ledger=None) -> Embedder:
    """工厂函数：按名字返回向量化实现。

    "bow" -> 词袋（教学版，离线可用）
    "siliconflow"（及 EMBED_PROVIDERS 里任何一家）-> 真语义向量
    ledger：M7.5 成本账本（可选）——真 embedder 每次调用把 token 记账
    """
    if name == "bow":
        return BagOfWordsEmbedder()
    if name in EMBED_PROVIDERS:
        cfg = EMBED_PROVIDERS[name]
        return OpenAICompatibleEmbedder(
            cfg["prefix"],
            cfg["base_url"],
            cfg["model"],
            float(cfg["min_score"]),
            float(cfg.get("price", 0.0)),
            ledger,
        )
    raise ValueError(f"未知的向量化方式: {name}")


class KnowledgeBase:
    """知识库（M7 变身）：检索语义的总管，把「向量住哪」委托给 VectorStore。

    两个注入件，跟 __main__ 组装层的老规矩一致：
        KnowledgeBase()                 # 词袋 + 内存库（教学版，离线可用）
        KnowledgeBase(get_embedder("siliconflow"), ChromaVectorStore(path))
    索引维护走 sync.sync_notes(kb, notes_dir)（增量同步，M7 心脏）。
    search 接口签名与 M7 前完全一致——工具层、evals 无感知。
    """

    def __init__(
        self,
        embedder: Embedder | None = None,
        store: VectorStore | None = None,
    ) -> None:
        self.embedder = embedder or BagOfWordsEmbedder()
        self.store = store or InMemoryVectorStore()

    def search(
        self, query: str, top_k: int = 3, min_score: float | None = None
    ) -> list[tuple[str, float]]:
        """返回与问题最相似的 top_k 个文本块及相似度（越大越像）。

        min_score：低于该分数的结果丢弃（防止 top_k 硬凑垃圾结果）。
        不传时用 embedder 的默认及格线——分数量纲跟着实现走。
        """
        if min_score is None:
            min_score = self.embedder.default_min_score
        qvec = self.embedder.embed([query])[0]
        results = self.store.query(qvec, top_k)
        return [(chunk, score) for chunk, score in results if score >= min_score]


def demo() -> None:
    from agent.knowledge.sync import sync_notes  # 函数内导入：sync 依赖本模块，避免循环

    kb = KnowledgeBase()
    report = sync_notes(kb, NOTES_DIR)
    print(f"知识库同步：新增 {report.added} / 删除 {report.removed} / 不变 {report.unchanged}")

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