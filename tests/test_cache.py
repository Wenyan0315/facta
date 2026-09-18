"""M7.5c 缓存验收：精确档（同输入命中/LRU/副本隔离）+ 语义档（相似复用/保守条件）。"""

from agent.core.gateway import GatewayConfig, RobustLLM, SemanticCacheLLM
from agent.core.llm import LLM
from agent.core.telemetry import UsageLedger
from agent.core.types import Message


class CountingLLM(LLM):
    """记录调用次数、按内容回声的假模型。"""

    name = "counting"

    def __init__(self) -> None:
        self.attempts = 0

    def generate(self, messages, tools=None) -> Message:
        self.attempts += 1
        return Message(role="assistant", content=f"echo:{messages[-1].content}")


def _ask(llm, text, tools=None) -> Message:
    return llm.generate([Message(role="user", content=text)], tools)


class FakeEmbedder:
    """离线假 embedder：给指定查询固定向量——语义测试不用真网络。

    「PHP 是什么」与「PHP 是啥」映射同一向量（0.99 相似计算所需），
    与「Python 是什么」正交。
    """

    def __init__(self) -> None:
        self.table = {
            "PHP 是什么": [1.0, 0.0],
            "PHP 是啥": [0.99, 0.14],
            "Python 是什么": [0.0, 1.0],
        }

    def embed(self, texts):
        return [self.table[t] for t in texts]


def _semantic_chain(embedder=None, ledger=None, threshold=0.92):
    """组装语义档测试链：语义缓存 → RobustLLM(精确档+重试) → 假模型。"""
    ledger = ledger or UsageLedger()
    inner = CountingLLM()
    return SemanticCacheLLM(
        RobustLLM(inner, ledger, GatewayConfig(retries=0)), embedder or FakeEmbedder(), ledger, threshold
    ), inner, ledger


def test_semantic_hit_on_similar_query():
    llm, inner, ledger = _semantic_chain()
    _ask(llm, "PHP 是什么")
    second = _ask(llm, "PHP 是啥")  # 相似度 0.99 ≥ 0.92 → 复用旧答案

    assert inner.attempts == 1
    assert second.content == "echo:PHP 是什么"
    assert ledger.llm_cache_hits == 1


def test_semantic_miss_on_dissimilar():
    llm, inner, _ = _semantic_chain()
    _ask(llm, "PHP 是什么")
    _ask(llm, "Python 是什么")  # 正交 → miss
    assert inner.attempts == 2


def test_semantic_skips_when_tools_present():
    llm, inner, _ = _semantic_chain()
    menu = [{"name": "get_current_time"}]
    _ask(llm, "PHP 是什么", tools=menu)
    _ask(llm, "PHP 是啥", tools=menu)  # 带菜单 → 跳过语义档，绝不重放 tool_calls
    assert inner.attempts == 2


def test_semantic_skips_without_user_message():
    llm, inner, _ = _semantic_chain()
    # 消息里没有 user → 找不到查询 → 语义档透传。
    # 用不同内容（否则会被下面 RobustLLM 的精确档截胡——那是精确档的正确行为）
    llm.generate([Message(role="assistant", content="自言自语一")], None)
    llm.generate([Message(role="assistant", content="自言自语二")], None)
    assert inner.attempts == 2


def test_semantic_threshold_blocks_borderline():
    # 阈值抬到 0.999：0.99 的「PHP 是啥」不够格 → miss
    llm, inner, _ = _semantic_chain(threshold=0.999)
    _ask(llm, "PHP 是什么")
    _ask(llm, "PHP 是啥")
    assert inner.attempts == 2


def test_semantic_reply_mutation_does_not_pollute():
    llm, inner, _ = _semantic_chain()
    reply = _ask(llm, "PHP 是什么")
    reply.content = "被改掉"
    again = _ask(llm, "PHP 是啥")
    assert again.content == "echo:PHP 是什么"
    assert inner.attempts == 1


def test_identical_call_hits_cache():
    ledger = UsageLedger()
    inner = CountingLLM()
    llm = RobustLLM(inner, ledger, GatewayConfig(retries=0))

    first = _ask(llm, "你好")
    second = _ask(llm, "你好")

    assert inner.attempts == 1  # 真模型只被叫了一次
    assert second.content == first.content
    assert second is not first  # 返回的是副本，不是缓存本体
    assert ledger.llm_cache_hits == 1
    assert ledger.llm_calls == 1


def test_different_input_misses_cache():
    inner = CountingLLM()
    llm = RobustLLM(inner, config=GatewayConfig(retries=0))
    _ask(llm, "问题一")
    _ask(llm, "问题二")
    assert inner.attempts == 2


def test_different_tools_menu_misses_cache():
    inner = CountingLLM()
    llm = RobustLLM(inner, config=GatewayConfig(retries=0))
    _ask(llm, "同一个问题", tools=None)
    _ask(llm, "同一个问题", tools=[{"name": "get_current_time"}])
    assert inner.attempts == 2  # 菜单不同 = 不同的请求，不能串味


def test_lru_eviction():
    inner = CountingLLM()
    llm = RobustLLM(inner, config=GatewayConfig(retries=0, cache_size=2))
    for q in ("一", "二", "三"):
        _ask(llm, q)
    assert inner.attempts == 3
    _ask(llm, "一")  # 「一」已被 LRU 淘汰 → 重新真调
    assert inner.attempts == 4


def test_mutating_reply_does_not_pollute_cache():
    inner = CountingLLM()
    llm = RobustLLM(inner, config=GatewayConfig(retries=0))
    reply = _ask(llm, "你好")
    reply.content = "被调用方改掉了"  # 模拟上游改动返回的消息
    again = _ask(llm, "你好")
    assert again.content == "echo:你好"  # 缓存本体不受污染
    assert inner.attempts == 1


def test_cache_disabled():
    inner = CountingLLM()
    llm = RobustLLM(inner, config=GatewayConfig(retries=0, cache=False))
    _ask(llm, "你好")
    _ask(llm, "你好")
    assert inner.attempts == 2
