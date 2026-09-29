"""M7.5d 验收：熔断三态 + 降级链 + 主循环优雅兜底（F 契约逐条钉死）。"""

import pytest

from facta.core.gateway import (
    CircuitOpenError,
    FallbackLLM,
    GatewayConfig,
    RobustLLM,
)
from facta.core.llm import LLM, LLMUnavailableError
from facta.core.telemetry import UsageLedger
from facta.core.types import Message


class SwitchableLLM(LLM):
    """手动切换成功/失败的假模型，记录被调用次数。"""

    name = "switchable"

    def __init__(self, fail: bool = True) -> None:
        self.attempts = 0
        self.fail = fail

    def generate(self, messages, tools=None) -> Message:
        self.attempts += 1
        if self.fail:
            raise ConnectionError("网络抖动")
        return Message(role="assistant", content="恢复啦")


class EchoFakeLLM(LLM):
    name = "echo_fake"

    def generate(self, messages, tools=None) -> Message:
        return Message(role="assistant", content=f"echo:{messages[-1].content}")


def _ask(llm, text="你好"):
    return llm.generate([Message(role="user", content=text)])


def test_breaker_opens_and_fails_fast():
    inner = SwitchableLLM(fail=True)
    llm = RobustLLM(
        inner, UsageLedger(), GatewayConfig(retries=0, fail_threshold=3, cooldown_seconds=60)
    )
    for _ in range(3):
        with pytest.raises(ConnectionError):
            _ask(llm)
    assert inner.attempts == 3

    # 第 4 次：熔断已打开 → 直接快速失败，inner 一次都没被叫（不再撞墙）
    with pytest.raises(CircuitOpenError):
        _ask(llm)
    assert inner.attempts == 3


def test_breaker_half_open_recovers():
    inner = SwitchableLLM(fail=True)
    llm = RobustLLM(
        inner, UsageLedger(), GatewayConfig(retries=0, fail_threshold=2, cooldown_seconds=0)
    )
    for _ in range(2):
        with pytest.raises(ConnectionError):
            _ask(llm)
    inner.fail = False  # 网络恢复

    reply = _ask(llm)  # 冷却已过 → 半开放行试探 → 成功 → 回路关闭
    assert reply.content == "恢复啦"
    assert _ask(llm).content == "恢复啦"  # 后续正常通行


def test_breaker_half_open_failure_reopens(monkeypatch):
    # 假时钟：熔断冷却依赖 time.monotonic，测试里由它控制时间前进
    clock = [100.0]
    monkeypatch.setattr("facta.core.gateway.time.monotonic", lambda: clock[0])

    inner = SwitchableLLM(fail=True)
    llm = RobustLLM(
        inner, UsageLedger(), GatewayConfig(retries=0, fail_threshold=2, cooldown_seconds=60)
    )
    for _ in range(2):
        with pytest.raises(ConnectionError):
            _ask(llm)  # t=100：连续 2 次失败 → 打开
    with pytest.raises(CircuitOpenError):
        _ask(llm)  # t=100：冷却中 → 快速失败
    clock[0] += 61  # 冷却期满
    with pytest.raises(ConnectionError):
        _ask(llm)  # 半开试探 → 失败 → 立即重新熔断
    with pytest.raises(CircuitOpenError):
        _ask(llm)  # 又打开了


def test_fallback_switches_to_next(caplog):
    import logging
    ledger = UsageLedger()
    chain = FallbackLLM(
        [
            RobustLLM(SwitchableLLM(fail=True), ledger, GatewayConfig(retries=0)),
            RobustLLM(EchoFakeLLM(), ledger, GatewayConfig(retries=0)),
        ],
        ledger,
    )
    with caplog.at_level(logging.WARNING):
        reply = _ask(chain, "PHP")
    assert reply.content == "echo:PHP"
    assert "[降级]" in caplog.text  # 切换时的诚实声明


def test_fallback_exhaustion_raises_llm_unavailable():
    ledger = UsageLedger()
    chain = FallbackLLM(
        [
            RobustLLM(SwitchableLLM(fail=True), ledger, GatewayConfig(retries=0)),
            RobustLLM(SwitchableLLM(fail=True), ledger, GatewayConfig(retries=0)),
        ],
        ledger,
    )
    with pytest.raises(LLMUnavailableError):
        _ask(chain)


def test_run_chat_survives_llm_unavailable(monkeypatch, capsys):
    """F 契约第 2 条：模型全挂时主循环不崩——用户消息留底片、无假回复。"""
    from facta.cli import run_chat

    inputs = iter(["测试问题", "退出"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))
    ledger = UsageLedger()
    llm = FallbackLLM(
        [RobustLLM(SwitchableLLM(fail=True), ledger, GatewayConfig(retries=0))], ledger
    )

    session, _ = run_chat(llm, None)  # 不该崩

    captured = capsys.readouterr().out
    assert "[模型不可用]" in captured
    assert session.messages[-1].role == "user"  # 用户消息留在底片，无假回复


def test_get_llm_builds_fallback_chain(monkeypatch):
    from facta.core.llm import get_llm

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
    llm = get_llm("deepseek")

    assert isinstance(llm, FallbackLLM)
    names = [c.name for c in llm.candidates]
    assert names[0] == "deepseek" and names[-1] == "mock"
