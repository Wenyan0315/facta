"""检索质量评估：precision@k / recall@k / MRR。

运行方式（项目根目录下）：
    .venv/bin/python -m evals.retrieval_eval

评估对象：KnowledgeBase.search 的排序质量（不带 min_score，纯看排序）。
会依次评估两种向量化实现并排对比：
    词袋（教学基线） vs BGE-M3（真语义向量，需 .env 里有 SILICONFLOW_API_KEY）

无答案问题单独检验：期望"最高分低于该 embedder 的及格线"（说明闸门能拦住）。
"""

from dotenv import load_dotenv

from agent.knowledge.knowledge_base import (
    BagOfWordsEmbedder,
    Embedder,
    KnowledgeBase,
    get_embedder,
)
from agent.knowledge.loader import load_notes
from evals.dataset import CASES

TOP_K = 5  # 检索深度：取前 5 条来评估


def is_relevant(chunk: str, expected: list[str]) -> bool:
    """chunk 是否命中了期望笔记（用标注的"指纹"文字做子串匹配）。"""
    return any(exp in chunk for exp in expected)


def precision_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    hits = sum(1 for c in retrieved[:k] if is_relevant(c, expected))
    return hits / k


def recall_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    if not expected:
        return 0.0  # 没有期望命中时无意义，调用侧会跳过
    hits = sum(1 for c in retrieved[:k] if is_relevant(c, expected))
    return hits / len(expected)


def reciprocal_rank(retrieved: list[str], expected: list[str]) -> float:
    """MRR 的单条版本：第一条命中的排名 r，得分 1/r；全不命中得 0。"""
    for rank, chunk in enumerate(retrieved, 1):
        if is_relevant(chunk, expected):
            return 1.0 / rank
    return 0.0


def build_kb(embedder: Embedder) -> KnowledgeBase:
    """搭一个和线上完全一样的知识库（向量化方式由 embedder 决定）。"""
    kb = KnowledgeBase(embedder)
    for note in load_notes(NOTES_DIR):
        kb.add_document(note)
    return kb


def evaluate(embedder: Embedder, label: str) -> None:
    """对一种向量化实现跑全部测试题，打印一张评估表。"""
    kb = build_kb(embedder)
    gate = embedder.default_min_score  # 及格线跟着实现走（量纲不同）

    p_total = r_total = mrr_total = 0.0
    n_scored = 0
    gate_pass = gate_all = 0

    print(f"\n===== 向量化方式：{label}（闸门 = {gate}）=====")
    print(f"{'问题':<16}{'P@5':>6}{'R@5':>6}{'MRR':>6}   说明")
    print("-" * 60)

    for question, expected in CASES:
        retrieved = [chunk for chunk, _ in kb.search(question, top_k=TOP_K, min_score=0.0)]

        if expected:
            p = precision_at_k(retrieved, expected, TOP_K)
            r = recall_at_k(retrieved, expected, TOP_K)
            m = reciprocal_rank(retrieved, expected)
            p_total += p
            r_total += r
            mrr_total += m
            n_scored += 1
            print(f"{question:<16}{p:>6.2f}{r:>6.2f}{m:>6.2f}   期望命中 {len(expected)} 条")
        else:
            # 无答案问题：检验最高分是否低于闸门（低了 = min_score 能拦住）
            top_score = kb.search(question, top_k=1, min_score=0.0)[0][1]
            ok = top_score < gate
            gate_pass += ok
            gate_all += 1
            verdict = "PASS" if ok else "FAIL"
            print(
                f"{question:<16}{'-':>6}{'-':>6}{'-':>6}   "
                f"{verdict} 最高分={top_score:.3f}（应<{gate}）"
            )

    print("-" * 60)
    print(
        f"有答案问题（{n_scored} 个）平均：P@{TOP_K}={p_total / n_scored:.3f}  "
        f"R@{TOP_K}={r_total / n_scored:.3f}  MRR={mrr_total / n_scored:.3f}"
    )
    print(f"无答案问题（{gate_all} 个）：{gate_pass}/{gate_all} 能被闸门 {gate} 拦住")


def main() -> None:
    # .env 里的 key 要先加载进环境变量（BGE 需要联网）
    load_dotenv()

    evaluate(BagOfWordsEmbedder(), "词袋 Bag-of-Words（教学基线）")
    try:
        evaluate(get_embedder("siliconflow"), "BGE-M3 语义向量（硅基流动）")
    except RuntimeError as e:
        print(f"\n[跳过 BGE-M3 对比] {e}")


if __name__ == "__main__":
    main()
