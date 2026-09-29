"""图谱消融实验（2026-09-25，roadmap 刺 #3 / 论文⑥ When to Use Graphs in RAG）。

问题：图谱对检索到底有没有增量？设计书预写判定规则——
「evalkit 场景按 L1/L2/L3 分级，预期图谱 L1 输、L2+ 赢；若 L2 也不赢，
图谱降级为按需工具」。本脚本产出分级的命中率对照表，数据说了算。

两组对照（同库同 embedder，唯一变量 = 是否加图谱导航块）：
- baseline  纯向量检索：kb.search(query, top_k=3)——生产 search_notes 同参
- +graph    baseline + 图谱导航块：query 里的实体名锚定图节点 → 实体 BFS
            2 跳扩展 → 沿途边的 source_note 就是「相关笔记」（去重限量 4 篇），
            每篇取词袋最相关 1 块追加进上下文

为什么实体要 BFS 到 2 跳：边的 source_note 是「抽取来源」——1 跳邻居边
往往只回到锚点笔记自己（知识图谱→Embedding 抽自知识图谱.md），2 跳才
拿到邻笔记（向量数据库.md 的边）。这是图数据形态决定的，不是拍脑袋。

分级（判定协议与 dataset.py 同款：expected 为指纹子串，任一出现即命中；
加新笔记/改图后必须复查本文件——标注与语料、图谱三方同步）：
- L1 单篇事实题：答案就在一篇里。预期图谱打平且白付块数成本（噪声）
  ——4 题覆盖三种形态：孤岛锚点（RAG/PHP/余弦相似度，零增量零成本）、
  枢纽锚点（Agent，块数膨胀实测）
- L2 跨笔记题（1-2 跳）：答案要拼两篇。query 用词刻意避开目标笔记字面
  （语义鸿沟），只能靠图谱导航走过去——图谱的主场
- L3 多跳题（3 跳实体链）：path 查询的领地

另设定性组（不进命中率统计，打印对照即可）：
- 孤岛全貌题：overview.islands——向量检索给不出全集（图谱独占价值）
- 不连通题：path(政策, Embedding)=None——图谱如实报「无关联」，
  向量检索只会硬凑块（防幻觉价值）

运行（项目根目录）：
    .venv/bin/python -m evals.graph_ablation
"""

from __future__ import annotations

from dotenv import load_dotenv

from evals.retrieval_eval import STOPWORDS
from facta.evalkit import is_relevant
from facta.knowledge.graph import GraphStore
from facta.knowledge.knowledge_base import (
    BagOfWordsEmbedder,
    Embedder,
    KnowledgeBase,
    chunk_text,
    get_embedder,
    tokenize,
)
from facta.knowledge.sync import sync_notes
from facta.paths import GRAPH_PATH, NOTES_DIR

TOP_K = 3               # 与生产 search_notes 同参
GRAPH_HOPS = 2          # 实体 BFS 扩展层数（见模块 docstring）
MAX_AUGMENT_NOTES = 4   # 追加笔记上限：上下文成本的硬闸

# (问题, 指纹列表, 分级)。指纹全部出自 data/notes 原文，已逐句核对。
CASES: list[tuple[str, list[str], str]] = [
    # ---- L1 单篇事实：图谱应无增量 ----
    ("RAG 是什么", ["RAG 是检索增强生成"], "L1"),                      # 孤岛锚点
    ("余弦相似度是什么", ["余弦相似度用来衡量"], "L1"),                # 孤岛锚点
    ("PHP 是什么语言", ["PHP 是一种脚本语言", "PHP是最好的语言"], "L1"),  # 孤岛锚点
    ("什么是 Agent", ["Agent 是能自主调用工具"], "L1"),                # 枢纽锚点（噪声）
    # ---- L2 跨笔记（1-2 跳）：图谱主场 ----
    # 答案=向量数据库.md+Embedding.md；知识图谱.md 里没有这两句指纹
    ("知识图谱靠哪两样技术撑着", ["向量数据库专门用来存储", "Embedding 是把文字转换成向量"], "L2"),
    # query 无「向量/数据库」字面；答案=向量数据库.md（Embedding→实体向量→向量库 2 跳）
    ("Embedding 的产出物最终存到哪儿", ["向量数据库专门用来存储"], "L2"),
    # 答案=三级信息政策.md+一致性检查三篇（Agent→政策 1 跳，政策→三篇 2 跳）
    ("Agent 要受什么约束，谁来检查", ["三级信息政策", "TOOL-ALLOWLIST"], "L2"),
    # 答案=政策自动复查落地手册.md（能力→落地手册 1 跳直达）
    ("能力清单和白名单对不上时哪条规则会红", ["TOOL-ALLOWLIST", "IO-BOUNDARY"], "L2"),
    # ---- L3 多跳（3 跳实体链）----
    # 答案=Agent仿制.md 四层拆解；Agent→工具→Agent骨架 2 跳拿到该笔记
    ("Agent 的决策核心和语言能力分别来自哪两层", ["底层 LLM", "规划 + 工具调用的循环"], "L3"),
    # Chroma↔向量库↔知识图谱；Chroma 字面就在向量数据库.md——baseline 可能蒙对的对照样本
    ("Chroma 跟知识图谱怎么扯上关系", ["向量数据库专门用来存储"], "L3"),
]


