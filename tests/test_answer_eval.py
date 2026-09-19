"""LLM-as-judge 评测链验收：无 mock 兜底纪律（解析件已内核化至 test_evalkit.py）。"""

from agent.core.gateway import FallbackLLM, RobustLLM
from agent.core.llm import get_llm


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
