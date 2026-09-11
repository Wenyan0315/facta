"""M7.5 网关：所有 LLM 调用的总闸门（进程内装饰器，不是独立服务）。

对外还是 LLM 接口——agent_loop / compressor / tools 照旧调用，无感知。
对内依次穿过四件衣服（随里程碑 a→d 渐次穿上）：
  a. 记账与可观测 —— 已穿（本文件当前形态）：每次调用记成败/token/钱/耗时
  b. 重试 + 超时    —— 待穿
  c. 缓存           —— 待穿
  d. 熔断 + 降级链  —— 待穿

为什么衣服穿在 wrapper 而不是 OpenAICompatibleLLM 里：
  假模型也要过闸（mock 模式账单也有一行）；
  缓存命中不该触达网络，只有 wrapper 的位置拦得住；
  重试次数只有 wrapper 数得清。
"""

import time

from agent.core.llm import LLM
from agent.core.telemetry import UsageLedger
from agent.core.types import Message


class RobustLLM(LLM):
    """包任意 LLM 实现的防护壳（a 段：只穿记账衣）。"""

    name = "robust"

    def __init__(self, inner: LLM, ledger: UsageLedger | None = None) -> None:
        self._inner = inner
        self._ledger = ledger or UsageLedger()

    @property
    def inner(self) -> LLM:
        """露出被包的原模型——测试断言与调试用。"""
        return self._inner

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        start = time.perf_counter()
        try:
            reply = self._inner.generate(messages, tools)
        except Exception:
            self._ledger.record_llm_failure()
            raise
        elapsed = time.perf_counter() - start
        self._ledger.record_llm(getattr(reply, "usage", None), self._cost_of(reply), elapsed)
        return reply

    def _cost_of(self, reply: Message) -> float:
        """按内层模型的价格表把 token 换算成钱；无 usage 或无价目 → 0。"""
        usage = getattr(reply, "usage", None)
        pricing = getattr(self._inner, "pricing", None)
        if not usage or not pricing:
            return 0.0
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        return tokens_in / 1e6 * pricing["in"] + tokens_out / 1e6 * pricing["out"]