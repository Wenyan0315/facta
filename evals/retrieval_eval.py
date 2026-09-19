"""检索质量评估：三路并评 + miss 归因（2026-09-19，混合检索决策的数据来源）。

三路：
- vector  向量检索（词袋 / BGE-M3 双实现对比）：
          评估参数（top_k=5，无闸门）测排序质量；
          生产参数（top_k=3 + embedder 默认闸门）测线上真实命中——
          与 search_notes 同参，miss 从这里数
- grep    关键词检索（极简模拟：query 内容词的块覆盖计数；与词袋同款
          tokenize 保证公平）。真实 rg 短语匹配只会更准——本路数据是
          grep 增量的下界
- union   两路并集（去重保序）——混合检索的形态模拟

miss 归因（生产参数下 vector 未命中、且题有答案）：
- grep 能命中 → b/c 类（精确词在场，向量排名或闸门没给上）＝混合检索的直接增量
- grep 也不中 → a 类（语义改写鸿沟）＝查询改写/换 embedder 的领地

语料 = data/notes（线上）+ evals/corpus（Context7 抓取 ~8 万字，
evals/fetch_corpus.py）。运行（项目根目录）：
    .venv/bin/python -m evals.retrieval_eval
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from dotenv import load_dotenv

from agent.evalkit import (
    attribute_miss,
    is_relevant,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from agent.knowledge.knowledge_base import (
    BagOfWordsEmbedder,
    Embedder,
    KnowledgeBase,
    chunk_text,
    get_embedder,
    tokenize,
)
from agent.knowledge.sync import sync_notes
from agent.paths import NOTES_DIR
from evals.dataset import CASES

TOP_K = 5        # 检索深度（评估参数）
PROD_TOP_K = 3   # 与生产 search_notes 同参

# grep 路停用词：中英文虚词（tokenize 中文逐字、英文整词，虚词覆盖会淹没区分度）
STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "of", "to", "in", "for", "and", "or",
    "what", "how", "why", "which", "does", "do", "can", "use", "with",
    "是", "什", "么", "怎", "为", "哪", "些", "吗", "呢", "的", "有",
    "能", "和", "与", "及", "我", "你", "他", "它", "在", "了", "个",
    "这", "那", "要", "会", "着", "就", "还", "被", "把", "给", "对",
})


def merged_corpus(tmp: Path) -> Path:
    """合并线上笔记 + 评估语料到临时目录（两源无同名文件；corpus 不进 data/notes）。"""
    corpus = Path(__file__).parent / "corpus"
    for src in (NOTES_DIR, corpus):
        for f in sorted(src.glob("*.md")):
            (tmp / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp


def build_kb(embedder: Embedder) -> KnowledgeBase:
    """搭评估知识库（answer_eval 也从这里拿）：合并语料 + 生产同款 sync 路径。

    临时目录退出即删无碍——块已进内存库（InMemory），不依赖源文件存活。
    """
    with tempfile.TemporaryDirectory() as tmp:
        merged = merged_corpus(Path(tmp))
        kb = KnowledgeBase(embedder)
        sync_notes(kb, merged)
    return kb


def make_grep(chunks: list[str]):
    """grep 路：预 tokenize 块集合，返回 (query, top_k) -> 块列表 的检索函数。

    极简词覆盖计数（BM25 的零阶近似）——不看 expected（防作弊），与向量路
    用同一 tokenize 保证公平。
    """
    chunk_tokens = [set(tokenize(c)) for c in chunks]

    def search(query: str, top_k: int) -> list[str]:
        terms = [t for t in tokenize(query) if t not in STOPWORDS]
        if not terms:
            return []
        scores = [sum(1 for t in terms if t in ct) for ct in chunk_tokens]
        ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
        return [chunks[i] for i in ranked[:top_k]]

    return search


def evaluate(embedder: Embedder, label: str, merged: Path, grep) -> None:
    """一路向量化实现 + grep + union 的并评与归因表。"""
    kb = KnowledgeBase(embedder)
    sync_notes(kb, merged)
    gate = embedder.default_min_score

    p_total = r_total = mrr_total = 0.0
    n_scored = 0
    miss_a = miss_bc = 0          # a 类（语义鸿沟）/ b,c 类（grep 可补救）
    absent_pass = absent_all = 0

    print(f"\n===== 向量化方式：{label}（闸门 = {gate}）=====")
    print(f"{'问题':<24}{'形态':<12}{'vec@5':>6}{'vec生产':>7}{'grep':>6}{'union':>6}   归因")
    print("-" * 78)

    for question, expected, form in CASES:
        if form == "absent":
            # 无答案题：检验生产闸门（top1 分应低于闸门）+ grep 假阳性观察
            top = kb.search(question, top_k=1, min_score=0.0)
            gate_ok = bool(top) and top[0].score < gate
            grep_top = grep(question, 1)
            absent_all += 1
            absent_pass += gate_ok
            print(
                f"{question:<24}{form:<12}{'—':>6}"
                f"{'拦住' if gate_ok else '漏过':>7}"
                f"{'(噪声)' if grep_top else '':>6}{'—':>6}"
            )
            continue

        # 评估参数（排序质量）+ 生产参数（真实命中）
        eval_hits = kb.search(question, top_k=TOP_K, min_score=0.0)
        prod_hits = kb.search(question, top_k=PROD_TOP_K)
        grep_hits = grep(question, PROD_TOP_K)
        union_hits = list(dict.fromkeys(
            [h.chunk for h in prod_hits] + grep_hits
        ))

        p = precision_at_k([h.chunk for h in eval_hits], expected, TOP_K)
        r = recall_at_k([h.chunk for h in eval_hits], expected, TOP_K)
        m = reciprocal_rank([h.chunk for h in eval_hits], expected)
        p_total += p
        r_total += r
        mrr_total += m
        n_scored += 1

        prod_ok = any(is_relevant(h.chunk, expected) for h in prod_hits)
        grep_ok = any(is_relevant(c, expected) for c in grep_hits)
        union_ok = any(is_relevant(c, expected) for c in union_hits)

        # 归因判定走 evalkit（内核化的意义：壳消费内核，API 被真实使用验证）
        kind = attribute_miss(prod_ok, grep_ok)
        if kind == "bc":
            attribution = "b/c：grep 可补救"
            miss_bc += 1
        elif kind == "a":
            attribution = "a：语义鸿沟"
            miss_a += 1
        else:
            attribution = ""

        print(
            f"{question:<24}{form:<12}{p:>6.2f}"
            f"{'命中' if prod_ok else 'miss':>7}"
            f"{'命中' if grep_ok else 'miss':>6}"
            f"{'命中' if union_ok else 'miss':>6}   {attribution}"
        )

    n = max(n_scored, 1)
    total_miss = miss_a + miss_bc
    print("-" * 78)
    print(
        f"P@5={p_total / n:.2f}  R@5={r_total / n:.2f}  MRR={mrr_total / n:.2f}  "
        f"｜ 生产 miss {total_miss}/{n_scored}"
        f"（a 类 {miss_a} / b,c 类 {miss_bc}）"
    )
    if total_miss:
        print(f"grep 补救率（b,c / 总 miss）= {miss_bc}/{total_miss} = {miss_bc / total_miss:.0%}")
    print(f"无答案闸门：{absent_pass}/{absent_all} 拦住（应全拦）")


def main() -> None:
    load_dotenv()
    with tempfile.TemporaryDirectory() as tmp:
        merged = merged_corpus(Path(tmp))
        chunks = []
        for f in sorted(merged.glob("*.md")):
            chunks.extend(chunk_text(f.read_text(encoding="utf-8")))
        print(f"语料：{len(list(merged.glob('*.md')))} 篇 / {len(chunks)} 块"
              f"（data/notes + evals/corpus）")
        grep = make_grep(chunks)

        evaluate(BagOfWordsEmbedder(), "词袋（教学基线）", merged, grep)
        try:
            bge = get_embedder("siliconflow")
        except Exception as exc:   # 无 key/无网：词袋结果仍有效，BGE 跳过
            print(f"\n[跳过 BGE-M3] {exc}")
            return
        evaluate(bge, "BGE-M3（真语义向量）", merged, grep)


if __name__ == "__main__":
    main()
