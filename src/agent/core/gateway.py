"""M7.5 网关：所有 LLM 调用的总闸门（进程内装饰器，不是独立服务）。

对外还是 LLM 接口——agent_loop / compressor / tools 照旧调用，无感知。
对内依次穿过四件衣服（随里程碑 a→d 渐次穿上）：
  a. 记账与可观测 —— 已穿：每次调用记成败/token/钱/耗时
  b. 重试 + 超时    —— 已穿：可重试异常指数退避重试，不可重试立即抛
  c. 缓存           —— 已穿：精确档（键=模型+输入哈希）命中即返回副本
  d. 熔断 + 降级链  —— 待穿

为什么衣服穿在 wrapper 而不是 OpenAICompatibleLLM 里：
  假模型也要过闸（mock 模式账单也有一行）；
  缓存命中不该触达网络，只有 wrapper 的位置拦得住；
  重试次数只有 wrapper 数得清。
"""

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import replace

from agent.core.llm import LLM
from agent.core.telemetry import UsageLedger
from agent.core.types import Message

# 值得重试的 HTTP 状态：限流 + 服务器侧暂时性错误
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RobustLLM(LLM):
    """包任意 LLM 实现的防护壳（a 记账衣 + b 重试衣 + c 缓存衣）。"""

    name = "robust"

    def __init__(
        self,
        inner: LLM,
        ledger: UsageLedger | None = None,
        retries: int = 2,
        backoff: float = 0.5,
        cache: bool = True,
        cache_size: int = 128,
    ) -> None:
        self._inner = inner
        self._ledger = ledger or UsageLedger()
        self._retries = retries
        self._backoff = backoff
        # 进程级 LRU 缓存，不落盘——重启清零没损失，下次重新算就是了
        self._cache: "OrderedDict[str, Message]" = OrderedDict()
        self._cache_on = cache
        self._cache_size = cache_size

    @property
    def inner(self) -> LLM:
        """露出被包的原模型——测试断言与调试用。"""
        return self._inner

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        # c 衣（第一层·精确档）：同输入就直接吐缓存，连重试衣都不进——命中即省钱
        key = self._cache_key(messages, tools) if self._cache_on else None
        if key is not None:
            cached = self._cache.get(key)
            if cached is not None:
                self._ledger.record_cache_hit()
                # 返回副本：调用方会 append/修改消息，共享同一个对象=缓存被污染
                # （List identity trap 的表兄弟）
                return replace(cached)

        start = time.perf_counter()
        for attempt in range(self._retries + 1):
            try:
                reply = self._inner.generate(messages, tools)
                elapsed = time.perf_counter() - start
                self._ledger.record_llm(getattr(reply, "usage", None), self._cost_of(reply), elapsed)
                if key is not None:
                    self._store(key, reply)
                return reply
            except Exception as exc:
                # 不可重试（如 401/400）或重试已耗尽 → 记失败，原样抛
                if not self._should_retry(exc) or attempt == self._retries:
                    self._ledger.record_llm_failure()
                    raise
                self._ledger.record_retry()
                if self._backoff:
                    time.sleep(self._backoff * (2 ** attempt))  # 指数退避
        raise RuntimeError("unreachable")  # 循环必然以 return 或 raise 结束

    def _cache_key(self, messages: list[Message], tools: list[dict] | None) -> str:
        """键 = 模型身份 + 完整输入哈希。usage 是回复侧的，不进键。

        换模型必换键——deepseek 与 siliconflow 的答案绝不互相串味。
        """
        raw = [
            [m.role, m.content, m.tool_calls, m.tool_call_id]
            for m in messages
        ]
        inner_model = getattr(self._inner, "_model", self._inner.__class__.__name__)
        payload = json.dumps([inner_model, raw, tools], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _store(self, key: str, reply: Message) -> None:
        """写入缓存并维持 LRU 上限；写满时淘汰最老一条。"""
        self._cache[key] = reply
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _should_retry(self, exc: Exception) -> bool:
        """鸭子判型：不 import openai，看异常身上有没有 status_code。

        OpenAI 异常系列都带 status_code：
        - 连接失败/超时等运输层错误没有它 → getattr 得 None → 值得重试
        - 429/5xx → 值得重试（对方暂时病了）
        - 401/400 等其余 4xx → 立即抛（重试一万次也不会好）
        """
        status = getattr(exc, "status_code", None)
        if status is None:
            return True
        return status in _RETRYABLE_STATUS

    def _cost_of(self, reply: Message) -> float:
        """按内层模型的价格表把 token 换算成钱；无 usage 或无价目 → 0。"""
        usage = getattr(reply, "usage", None)
        pricing = getattr(self._inner, "pricing", None)
        if not usage or not pricing:
            return 0.0
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        return tokens_in / 1e6 * pricing["in"] + tokens_out / 1e6 * pricing["out"]