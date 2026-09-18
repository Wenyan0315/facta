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
import logging
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, replace

from agent.core.llm import LLM, LLMUnavailableError, StreamChunk, merge_stream_chunks
from agent.core.telemetry import UsageLedger
from agent.core.types import Message
from agent.core.vector_math import cosine_similarity

logger = logging.getLogger(__name__)

# 值得重试的 HTTP 状态：限流 + 服务器侧暂时性错误
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class GatewayConfig:
    """网关各件衣服的参数（评审 B 条：收敛构造参数，防签名膨胀）。

    语义档参数在 SemanticCacheLLM 自己构造上——它是独立装饰器，
    threshold/max_entries 是它的业务参数，不蹭网关配置。
    """

    retries: int = 2
    backoff: float = 0.5
    cache: bool = True
    cache_size: int = 128
    # d 段熔断：连续失败达阈值 → 打开（冷却期内快速失败）→ 冷却期满半开放行一次试探
    fail_threshold: int = 3
    cooldown_seconds: float = 30.0


class CircuitOpenError(RuntimeError):
    """熔断打开时的快速失败信号——FallbackLLM 拿它切下一个候选。"""

    def __init__(self, cooldown_left: float = 0.0) -> None:
        super().__init__(f"熔断打开中，约 {cooldown_left:.0f}s 后自动恢复")
        self.cooldown_left = cooldown_left


