"""M7.5 账本（telemetry）：所有外部模型调用的花费与成败记录。

职责只有记账——不参与调用控制（重试/缓存/熔断归 gateway.py），也不做决策。
它是「事后归因」的证据，也是后续几段的度量尺：缓存省了多少钱、熔断挡了
多少次撞墙，全凭这本账说话。

LLM 与 embedding 共用一本账（全局一个实例），
退出的账单打印归组装层（账本自己不懂何时该开口）。

并发下的计数是近似值（S8a 起多会话 worker 并行）：`x += 1` 在 CPython 里
是读-改-写三步，GIL 可在中间切换，两个线程同时记一笔可能只加一次。
裁定不加锁——10 个 record_* 各包一层 with 噪音大，dataclass 塞 Lock 字段
还会污染 ==/repr 语义（测试要比对账本）。账单是参考值不是结算账目，
误差量级 = 并发度，可接受。真要精确账目得上进程外聚合，那是多进程演进的事。
"""

from dataclasses import dataclass


@dataclass
class UsageLedger:
    """进程级账本：累计 token、钱、成败。字段只增不减。"""

    llm_calls: int = 0      # 成功落地的 LLM 调用次数
    llm_retries: int = 0    # 重试次数（b 段启用）
    llm_failures: int = 0   # 最终失败次数（重试耗尽）
    llm_cache_hits: int = 0 # 缓存直接命中（c 段启用）
    tokens_in: int = 0
    tokens_out: int = 0
    # 080：prefix 缓存账目（DeepSeek 字节稳定前缀命中）——命中率纳入成本回归，
    # 账单是「事后归因」的观测尺，不是精确结算账目（并发近似同本文件约定）
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    llm_cost: float = 0.0
    llm_seconds: float = 0.0  # 累计耗时（含重试），可换算平均延迟
    embed_calls: int = 0
    embed_tokens: int = 0
    embed_cost: float = 0.0
    # M10 场景路由（Jev 决策模型）：路由调用与降级次数（降级=Jev 故障
    # fail-open 走 LLM 原生路径——账单可见，可观测性要求降级显式化）
    jev_calls: int = 0
    jev_degradations: int = 0

    def record_llm(
        self, usage: dict | None, cost: float = 0.0, elapsed: float = 0.0
    ) -> None:
        """一笔成功调用入账。usage 是 OpenAI 格式 token 账目，可能为 None。"""
        self.llm_calls += 1
        if usage:
            self.tokens_in += usage.get("prompt_tokens", 0)
            self.tokens_out += usage.get("completion_tokens", 0)
            self.prompt_cache_hit_tokens += usage.get("prompt_cache_hit_tokens") or 0
            self.prompt_cache_miss_tokens += usage.get("prompt_cache_miss_tokens") or 0
        self.llm_cost += cost
        self.llm_seconds += elapsed

    def record_llm_failure(self) -> None:
        self.llm_failures += 1

    def record_retry(self) -> None:
        self.llm_retries += 1

    def record_cache_hit(self) -> None:
        self.llm_cache_hits += 1

    def record_embed(self, tokens: int, cost: float = 0.0) -> None:
        self.embed_calls += 1
        self.embed_tokens += tokens
        self.embed_cost += cost

    def record_jev(self) -> None:
        """一笔 Jev 路由调用入账（M10）。token/成本暂不展开——路由按次计费，
        量级远低于 LLM（bench：约 1/20），账单行先记次数。"""
        self.jev_calls += 1

    def record_jev_degradation(self) -> None:
        """一次路由降级入账（M10）：Jev 故障 fail-open 走 LLM 原生路径。"""
        self.jev_degradations += 1

    def bill(self) -> str:
        """把账本渲染成人话账单（退出时由 __main__ 调用）。"""
        notes = []
        if self.llm_retries or self.llm_failures:
            notes.append(f"重试 {self.llm_retries} 次 / 失败 {self.llm_failures} 次")
        if self.llm_cache_hits:
            notes.append(f"缓存命中 {self.llm_cache_hits} 次")
        tail = f"（{'，'.join(notes)}）" if notes else ""

        lines = [
            "===== 本次会话账单 =====",
            f"LLM 调用   : {self.llm_calls} 次{tail}",
            f"  tokens   : 入 {self.tokens_in:,} / 出 {self.tokens_out:,}",
            f"  LLM 小计 : ¥{self.llm_cost:.4f}",
            f"Embedding  : {self.embed_calls} 次，{self.embed_tokens:,} tokens，¥{self.embed_cost:.4f}",
            f"合计       : ¥{self.llm_cost + self.embed_cost:.4f}",
        ]
        if self.llm_calls:
            lines.insert(2, f"  平均耗时 : {self.llm_seconds / self.llm_calls:.2f}s/次")
        prefix_total = self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
        if prefix_total:
            rate = self.prompt_cache_hit_tokens / prefix_total
            lines.insert(3, f"  prefix   : 命中 {rate:.1%}（{self.prompt_cache_hit_tokens:,} / {prefix_total:,} tokens）")
        if self.jev_calls or self.jev_degradations:
            deg = f"（降级 {self.jev_degradations} 次走原生路径）" if self.jev_degradations else ""
            lines.append(f"Jev 路由   : {self.jev_calls} 次{deg}")
        return "\n".join(lines)


