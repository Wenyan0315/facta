"""M7.5c 精确缓存验收：同输入命中、不同输入/菜单错开、LRU 淘汰、副本隔离。"""

from agent.core.gateway import RobustLLM
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


def test_identical_call_hits_cache():
    ledger = UsageLedger()
    inner = CountingLLM()
    llm = RobustLLM(inner, ledger, retries=0)

    first = _ask(llm, "你好")
    second = _ask(llm, "你好")

    assert inner.attempts == 1  # 真模型只被叫了一次
    assert second.content == first.content
    assert second is not first  # 返回的是副本，不是缓存本体
    assert ledger.llm_cache_hits == 1
    assert ledger.llm_calls == 1


def test_different_input_misses_cache():
    inner = CountingLLM()
    llm = RobustLLM(inner, retries=0)
    _ask(llm, "问题一")
    _ask(llm, "问题二")
    assert inner.attempts == 2


def test_different_tools_menu_misses_cache():
    inner = CountingLLM()
    llm = RobustLLM(inner, retries=0)
    _ask(llm, "同一个问题", tools=None)
    _ask(llm, "同一个问题", tools=[{"name": "get_current_time"}])
    assert inner.attempts == 2  # 菜单不同 = 不同的请求，不能串味


def test_lru_eviction():
    inner = CountingLLM()
    llm = RobustLLM(inner, retries=0, cache_size=2)
    for q in ("一", "二", "三"):
        _ask(llm, q)
    assert inner.attempts == 3
    _ask(llm, "一")  # 「一」已被 LRU 淘汰 → 重新真调
    assert inner.attempts == 4


def test_mutating_reply_does_not_pollute_cache():
    inner = CountingLLM()
    llm = RobustLLM(inner, retries=0)
    reply = _ask(llm, "你好")
    reply.content = "被调用方改掉了"  # 模拟上游改动返回的消息
    again = _ask(llm, "你好")
    assert again.content == "echo:你好"  # 缓存本体不受污染
    assert inner.attempts == 1


def test_cache_disabled():
    inner = CountingLLM()
    llm = RobustLLM(inner, retries=0, cache=False)
    _ask(llm, "你好")
    _ask(llm, "你好")
    assert inner.attempts == 2