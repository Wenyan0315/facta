"""M7.5a 记账验收：UsageLedger 与 RobustLLM 的账目正确性（全离线）。

不变量：
- 成功调用入账 token/钱/耗时；失败计失败次数、原样抛出，不计成功
- 无价目（假模型）时成本为 0，账本照常工作
- 工厂 get_llm 返回的是穿壳后的网关（对外仍是 LLM 接口）
"""

import pytest

from agent.core.gateway import RobustLLM
from agent.core.llm import LLM, get_llm
from agent.core.telemetry import UsageLedger
from agent.core.types import Message


class UsageFakeLLM(LLM):
    """返回带 usage 的假模型，价目可配。"""

    name = "usage_fake"

    def __init__(self, pricing: dict | None = None) -> None:
        self.pricing = pricing

    def generate(self, messages, tools=None) -> Message:
        return Message(
            role="assistant",
            content="ok",
            usage={"prompt_tokens": 1000, "completion_tokens": 500},
        )


class BoomLLM(LLM):
    """永远失败的假模型：模拟网络中断。"""

    name = "boom"

    def generate(self, messages, tools=None) -> Message:
        raise RuntimeError("网络断了")


def test_ledger_records_llm_call_with_cost():
    ledger = UsageLedger()
    llm = RobustLLM(UsageFakeLLM(pricing={"in": 1.0, "out": 2.0}), ledger)
    llm.generate([Message(role="user", content="hi")])

    assert ledger.llm_calls == 1
    assert ledger.tokens_in == 1000
    assert ledger.tokens_out == 500
    # 1000/1e6*1 + 500/1e6*2 = 0.001 + 0.001
    assert ledger.llm_cost == pytest.approx(0.002)


def test_failure_recorded_and_rethrown():
    ledger = UsageLedger()
    llm = RobustLLM(BoomLLM(), ledger)
    with pytest.raises(RuntimeError, match="网络断了"):
        llm.generate([Message(role="user", content="hi")])
    assert ledger.llm_failures == 1
    assert ledger.llm_calls == 0  # 失败不计成功次数


def test_no_pricing_means_zero_cost():
    ledger = UsageLedger()
    llm = RobustLLM(UsageFakeLLM(), ledger)  # 无价目 = 假模型/未知价目
    llm.generate([Message(role="user", content="hi")])
    assert ledger.llm_cost == 0.0
    assert ledger.tokens_in == 1000  # token 照记，钱免单


def test_bill_renders():
    ledger = UsageLedger()
    ledger.record_llm({"prompt_tokens": 100, "completion_tokens": 10}, cost=0.0001)
    ledger.record_embed(1000, 0.001)
    bill = ledger.bill()
    assert "合计" in bill
    assert "¥0.0011" in bill


def test_get_llm_wraps_with_gateway():
    llm = get_llm("mock")
    assert isinstance(llm, RobustLLM)
    reply = llm.generate([Message(role="user", content="hi")])
    assert reply.role == "assistant"