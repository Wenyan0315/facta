"""M7.5 账本（telemetry）：所有外部模型调用的花费与成败记录。

职责只有记账——不参与调用控制（重试/缓存/熔断归 gateway.py），也不做决策。
它是「事后归因」的证据，也是后续几段的度量尺：缓存省了多少钱、熔断挡了
多少次撞墙，全凭这本账说话。

LLM 与 embedding 共用一本账（进程单会话，全局一个实例即可），
退出的账单打印归组装层（账本自己不懂何时该开口）。
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
    llm_cost: float = 0.0
    llm_seconds: float = 0.0  # 累计耗时（含重试），可换算平均延迟
    embed_calls: int = 0
    embed_tokens: int = 0
    embed_cost: float = 0.0

    def record_llm(
        self, usage: dict | None, cost: float = 0.0, elapsed: float = 0.0
    ) -> None:
        """一笔成功调用入账。usage 是 OpenAI 格式 token 账目，可能为 None。"""
        self.llm_calls += 1
        if usage:
            self.tokens_in += usage.get("prompt_tokens", 0)
            self.tokens_out += usage.get("completion_tokens", 0)
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
        return "\n".join(lines)