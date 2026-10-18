"""M7.5a 记账验收：UsageLedger 与 RobustLLM 的账目正确性（全离线）。

不变量：
- 成功调用入账 token/钱/耗时；失败计失败次数、原样抛出，不计成功
- 无价目（假模型）时成本为 0，账本照常工作
- 工厂 get_llm 返回的是穿壳后的网关（对外仍是 LLM 接口）
"""

import pytest

from facta.core.gateway import GatewayConfig, RobustLLM
from facta.core.llm import LLM, OpenAICompatibleLLM, get_llm
from facta.core.telemetry import CallLedger, UsageLedger, verify_calls
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


# ---- 080：prefix 缓存账目 ----


class _UsageChunk:
    """带 usage 的假 chunk，字段可选（模拟不同中转商的 usage 形状）。"""

    def __init__(self, hit=None, miss=None) -> None:
        self.usage = type(
            "Usage",
            (),
            {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_cache_hit_tokens": hit,
                "prompt_cache_miss_tokens": miss,
            },
        )()


def test_usage_of_collects_prefix_cache():
    usage = OpenAICompatibleLLM._usage_of(_UsageChunk(hit=30, miss=70))
    assert usage is not None
    assert usage["prompt_cache_hit_tokens"] == 30
    assert usage["prompt_cache_miss_tokens"] == 70
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50


def test_usage_of_prefix_cache_missing_is_none():
    """中转商不回 prefix 字段：getattr 兜底 None，账本 `or 0` 消化。"""
    usage = OpenAICompatibleLLM._usage_of(_UsageChunk())
    assert usage is not None
    assert usage["prompt_cache_hit_tokens"] is None
    assert usage["prompt_cache_miss_tokens"] is None


def test_record_llm_accumulates_prefix_cache():
    ledger = UsageLedger()
    ledger.record_llm(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_cache_hit_tokens": 60,
            "prompt_cache_miss_tokens": 40,
        }
    )
    # 无 prefix 字段的 usage（假模型）→ `or 0` 不炸也不误加
    ledger.record_llm({"prompt_tokens": 100, "completion_tokens": 10})
    assert ledger.prompt_cache_hit_tokens == 60
    assert ledger.prompt_cache_miss_tokens == 40
    assert ledger.tokens_in == 200


def test_bill_renders_prefix_rate():
    ledger = UsageLedger()
    ledger.record_llm(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_cache_hit_tokens": 75,
            "prompt_cache_miss_tokens": 25,
        }
    )
    bill = ledger.bill()
    assert "prefix" in bill
    assert "命中 75.0%" in bill


def test_bill_omits_prefix_when_no_cache_usage():
    """无任何 prefix 账目（假模型全程）→ 不渲染 prefix 行。"""
    ledger = UsageLedger()
    ledger.record_llm({"prompt_tokens": 100, "completion_tokens": 10})
    bill = ledger.bill()
    assert "prefix" not in bill


# ---- 108：成本记账交叉核验（明细账 + call_id 唯一归属 + 观测真源） ----


def test_verify_calls_matches_total_identity():
    """恒等式：总 attempt = llm_calls + llm_retries + llm_failures（混合场景）。"""
    ledger = UsageLedger()
    calls = CallLedger()
    # 成功 1 次（无重试）：1 attempt
    RobustLLM(UsageFakeLLM(), ledger, call_ledger=calls).generate(
        [Message(role="user", content="hi")]
    )
    # 重试成功：2 次重试 + 1 次落地 = 3 attempts
    flaky = FlakyLLM(fail_times=2)
    RobustLLM(flaky, ledger, GatewayConfig(retries=2, backoff=0), call_ledger=calls).generate(
        [Message(role="user", content="hi")]
    )
    # 失败（401 不可重试）：1 attempt
    with pytest.raises(_AuthError):
        RobustLLM(
            UnauthorizedLLM(), ledger, GatewayConfig(retries=2, backoff=0), call_ledger=calls
        ).generate([Message(role="user", content="hi")])

    v = verify_calls(calls, ledger)
    # llm_calls=2 + llm_retries=2 + llm_failures=1 = 5 == 明细 1+3+1
    assert v.expected_attempts == 5
    assert v.ledger_attempts == 5
    assert v.ledger_matches_total is True
    assert v.duplicates == []
    assert v.ok is True


def test_verify_calls_detects_duplicate():
    """双计篡改：同一 call_id 入账两次，内部账本自洽时依然被查出。"""
    ledger = UsageLedger()
    calls = CallLedger()
    calls.record("usage_fake", 1, None, "ok", call_id="c0001")
    calls.record("usage_fake", 1, None, "ok", call_id="c0001")  # 双计
    # 同步把累计尺也加两次，让 ledger_matches_total 仍成立——同源自比的盲区
    ledger.record_llm(None)
    ledger.record_llm(None)

    v = verify_calls(calls, ledger)
    assert v.ledger_matches_total is True    # 内部仍自洽
    assert v.duplicates == ["c0001"]         # 但双计被唯一归属抓到
    assert v.ok is False


def test_verify_calls_detects_undercount():
    """漏记：明细少记一笔（attempt 合计 < 累计尺推算），内部一致性不成立。"""
    ledger = UsageLedger()
    calls = CallLedger()
    calls.record("usage_fake", 1, None, "ok")
    ledger.record_llm(None)
    ledger.record_llm(None)  # 累计两次调用，明细只一笔

    v = verify_calls(calls, ledger)
    assert v.ledger_matches_total is False
    assert v.ok is False


def test_attempts_reflects_actual_inner_calls():
    """观测真源：明细 attempt 数 == 独立第二真源（spy 模型自己数的调用次数）。"""
    ledger = UsageLedger()
    calls = CallLedger()
    flaky = FlakyLLM(fail_times=2)
    RobustLLM(flaky, ledger, GatewayConfig(retries=2, backoff=0), call_ledger=calls).generate(
        [Message(role="user", content="hi")]
    )
    assert flaky.attempts == 3                # spy 独立计数：2 失败 + 1 成功
    assert calls.entries[0].attempts == 3     # 观测与 spy 一致（非同源自比）
    assert verify_calls(calls, ledger).ok is True


def test_failure_attempts_recorded_usage_none():
    """失败路径：attempt 照实入账、usage=None（无响应体不猜测），恒等式仍成立。"""
    ledger = UsageLedger()
    calls = CallLedger()
    with pytest.raises(RuntimeError):
        RobustLLM(
            BoomLLM(), ledger, GatewayConfig(retries=1, backoff=0), call_ledger=calls
        ).generate([Message(role="user", content="hi")])

    entry = calls.entries[0]
    assert entry.outcome == "error"
    assert entry.attempts == 2   # retries=1：第一次失败 + 第二次失败
    assert entry.usage is None
    v = verify_calls(calls, ledger)
    assert v.expected_attempts == 2   # llm_calls=0 + retries=1 + failures=1
    assert v.ok is True