class RobustLLM(LLM):
    """包任意 LLM 实现的防护壳（a 记账 + b 重试 + c 精确缓存 + d 熔断）。

    四件衣服的穿法（顺序即语义）：
      精确缓存最先——命中即返回，不碰熔断不碰网络；
      熔断闸门次之——打开中快速失败，不再去撞墙；
      重试+超时最内——真调用的最后一次防守；
      记账横切所有出口（命中/成功/失败/重试都有账）。
    """

    def __init__(
        self,
        inner: LLM,
        ledger: UsageLedger | None = None,
        config: GatewayConfig | None = None,
    ) -> None:
        self._inner = inner
        # 对外报内层模型的名字（降级链打印认得清谁是谁）；
        # 空 label 兜底类名——与旧版 getattr property 同语义
        self.name = inner.name or inner.__class__.__name__
        self._ledger = ledger or UsageLedger()
        self._cfg = config or GatewayConfig()
        # 进程级 LRU 精确缓存，不落盘——重启清零没损失，下次重新算就是了
        self._cache: OrderedDict[str, Message] = OrderedDict()
        # d 衣熔断状态：closed（计数中）→ open（冷却中）→ half_open（放行一次试探）
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open = False

    @property
    def inner(self) -> LLM:
        """露出被包的原模型——测试断言与调试用。"""
        return self._inner

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        # c 衣：精确缓存命中 → 直接返回副本，不碰熔断不碰网络
        key = self._cache_key(messages, tools) if self._cfg.cache else None
        if key is not None:
            cached = self._cache.get(key)
            if cached is not None:
                self._ledger.record_cache_hit()
                return replace(cached)

        # d 衣：熔断闸门。open 且冷却未到 → CircuitOpenError 快速失败；
        #        冷却期满 → 半开放行这一次试探（结局由本次调用的成败决定）
        self._check_breaker()

        start = time.perf_counter()
        error: Exception | None = None
        for attempt in range(self._cfg.retries + 1):
            try:
                reply = self._inner.generate(messages, tools)
            except Exception as exc:
                error = exc
                # 不可重试（如 401/400）或重试已耗尽 → 奔最终失败
                if not self._should_retry(exc) or attempt == self._cfg.retries:
                    break
                self._ledger.record_retry()
                if self._cfg.backoff:
                    time.sleep(self._cfg.backoff * (2 ** attempt))  # 指数退避
            else:
                elapsed = time.perf_counter() - start
                self._ledger.record_llm(getattr(reply, "usage", None), self._cost_of(reply), elapsed)
                self._on_success()
                if key is not None:
                    self._store(key, reply)
                # 存进缓存的对象绝不外流：返回副本——调用方拿到的手伸不进缓存。
                # 注意 replace 是浅拷贝：tool_calls 列表仍与缓存本体共享引用——
                # 「缓存不可被改」靠「消息按不可变使用」的约定成立；若有人改动
                # 返回消息的 tool_calls 列表，隔离会被击穿（已知边界）
                return replace(reply)

        # 最终失败：记账 + 熔断计数 + 原样抛给上层（FallbackLLM / 主循环）
        self._ledger.record_llm_failure()
        self._on_failure()
        if error is None:  # 理论不可达：循环不 return 就必有 error
            raise RuntimeError("unreachable")
        raise error

    def generate_stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Iterator[StreamChunk]:
        """流式版防护壳（streaming）：四件衣的新语义按「第一块」重新划线。

        生成器惰性是本方法的全部地基：调用本函数 ≠ 请求已发出，
        函数体在第一次 next() 时才开始执行。于是：
        - 缓存/熔断照旧在开场判断
        - 重试只发生在「第一块到手之前」——第一块一 yield 字就外流了，
          按旧语义整段重试会把话吐两遍
        锁定候选后：后续块转发 + 途中抓 usage（末班车）→ 流耗尽记成功账；
        中途失败 → 记失败账 + 熔断计数 + 原样上抛（不能重试不能降级）。
        """
        # c 衣：精确缓存命中 → 伪流整块吐出（首字延迟=0，比真流还快）
        key = self._cache_key(messages, tools) if self._cfg.cache else None
        if key is not None:
            cached = self._cache.get(key)
            if cached is not None:
                self._ledger.record_cache_hit()
                yield StreamChunk(
                    content=cached.content or "",
                    tool_calls=cached.tool_calls,
                    usage=getattr(cached, "usage", None),
                )
                return

        # d 衣：熔断闸门（开场检查一次；流中途没有第二次检查——流已锁定）
        self._check_breaker()

        start = time.perf_counter()
        error: Exception | None = None
        for attempt in range(self._cfg.retries + 1):
            try:
                stream = self._inner.generate_stream(messages, tools)
                first = next(stream)   # 惰性：真正的网络请求发生在这一行
            except Exception as exc:
                error = exc
                if not self._should_retry(exc) or attempt == self._cfg.retries:
                    break
                self._ledger.record_retry()
                if self._cfg.backoff:
                    time.sleep(self._cfg.backoff * (2 ** attempt))  # 指数退避
            else:   # 第一块到手 → 立即锁死该候选
                usage: dict | None = None
                try:
                    if first.usage:
                        usage = first.usage
                    yield first
                    for chunk in stream:
                        if chunk.usage:
                            usage = chunk.usage
                        yield chunk
                except Exception:
                    # 中途失败：字已外流，重试=重复说话，降级也一样——只能上抛
                    self._ledger.record_llm_failure()
                    self._on_failure()
                    raise
                # 流耗尽 = 调用成功：usage 记账 + 熔断复位 + 计时
                self._ledger.record_llm(usage, self._cost_of_usage(usage), time.perf_counter() - start)
                self._on_success()
                return

        # 开场失败（一直没拿到第一块）：与 generate 相同的最终失败路径
        self._ledger.record_llm_failure()
        self._on_failure()
        if error is None:  # 理论不可达
            raise RuntimeError("unreachable")
        raise error

    # ---- d 衣：熔断三态 ----

    def _check_breaker(self) -> None:
        if self._opened_at is None and not self._half_open:
            return  # closed：正常放行
        if self._opened_at is not None:
            left = self._cfg.cooldown_seconds - (time.monotonic() - self._opened_at)
            if left > 0:
                raise CircuitOpenError(left)  # open：快速失败，不再撞墙
            # 冷却期满 → 半开：放行这一次试探
            self._opened_at = None
            self._half_open = True

    def _on_failure(self) -> None:
        """一次调用级失败落账（重试再多次也只算一次调用级失败）。"""
        self._consecutive_failures += 1
        if self._half_open:
            # 半开试探失败 → 立即重新熔断，开始新一轮冷却
            self._half_open = False
            self._opened_at = time.monotonic()
            self._consecutive_failures = 0
        elif self._consecutive_failures >= self._cfg.fail_threshold:
            self._opened_at = time.monotonic()
            self._consecutive_failures = 0

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._half_open = False

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
        while len(self._cache) > self._cfg.cache_size:
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
        return self._cost_of_usage(getattr(reply, "usage", None))

    def _cost_of_usage(self, usage: dict | None) -> float:
        """按内层模型的价格表把 token 换算成钱；无 usage 或无价目 → 0。

        与 _cost_of 的差别只在入口：非流式从 Message 身上摘 usage，
        流式路径只有 usage 本尊（末班车块），直接喂。"""
        pricing = getattr(self._inner, "pricing", None)
        if not usage or not pricing:
            return 0.0
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        return tokens_in / 1e6 * pricing["in"] + tokens_out / 1e6 * pricing["out"]


