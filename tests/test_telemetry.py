"""M7.5a 记账验收：UsageLedger 与 RobustLLM 的账目正确性（全离线）。

不变量：
- 成功调用入账 token/钱/耗时；失败计失败次数、原样抛出，不计成功
- 无价目（假模型）时成本为 0，账本照常工作
- 工厂 get_llm 返回的是穿壳后的网关（对外仍是 LLM 接口）
"""

import pytest

from facta.core.gateway import GatewayConfig, RobustLLM
from facta.core.llm import LLM, get_llm
from facta.core.telemetry import UsageLedger
from facta.core.types import Message


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


class FlakyLLM(LLM):
    """先失败 N 次再成功的假模型：模拟网络抖动。"""

    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        self._fail_left = fail_times
        self.attempts = 0

    def generate(self, messages, tools=None) -> Message:
        self.attempts += 1
        if self._fail_left > 0:
            self._fail_left -= 1
            raise ConnectionError("网络抖动")  # 无 status_code → 可重试
        return Message(
            role="assistant",
            content="终于成功",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


class _AuthError(Exception):
    """401 语义：key 无效——重试一万次也不会好。"""

    status_code = 401


class UnauthorizedLLM(LLM):
    name = "unauthorized"

    def generate(self, messages, tools=None) -> Message:
        raise _AuthError("key 无效")


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
    llm = RobustLLM(BoomLLM(), ledger, GatewayConfig(backoff=0))  # 关退避，测试不睡觉
    with pytest.raises(RuntimeError, match="网络断了"):
        llm.generate([Message(role="user", content="hi")])
    assert ledger.llm_failures == 1
    assert ledger.llm_calls == 0  # 失败不计成功次数


def test_retry_then_success():
    ledger = UsageLedger()
    flaky = FlakyLLM(fail_times=2)
    llm = RobustLLM(flaky, ledger, GatewayConfig(retries=2, backoff=0))
    reply = llm.generate([Message(role="user", content="hi")])

    assert reply.content == "终于成功"
    assert flaky.attempts == 3  # 2 次失败 + 1 次成功
    assert ledger.llm_retries == 2
    assert ledger.llm_failures == 0
    assert ledger.llm_calls == 1  # 最终成功只算一次


def test_non_retriable_fails_fast():
    ledger = UsageLedger()
    llm = RobustLLM(UnauthorizedLLM(), ledger, GatewayConfig(retries=2, backoff=0))
    with pytest.raises(_AuthError, match="key 无效"):
        llm.generate([Message(role="user", content="hi")])
    assert ledger.llm_retries == 0  # 401 不值得重试
    assert ledger.llm_failures == 1


def test_retry_exhausted_records_failure():
    ledger = UsageLedger()
    llm = RobustLLM(BoomLLM(), ledger, GatewayConfig(retries=1, backoff=0))
    with pytest.raises(RuntimeError, match="网络断了"):
        llm.generate([Message(role="user", content="hi")])
    assert ledger.llm_retries == 1
    assert ledger.llm_failures == 1


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
