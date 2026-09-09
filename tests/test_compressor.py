"""M6 压缩层的回归测试：把「验收五轮 saga」踩过的坑固化成断言。

每一条测试都对应一次真实的翻车，不是为了凑覆盖率——
修过一次就再也不踩。（devix 评审：问题 2「测试节奏完全缺位」）
"""

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.compressor import (
    build_payload,
    maybe_compress,
    trim_incomplete_round,
    window_start,
)


def user(text: str) -> Message:
    return Message(role="user", content=text)


def assistant(text: str) -> Message:
    return Message(role="assistant", content=text)


def call_msg(tool_name: str = "get_current_time", call_id: str = "t1") -> Message:
    """造一条「点菜」消息：assistant + tool_calls（content 为空、只点菜）。"""
    return Message(
        role="assistant",
        content="",
        tool_calls=[{"id": call_id, "name": tool_name, "arguments": "{}"}],
    )


def tool_result(text: str, call_id: str = "t1") -> Message:
    return Message(role="tool", content=text, tool_call_id=call_id)


# ── 窗口边界 ──────────────────────────────────────────────

def test_window_start_lands_on_user():
    """铁律①：窗口左边界必须落在 user 消息上，不能停在 tool / 点菜 assistant。"""
    messages = [
        Message(role="system", content="人设"),
        user("问题"),
        call_msg(),
        tool_result("结果"),
        assistant("回答"),
        user("新问题"),
        assistant("答"),
    ]
    start = window_start(messages, keep_last=3)
    assert messages[start].role == "user"


def test_window_start_extends_to_nearest_user():
    """孤儿防御：窗口内一条 user 都没有（工具轮超长），必须向左扩到最近的 user。

    若返回窗口内的 tool / 点菜 assistant，就会把 tool 消息和它的 tool_calls 爹
    切分家——孤儿的 tool 消息会被 API 400 拒收。"""
    messages = [
        Message(role="system", content="人设"),
        user("帮我查"),
        call_msg("f", "a"),
        tool_result("结果1", "a"),
        call_msg("g", "b"),
        tool_result("结果2", "b"),
    ]
    start = window_start(messages, keep_last=2)
    assert start == 1
    assert messages[start].role == "user"


# ── 覆盖不变量 ────────────────────────────────────────────

def test_build_payload_no_dead_zone():
    """覆盖不变量：没进摘要的消息，绝不能从视野里消失（死区防御）。

    暗号这条 user/assistant 落在「窗口外、又还没被摘要覆盖」的死区——
    第一版没有 `start = min(window_start, summarized_upto)`，暗号掉进去
    模型就看不见原文，当场编了个假暗号（幻觉标准机制）。"""
    messages = [
        Message(role="system", content="人设"),
        user("早"),
        assistant("早"),
        user("暗号=海星"),     # 3 ← 死区候选：既不 < summarized_upto(3)，也不在窗口内
        assistant("记住了"),   # 4
        user("最近这句"),      # 5
        assistant("收到"),     # 6
    ]
    summary, upto = "摘要盖到前两条对话", 3
    # keep_last=2 把窗口缩到最右，逼出「窗口外还没进摘要」的死区
    payload = build_payload(messages, summary, upto, keep_last=2)
    for i in range(upto, len(messages)):
        assert messages[i] in payload, f"第 {i} 条原文掉进死区、模型看不见了"


# ── 触发缓存 ──────────────────────────────────────────────

def test_maybe_compress_reuses_cache_below_threshold():
    """触发策略：窗口外积压 < margin 时，一次内部 LLM 调用都不该发生。"""
    llm = ScriptedLLM([assistant("[摘要]")])
    messages = [
        Message(role="system", content="人设"),
        user("a"),
        assistant("b"),
        user("c"),
        assistant("d"),
    ]
    summary, upto = maybe_compress(llm, messages, None, 1)
    assert summary is None
    assert upto == 1
    assert len(llm.calls) == 0   # 没到阈值：零 token、零调用


def test_maybe_compress_triggers_and_advances():
    """到了阈值才付一次 LLM 调用，并推进覆盖游标（滚动而非全量重来）。"""
    llm = ScriptedLLM([assistant("新摘要内容")])
    messages = [Message(role="system", content="人设")]
    for i in range(20):
        messages.append(user(f"问{i}"))
        messages.append(assistant(f"答{i}"))
    summary, upto = maybe_compress(llm, messages, None, 1)
    assert summary == "新摘要内容"
    assert upto > 1
    assert len(llm.calls) == 1


# ── 孤儿清理 ──────────────────────────────────────────────

def test_trim_incomplete_round_removes_orphan():
    """Ctrl+C 落在「点菜已入史、结果未回填」的瞬间：掐掉孤儿工具轮。"""
    messages = [
        Message(role="system", content="人设"),
        user("问"),
        call_msg("f", "a"),
        tool_result("结果", "a"),
        call_msg("g", "b"),   # 尾部：点了菜但结果没回来 → 孤儿
    ]
    result = trim_incomplete_round(messages)
    assert result[-1].role == "user"
    assert len(result) == 2


def test_trim_incomplete_round_keeps_complete():
    """完整轮次应原样保留，不被误掐。"""
    messages = [
        Message(role="system", content="人设"),
        user("问"),
        assistant("答"),
    ]
    assert len(trim_incomplete_round(messages)) == 3


# ── ScriptedLLM 能力（P0-2）───────────────────────────────

def test_scripted_llm_produces_tool_calls():
    """假模型能按脚本返回 tool_calls——工具链路自此可以离线验证。"""
    script = [
        call_msg("get_current_time", "t1"),
        assistant("最终回答"),
    ]
    llm = ScriptedLLM(script)
    msgs = [user("现在几点")]
    first = llm.generate(msgs)
    assert first.tool_calls == [
        {"id": "t1", "name": "get_current_time", "arguments": "{}"}
    ]
    second = llm.generate(msgs)
    assert second.content == "最终回答"
    assert len(llm.calls) == 2