class SemanticCacheLLM(LLM):
    """语义缓存衣（c 段第二档）：精确档 miss 后，按语义相似度复用旧答案。

    独立装饰器而不是 RobustLLM 的一个开关（评审 C 条）——
    它吃 embedder 依赖，塞进 RobustLLM 会让网关吃知识层组件，职责糊掉。

    三个保守触发条件（复用旧答案离答非所问只有一线之隔）：
    1. 仅 tools=None（纯聊天）——带菜单的回复携带 tool_calls，
       复用旧答案等于重放旧动作，不安全
    2. 能定位最后一条 role="user" 的消息当查询；找不到就透传，不硬来
    3. 与新查询最相似的历史问答 ≥ threshold（BGE 下 0.92 ≈ 同一问）

    诚实定位：个人聊天场景语义命中率天然低——它的战场是 FAQ 类高频
    相似查询。教学项目实现它是掌握机制；账单会真实反映命中率。

    成本：每次 miss 要 embed 一次查询（经注入的 embedder 进同一个账本）。
    """

    name = "semantic_cache"

    def __init__(
        self,
        inner: LLM,
        embedder,
        ledger: UsageLedger | None = None,
        threshold: float = 0.92,
        max_entries: int = 64,
    ) -> None:
        self._inner = inner
        self._embedder = embedder
        self._ledger = ledger or UsageLedger()
        self._threshold = threshold
        # (查询向量, 回复) 线性扫描——教学规模 64 条以内，简单就是性能
        self._entries: list[tuple[list[float], Message]] = []
        self._max_entries = max_entries

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        qvec: list[float] | None = None
        if tools is None:
            query = next(
                (m.content for m in reversed(messages) if m.role == "user"), None
            )
            if query is not None:
                qvec = self._embedder.embed([query])[0]
                cached = self._best_match(qvec)
                if cached is not None:
                    self._ledger.record_cache_hit()
                    return replace(cached)  # 副本外流，缓存本体不可被改（同精确档）

        reply = self._inner.generate(messages, tools)

        # 只有真正走了真模型的回复才入库；超上限时淘汰最老一条
        if qvec is not None:
            self._entries.append((qvec, reply))
            if len(self._entries) > self._max_entries:
                self._entries.pop(0)
            # 存进条目的对象绝不外流（同精确档的缓存污染教训）
            return replace(reply)
        return reply

    def generate_stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Iterator[StreamChunk]:
        """流式版语义缓存：判断照旧（embed+阈值），只换两处手脚。

        - 命中 → 伪流整块吐出（同精确档，首字延迟=0）
        - miss → 转发流，流耗尽后用 merge_stream_chunks 把这一路拼回
          完整 Message 入库——语义档的「miss 也积累条目」对流式调用同样成立
        """
        qvec: list[float] | None = None
        if tools is None:
            query = next(
                (m.content for m in reversed(messages) if m.role == "user"), None
            )
            if query is not None:
                qvec = self._embedder.embed([query])[0]
                cached = self._best_match(qvec)
                if cached is not None:
                    self._ledger.record_cache_hit()
                    yield StreamChunk(
                        content=cached.content or "",
                        tool_calls=cached.tool_calls,
                        usage=getattr(cached, "usage", None),
                    )
                    return

        collected: list[StreamChunk] = []
        for chunk in self._inner.generate_stream(messages, tools):
            collected.append(chunk)
            yield chunk

        # 流耗尽才拼得出完整回复，此时才入库（entries 存的仍是本体，
        # 这里刚拼出来的对象没有被消费层引用，无缓存污染面——与外流通道隔离）
        if qvec is not None:
            self._entries.append((qvec, merge_stream_chunks(iter(collected))))
            if len(self._entries) > self._max_entries:
                self._entries.pop(0)

    def _best_match(self, qvec: list[float]) -> Message | None:
        best: Message | None = None
        best_score = -1.0
        for vec, reply in self._entries:
            score = cosine_similarity(qvec, vec)
            if score > best_score:
                best_score = score
                best = reply
        if best is not None and best_score >= self._threshold:
            return best
        return None


class FallbackLLM(LLM):
    """降级链（d 段）：候选列表逐个试，全挂抛 LLMUnavailableError。

    切换时打印「已降级到 {name}」——诚实降级，不装正常（也当可观测性事件）。
    捕获范围是 Exception 全部：401 这类「这个候选没救了」也切下一个——
    主候选没救不代表备选没救；只有链耗尽才是 LLMUnavailableError。
    """

    name = "fallback"

    def __init__(
        self, candidates: list[LLM], ledger: UsageLedger | None = None
    ) -> None:
        self._candidates = list(candidates)
        self._ledger = ledger or UsageLedger()

    @property
    def candidates(self) -> list[LLM]:
        return self._candidates

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        errors: list[str] = []
        for index, candidate in enumerate(self._candidates):
            if index > 0:
                logger.warning(f"[降级] 前面的模型不可用，已切换到 {candidate.name}")
            try:
                return candidate.generate(messages, tools)
            except Exception as exc:
                errors.append(f"{candidate.name}: {exc}")
        raise LLMUnavailableError("全部模型不可用——" + "；".join(errors))

    def generate_stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Iterator[StreamChunk]:
        """流式降级：候选按「第一块」锁定——惰性重试/降级的链上版。

        - 第一块到手之前失败 → 切下一个候选（字还没外流，随便换）
        - 第一块到手（yield first 之后）→ 锁死：后续失败直接上抛，
          中途换模型会把前半段回答吐两遍（RobustLLM 同款划线）
        """
        errors: list[str] = []
        for index, candidate in enumerate(self._candidates):
            if index > 0:
                logger.warning(f"[降级] 前面的模型不可用，已切换到 {candidate.name}")
            try:
                stream = candidate.generate_stream(messages, tools)
                first = next(stream)
            except Exception as exc:
                errors.append(f"{candidate.name}: {exc}")
            else:
                yield first   # 从这里开始这段话的每一个字都算数了
                yield from stream
                return
        raise LLMUnavailableError("全部模型不可用——" + "；".join(errors))