@dataclass
class CallEntry:
    """一笔逻辑模型调用的明细记录（含重试；缓存命中不在此列）。

    call_id 进程内唯一；attempts 是真实下游尝试次数——观测真源，来自
    RobustLLM 重试循环控制流（_inner.generate 每被调用一次），不取自 usage
    字段：即使 usage=None，请求尝试次数仍准确记 1；usage 是 LLM 自报用量
    （可能 None，失败或无响应体时不猜测）；outcome 为 ok|error。
    """

    call_id: str
    operation: str
    attempts: int
    usage: dict | None
    outcome: str


class CallLedger:
    """明细账：与 UsageLedger 累计尺职责分离（累计尺只增不减，明细逐笔）。

    UsageLedger 答「总共花了多少」，CallLedger 答「每一笔花在哪」——
    call_id 是核验【唯一归属】的锚（查双计/漏记/虚构全靠它）。
    """

    def __init__(self) -> None:
        self._entries: list[CallEntry] = []
        self._seq = 0

    def record(
        self,
        operation: str,
        attempts: int,
        usage: dict | None,
        outcome: str,
        call_id: str | None = None,
    ) -> str:
        """落一笔，返回 call_id。production 自增；测试传固定 call_id 制造篡改场景。"""
        if call_id is None:
            self._seq += 1
            call_id = f"c{self._seq:04d}"
        self._entries.append(CallEntry(call_id, operation, attempts, usage, outcome))
        return call_id

    @property
    def entries(self) -> list[CallEntry]:
        return list(self._entries)


@dataclass
class CallVerification:
    """明细账 vs 累计尺 vs 唯一归属的交叉核验结果。"""

    ledger_attempts: int         # 明细账 attempt 合计
    expected_attempts: int       # 累计尺推算的总 attempt 数（calls+retries+failures）
    ledger_matches_total: bool
    duplicates: list[str]        # 同一 call_id 重复入账
    ok: bool


def verify_calls(call_ledger: CallLedger, usage: UsageLedger) -> CallVerification:
    """交叉核验：明细账 attempt 合计须等于累计尺推算值，且 call_id 无重复。

    恒等式依据：一次逻辑调用要么成功（1 次落地 attempt + 其重试）要么最终失败
    （重试耗尽），每一次 attempt 恰好落入 calls / retries / failures 三者之一——
    故 总 attempt = llm_calls + llm_retries + llm_failures。这是「内部一致性」，
    只能查出账本内部漏写；「与外部观测不一致」的漏记/虚构靠测试替身的
    matches_observation 兜住（见 ADR 108 档 A）。
    """
    entries = call_ledger.entries
    ledger_attempts = sum(e.attempts for e in entries)
    expected = usage.llm_calls + usage.llm_retries + usage.llm_failures
    seen: dict[str, int] = {}
    for e in entries:
        seen[e.call_id] = seen.get(e.call_id, 0) + 1
    duplicates = [cid for cid, n in seen.items() if n > 1]
    ok = ledger_attempts == expected and not duplicates
    return CallVerification(
        ledger_attempts=ledger_attempts,
        expected_attempts=expected,
        ledger_matches_total=ledger_attempts == expected,
        duplicates=duplicates,
        ok=ok,
    )