def best_chunk(chunks: list[str], query: str) -> str | None:
    """一篇笔记内与 query 词袋覆盖最高的块（grep 路同款零阶近似，去停用词）。"""
    terms = [t for t in tokenize(query) if t not in STOPWORDS]
    if not terms:
        return chunks[0] if chunks else None
    scored = sorted(
        chunks,
        key=lambda c: sum(1 for t in terms if t in tokenize(c)),
        reverse=True,
    )
    return scored[0] if scored else None


def evaluate(
    embedder: Embedder, label: str, store: GraphStore, note_chunks: dict[str, list[str]]
) -> None:
    """一组 embedder 下的 baseline vs +graph 分级对照表 + 预写规则判定。"""
    kb = KnowledgeBase(embedder)
    sync_notes(kb, NOTES_DIR)   # 语料 = data/notes only：生产同构（图谱也只覆盖它）

    print(f"\n===== 向量化方式：{label} =====")
    print(f"{'问题':<22}{'级':>3}{'baseline':>9}{'+graph':>8}{'块数':>9}   追加的相关笔记")
    print("-" * 84)

    # rows: (level, base_ok, graph_ok, n_blocks)
    rows: list[tuple[str, bool, bool, int]] = []
    for question, expected, level in CASES:
        base_hits = kb.search(question, top_k=TOP_K)
        base_chunks = [h.chunk for h in base_hits]

        notes = store.related_notes(question, hops=GRAPH_HOPS, limit=MAX_AUGMENT_NOTES)
        aug = [
            c for n in notes
            if (c := best_chunk(note_chunks.get(n, []), question)) is not None
        ]
        graph_chunks = list(dict.fromkeys(base_chunks + aug))   # 去重保序

        base_ok = any(is_relevant(c, expected) for c in base_chunks)
        graph_ok = any(is_relevant(c, expected) for c in graph_chunks)
        rows.append((level, base_ok, graph_ok, len(graph_chunks)))
        print(
            f"{question:<22}{level:>3}"
            f"{'命中' if base_ok else 'miss':>9}"
            f"{'命中' if graph_ok else 'miss':>8}"
            f"{len(base_chunks):>4}→{len(graph_chunks):<3}   {', '.join(notes)}"
        )

    print("-" * 84)
    for lv in ("L1", "L2", "L3"):
        sub = [r for r in rows if r[0] == lv]
        b = sum(1 for r in sub if r[1])
        g = sum(1 for r in sub if r[2])
        avg = sum(r[3] for r in sub) / max(len(sub), 1)
        print(f"{lv}: baseline {b}/{len(sub)}  +graph {g}/{len(sub)}  （平均块数 {avg:.1f}）")

    # 预写规则（roadmap 刺 #3）：判定只看 L2——L1 的噪声成本是已知代价
    l2 = [r for r in rows if r[0] == "L2"]
    l2_b, l2_g = sum(1 for r in l2 if r[1]), sum(1 for r in l2 if r[2])
    print("预写规则判定：", end="")
    if l2_g > l2_b:
        print(f"L2 图谱 {l2_g} > 纯向量 {l2_b} → 图谱有检索增量，值得继续投入（检索分诊可立项）")
    else:
        print(f"L2 图谱 {l2_g} ≤ 纯向量 {l2_b} → 图谱降级为按需工具（overview/path 面板）")

    # ---- 定性组：图谱独占能力，向量检索结构性给不出 ----
    ov = store.overview()
    top = kb.search("我笔记里哪些概念还是孤岛", top_k=1, min_score=0.0)
    print(f"\n[定性] 孤岛题：overview 给出 {len(ov['islands'])} 个孤岛 {ov['islands'][:5]}…")
    print(f"       baseline top1 硬凑：{top[0].chunk[:40] if top else '（无块）'}…")
    p = store.path("政策", "Embedding")
    top2 = kb.search("政策和 Embedding 什么关系", top_k=1, min_score=0.0)
    print(f"[定性] 不连通题：path(政策, Embedding) = {p}（图谱如实报告）")
    print(f"       baseline top1 硬凑：{top2[0].chunk[:40] if top2 else '（无块）'}…")


def main() -> None:
    load_dotenv()
    store = GraphStore.load(GRAPH_PATH)
    note_chunks = {
        f.name: chunk_text(f.read_text(encoding="utf-8"))
        for f in sorted(NOTES_DIR.glob("*.md"))
    }
    print(f"语料：{len(note_chunks)} 篇笔记（data/notes only）｜"
          f"图谱：{len(store.nodes)} 节点 / {len(store.to_dict()['edges'])} 边")

    evaluate(BagOfWordsEmbedder(), "词袋（教学基线）", store, note_chunks)
    try:
        bge = get_embedder("siliconflow")
    except Exception as exc:   # 无 key/无网：词袋结果仍有效，BGE 跳过
        print(f"\n[跳过 BGE-M3] {exc}")
        return
    evaluate(bge, "BGE-M3（真语义向量）", store, note_chunks)


if __name__ == "__main__":
    main()
