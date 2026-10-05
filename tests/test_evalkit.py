"""evalkit 内核验收（2026-09-19，决策记录 024 内核化）。

纯函数行为锁定：指标（P@k/R@k/MRR）、指纹判定、miss 归因、裁判解析。
内核的验收标准比壳高一档——它是「随时可拿走」的组件，行为契约必须
钉死在测试里，拿走时测试跟着走。
"""

import pytest

from facta.evalkit import (
    attribute_miss,
    is_relevant,
    parse_judge_json,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    staleness_at_k,
)

# ---------- 指纹判定 ----------

def test_is_relevant_substring_match():
    assert is_relevant("PHP 是一种脚本语言", ["脚本语言"]) is True
    assert is_relevant("PHP 是一种脚本语言", ["向量数据库"]) is False


def test_is_relevant_multi_fingerprint_any():
    # 多指纹任一命中即命中（PHP 题标两条指纹的依据）
    assert is_relevant("Python 是通用的", ["PHP 是一种", "Python 是通用"]) is True


def test_is_relevant_empty_expected_never_matches():
    # 无答案题 expected 为空 → 永不命中（闸门语义的判定侧）
    assert is_relevant("任何内容", []) is False


# ---------- P@k / R@k / MRR ----------

def test_precision_at_k_penalizes_wasted_depth():
    # 分母固定 k：3 条里只中 1 → 1/3（不是 1/1——检索深度浪费要被罚）
    assert precision_at_k(["命中块", "噪声", "噪声"], ["命中块"], 3) == pytest.approx(1 / 3)


def test_precision_at_k_truncates_at_k():
    assert precision_at_k(["a", "b"], ["a"], 1) == pytest.approx(1.0)


def test_recall_at_k_multi_fingerprint_partial():
    # 两条指纹只覆盖一条 → 0.5
    assert recall_at_k(["甲指纹在场"], ["甲", "乙"], 3) == pytest.approx(0.5)
    assert recall_at_k(["甲指纹在场", "乙指纹在场", "噪声"], ["甲", "乙"], 3) == pytest.approx(1.0)


def test_recall_at_k_empty_expected_is_zero():
    assert recall_at_k(["x"], [], 3) == 0.0


def test_reciprocal_rank():
    assert reciprocal_rank(["a", "b"], ["b"]) == pytest.approx(0.5)
    assert reciprocal_rank(["a"], ["b"]) == 0.0


# ---------- 陈旧率（ADR 075 影子指标） ----------

def test_staleness_at_k_ratio_of_checked():
    # 分母＝实检候选数：3 条里 1 条旧 → 1/3
    assert staleness_at_k([True, False, False], 3) == pytest.approx(1 / 3)


def test_staleness_at_k_truncates_at_k():
    # 只看前 k 个：第 3 位是旧条但 k=1 时不进分母
    assert staleness_at_k([True, False, True], 1) == pytest.approx(1.0)


def test_staleness_at_k_denominator_is_checked_not_k():
    # 没检够 k 也按实检数算（不惩罚检索深度，与 recall_at_k 无期望=0 同款宽容）
    assert staleness_at_k([True], 3) == pytest.approx(1.0)


def test_staleness_at_k_empty_is_zero():
    assert staleness_at_k([], 3) == 0.0


def test_staleness_at_k_all_fresh_is_zero():
    assert staleness_at_k([False, False], 2) == 0.0


# ---------- miss 归因（022 判定件） ----------

def test_attribute_miss_three_states():
    assert attribute_miss(prod_ok=True, grep_ok=False) is None   # 向量命中：无 miss
    assert attribute_miss(prod_ok=False, grep_ok=True) == "bc"   # grep 可补
    assert attribute_miss(prod_ok=False, grep_ok=False) == "a"   # 语义鸿沟


# ---------- 裁判输出解析 ----------

def test_parse_plain_json():
    assert parse_judge_json('{"score": 5, "reason": "完全正确"}') == {
        "score": 5, "reason": "完全正确"
    }


def test_parse_json_wrapped_in_prose():
    # 裁判偶尔先解释再给 JSON——正则抠块必须救得回
    verdict = parse_judge_json('评估如下：\n{"score": 4, "reason": "基本正确"}\n仅供参考')
    assert verdict["score"] == 4


def test_parse_garbage_returns_none():
    assert parse_judge_json("没有 JSON 的输出") is None
    assert parse_judge_json('{"no_score": 1}') is None   # 有 JSON 但没 score
    assert parse_judge_json("") is None
