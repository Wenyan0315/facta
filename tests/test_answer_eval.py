"""LLM-as-judge 离线验收：裁判输出解析的健壮性 + 评测链无 mock 兜底。"""

from agent.core.gateway import FallbackLLM, RobustLLM
from agent.core.llm import get_llm
from evals.answer_eval import parse_judge_json


def test_parse_plain_json():
    verdict = parse_judge_json('{"score": 5, "reason": "完全正确"}')
    assert verdict == {"score": 5, "reason": "完全正确"}


def test_parse_json_wrapped_in_prose():
    # 裁判偶尔会先解释再给 JSON——正则抠块必须救得回
    verdict = parse_judge_json('评估如下：\n{"score": 4, "reason": "基本正确"}\n仅供参考')
    assert verdict["score"] == 4


def test_parse_garbage_returns_none():
    assert parse_judge_json("没有 JSON 的输出") is None
    assert parse_judge_json('{"no_score": 1}') is None
    assert parse_judge_json("") is None


def test_get_llm_without_mock_fallback_single_candidate(monkeypatch):
    # 只有主模型 key：无备用无兜底 → 直接返回单候选 RobustLLM
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    llm = get_llm("deepseek", with_mock_fallback=False)
    assert isinstance(llm, RobustLLM)


def test_get_llm_without_mock_fallback_chain_excludes_mock(monkeypatch):
    # 两个 key 都在：链 = 主+备，最后一名绝不能是 mock
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
    llm = get_llm("deepseek", with_mock_fallback=False)
    assert isinstance(llm, FallbackLLM)
    names = [c.name for c in llm.candidates]
    assert names[-1] != "mock"  # 评测失败必须大声，不被 mock 顶替污染分数
