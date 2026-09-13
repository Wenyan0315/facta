"""streaming 里程碑验收：伪流默认 / 分片合并纯函数 / 网关四件衣的流式语义。

红线上位：本文件钉死的是「第一块」划出来的新语义——
重试只能发生在第一块到手之前，锁了候选中途失败只会上抛；
缓存命中走伪流（首字延迟=0）；usage 只在末班车，成功记账发生在流耗尽时。
"""

import pytest

from agent.core.gateway import (
    CircuitOpenError,
    FallbackLLM,
    GatewayConfig,
    RobustLLM,
    SemanticCacheLLM,
)
from agent.core.llm import (
    LLM,
    MockLLM,
    ScriptedLLM,
    StreamChunk,
    merge_stream_chunks,
)
from agent.core.telemetry import UsageLedger
from agent.core.types import Message


# ---------- 假组件 ----------

class StreamingFake(LLM):
    """按模式吐块的假流模型：可编程第一块前失败 / 中途失败。

    关键机制供测试：「raise 放在第一个 yield 之前」让它发生在
    第一次 next() 时——这就是真实供应商「请求发出才炸」的离线替身。
    """

    name = "stream_fake"

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode            # ok | open_fail_once | always_fail | mid_fail
        self.stream_starts = 0

    def generate(self, messages, tools=None) -> Message:
        return Message(role="assistant", content="你好！")

    def generate_stream(self, messages, tools=None):
        self.stream_starts += 1
        if self.mode == "open_fail_once":
            self.mode = "ok"        # 只炸第一回，之后正常
            raise ConnectionError("网络抖动")
        if self.mode == "always_fail":
            raise ConnectionError("连不上")
        yield StreamChunk(content="你")
        yield StreamChunk(content="好")
        if self.mode == "mid_fail":
            raise ConnectionError("中途断线")
        yield StreamChunk(
            content="！",
            usage={"prompt_tokens": 10, "completion_tokens": 3},
        )


class EchoCountingLLM(LLM):
    """回声假模型 + 调用计数：非流与流式（默认伪流）共用。"""

    name = "echo_counting"

    def __init__(self) -> None:
        self.attempts = 0

    def generate(self, messages, tools=None) -> Message:
        self.attempts += 1
        return Message(role="assistant", content=f"echo:{messages[-1].content}")


class FakeEmbedder:
    """离线假 embedder：同义查询映射近向量（复用 test_cache 的同款手法）。"""

    def __init__(self) -> None:
        self.table = {
            "PHP 是什么": [1.0, 0.0],
            "PHP 是啥": [0.99, 0.14],
            "Python 是什么": [0.0, 1.0],
        }

    def embed(self, texts):
        return [self.table[t] for t in texts]


def _ask_stream(llm, text, tools=None):
    return llm.generate_stream([Message(role="user", content=text)], tools)


# ---------- 伪流默认（接口演进：老实现零改动获得流能力） ----------

def test_default_pseudo_stream_on_mock():
    reply = MockLLM().generate([Message(role="user", content="你好")])
    chunks = list(MockLLM().generate_stream([Message(role="user", content="你好")]))
    assert len(chunks) == 1
    assert chunks[0].content == reply.content


def test_default_pseudo_stream_carries_tool_calls():
    script = [
        Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "name": "get_current_time", "arguments": "{}"}],
        )
    ]
    chunks = list(ScriptedLLM(script).generate_stream([Message(role="user", content="几点了")]))
    assert chunks[0].tool_calls == script[0].tool_calls


# ---------- 合并纯函数：content 拼接 / tool_calls 分片重组 / usage 末班车 ----------

def test_merge_joins_text_and_invokes_on_text():
    chunks = iter(
        [StreamChunk(content="你"), StreamChunk(content="好"), StreamChunk(content="世界")]
    )
    seen: list[str] = []
    reply = merge_stream_chunks(chunks, on_text=seen.append)

    assert reply.content == "你好世界"
    assert seen == ["你", "好", "世界"]   # 打印副作用与合并计算解耦


def test_merge_reassembles_fragmented_tool_call():
    chunk_a = StreamChunk(tool_calls=[{"index": 0, "id": "call_1", "name": "get_current_time", "arguments": ""}])
    chunk_b = StreamChunk(tool_calls=[{"index": 0, "id": None, "name": None, "arguments": '{"tz"'}])
    chunk_c = StreamChunk(tool_calls=[{"index": 0, "id": None, "name": None, "arguments": ': "Asia/Shanghai"}'}])

    reply = merge_stream_chunks(iter([chunk_a, chunk_b, chunk_c]))

    assert reply.tool_calls == [
        {"id": "call_1", "name": "get_current_time", "arguments": '{"tz": "Asia/Shanghai"}'}
    ]


def test_merge_interleaved_multi_tool_calls_preserves_order():
    chunks = iter(
        [
            StreamChunk(tool_calls=[{"index": 0, "id": "c1", "name": "t1", "arguments": '{"a"'}]),
            StreamChunk(tool_calls=[{"index": 1, "id": "c2", "name": "t2", "arguments": '{"x"'}]),
            StreamChunk(tool_calls=[{"index": 0, "id": None, "name": None, "arguments": ": 1}"}]),
            StreamChunk(tool_calls=[{"index": 1, "id": None, "name": None, "arguments": ": 2}"}]),
        ]
    )
    reply = merge_stream_chunks(chunks)

    assert [tc["id"] for tc in reply.tool_calls] == ["c1", "c2"]   # 首现顺序保序
    assert reply.tool_calls[0]["arguments"] == '{"a": 1}'
    assert reply.tool_calls[1]["arguments"] == '{"x": 2}'


