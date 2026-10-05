"""检索排序质量指标（evalkit 内核）：指纹判定 + P@k / R@k / MRR + miss 归因。

指纹协议：expected 里每条是资料中一段独特文字（"指纹"），子串出现在
检索结果里即命中。协议廉价但有效——不需要人工标注相关度等级，
且天然支持多指纹（任一命中即命中）。
"""

from __future__ import annotations


def is_relevant(chunk: str, expected: list[str]) -> bool:
    """chunk 是否命中了期望资料（指纹子串匹配，多指纹任一命中即命中）。"""
    return any(exp in chunk for exp in expected)


def precision_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    """前 k 条中命中占比。分母固定为 k——没检够也占位，惩罚深度浪费。"""
    hits = sum(1 for c in retrieved[:k] if is_relevant(c, expected))
    return hits / k


def recall_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    """期望指纹被覆盖的比例；无期望时返回 0.0（调用侧应跳过该题）。"""
    if not expected:
        return 0.0
    hits = sum(1 for c in retrieved[:k] if is_relevant(c, expected))
    return hits / len(expected)


def reciprocal_rank(retrieved: list[str], expected: list[str]) -> float:
    """MRR 单条版：第一条命中的排名 r 得 1/r；全不命中得 0。"""
    for rank, chunk in enumerate(retrieved, 1):
        if is_relevant(chunk, expected):
            return 1.0 / rank
    return 0.0


def staleness_at_k(stale_flags: list[bool], k: int) -> float:
    """前 k 个候选里「旧条」（已过期/被取代）的占比（ADR 075 影子指标）。

    stale_flags[i] 为 True 表示第 i 个候选是旧条。分母 = 实检候选数
    （min(k, len(stale_flags))）；无候选返回 0.0（与 recall_at_k 无期望返回
    0.0 同款宽容）——「占比」按实检数算，不惩罚没检够的深度（陈旧率问的是
    「检出来的有多旧」，不是「预算浪费了多少」）。
    """
    top = stale_flags[:k]
    if not top:
        return 0.0
    return sum(1 for f in top if f) / len(top)


def attribute_miss(prod_ok: bool, grep_ok: bool) -> str | None:
    """miss 归因（022 混合检索决策的判定件）。

    - None   向量路已命中，无 miss 可归因
    - "bc"   向量 miss 但关键词路能补（精确词在场）→ 加检索路的直接增量
    - "a"    两路全 miss → 语义鸿沟，查询改写/换 embedder 的领地
    """
    if prod_ok:
        return None
    return "bc" if grep_ok else "a"