def test_merge_takes_usage_from_last_chunk():
    chunks = iter(
        [
            StreamChunk(content="第一句", usage=None),
            StreamChunk(content="。", usage={"prompt_tokens": 7, "completion_tokens": 2}),
        ]
    )
    reply = merge_stream_chunks(chunks)
    assert reply.usage == {"prompt_tokens": 7, "completion_tokens": 2}


def test_merge_empty_stream_gives_plain_reply():
    reply = merge_stream_chunks(iter([]))
    assert reply.content == "" and reply.tool_calls is None and reply.usage is None


# ---------- RobustLLM 流式衣 ----------

def test_robust_stream_retries_only_before_first_chunk():
    ledger = UsageLedger()
    inner = StreamingFake(mode="open_fail_once")
    llm = RobustLLM(inner, ledger, GatewayConfig(retries=2, backoff=0.0))

    reply = merge_stream_chunks(_ask_stream(llm, "你好"))

    assert reply.content == "你好！"     # 第一块前的失败被重试救回
    assert ledger.llm_retries == 1
    assert ledger.llm_calls == 1
    assert ledger.tokens_in == 10 and ledger.tokens_out == 3   # usage 末班车入账


def test_robust_stream_mid_failure_propagates_without_retry():
    ledger = UsageLedger()
    inner = StreamingFake(mode="mid_fail")
    llm = RobustLLM(inner, ledger, GatewayConfig(retries=2, backoff=0.0))

    chunks = _ask_stream(llm, "你好")
    got = [next(chunks).content, next(chunks).content]
    with pytest.raises(ConnectionError):
        next(chunks)   # 第三块：流中途断线

    assert got == ["你", "好"]          # 已经外流的两块收不回
    assert inner.stream_starts == 1     # 中途失败绝不重新召唤（重试=重复说话）
    assert ledger.llm_failures == 1


def test_robust_stream_breaker_opens_and_fails_fast():
    inner = StreamingFake(mode="always_fail")
    llm = RobustLLM(inner, UsageLedger(), GatewayConfig(retries=0, fail_threshold=1, cooldown_seconds=60))

    with pytest.raises(ConnectionError):
        list(_ask_stream(llm, "你好"))
    assert inner.stream_starts == 1

    with pytest.raises(CircuitOpenError):   # 熔断打开 → 不再撞墙
        list(_ask_stream(llm, "你好"))
    assert inner.stream_starts == 1


def test_robust_stream_reads_cache_as_single_chunk():
    ledger = UsageLedger()
    inner = EchoCountingLLM()
    llm = RobustLLM(inner, ledger, GatewayConfig(retries=0))

    llm.generate([Message(role="user", content="你好")])   # 非流写入缓存

    chunks = list(_ask_stream(llm, "你好"))
    assert len(chunks) == 1                                  # 伪流：命中整块吐出
    assert chunks[0].content == "echo:你好"
    assert ledger.llm_cache_hits == 1
    assert inner.attempts == 1                               # 没触网


# ---------- FallbackLLM 流式降级 ----------

def test_fallback_stream_switches_before_first_chunk(capsys):
    ledger = UsageLedger()
    chain = FallbackLLM(
        [
            RobustLLM(StreamingFake(mode="always_fail"), ledger, GatewayConfig(retries=0)),
            RobustLLM(StreamingFake(mode="ok"), ledger, GatewayConfig(retries=0)),
        ],
        ledger,
    )
    reply = merge_stream_chunks(_ask_stream(chain, "你好"))
    assert reply.content == "你好！"
    assert "[降级]" in capsys.readouterr().out   # 诚实声明照旧


def test_fallback_stream_locks_candidate_after_first_chunk(capsys):
    ledger = UsageLedger()
    spare = StreamingFake(mode="ok")
    chain = FallbackLLM(
        [
            RobustLLM(StreamingFake(mode="mid_fail"), ledger, GatewayConfig(retries=0)),
            RobustLLM(spare, ledger, GatewayConfig(retries=0)),
        ],
        ledger,
    )
    chunks = _ask_stream(chain, "你好")
    got = [next(chunks).content, next(chunks).content]
    with pytest.raises(ConnectionError):
        next(chunks)

    assert got == ["你", "好"]
    assert spare.stream_starts == 0           # 字已外流，备胎进场会把前半段吐两遍
    assert "[降级]" not in capsys.readouterr().out


# ---------- SemanticCacheLLM 流式语义档 ----------

def _semantic_chain(threshold=0.92):
    ledger = UsageLedger()
    inner = EchoCountingLLM()
    llm = SemanticCacheLLM(
        RobustLLM(inner, ledger, GatewayConfig(retries=0)),
        FakeEmbedder(),
        ledger,
        threshold,
    )
    return llm, inner, ledger


def test_semantic_stream_hit_replays_single_chunk():
    llm, inner, ledger = _semantic_chain()
    list(_ask_stream(llm, "PHP 是什么"))

    chunks = list(_ask_stream(llm, "PHP 是啥"))   # 近义查询 → 命中

    assert len(chunks) == 1
    assert chunks[0].content == "echo:PHP 是什么"
    assert ledger.llm_cache_hits == 1
    assert inner.attempts == 1


def test_semantic_stream_miss_stores_for_next_hit():
    llm, inner, ledger = _semantic_chain()
    list(_ask_stream(llm, "Python 是什么"))       # miss：转发流，流耗尽拼回入库

    chunks = list(_ask_stream(llm, "Python 是什么"))   # 同问再来 → 命中
    assert chunks[0].content == "echo:Python 是什么"
    assert ledger.llm_cache_hits == 1
    assert inner.attempts == 1                   # 流式 miss 同样积累条目