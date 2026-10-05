"""M6 压缩层的回归测试：把「验收五轮 saga」踩过的坑固化成断言。

每一条测试都对应一次真实的翻车，不是为了凑覆盖率——
修过一次就再也不踩。（devix 评审：问题 2「测试节奏完全缺位」）
"""

from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.compressor import (
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


# ── 073 摘要决定节：程序校验（宽容方向——缺失只 warning 照用不阻断） ──


def _overdue_messages() -> list[Message]:
    """攒一批窗口外积压（触发滚动摘要的量）。"""
    messages = [Message(role="system", content="人设")]
    for i in range(20):
        messages.append(user(f"问{i}"))
        messages.append(assistant(f"答{i}"))
    return messages


def test_summary_with_decision_section_no_warning(caplog):
    """摘要带决定节：零 warning，小节文本随摘要原样进投影（结构化视野增益）。"""
    import logging as _logging

    good = "会话内容摘要。\n【关键决定与约束】用户要求改用闪存档。"
    llm = ScriptedLLM([assistant(good)])
    with caplog.at_level(_logging.WARNING):
        summary, upto = maybe_compress(llm, _overdue_messages(), None, 1)
    assert summary == good
    assert "摘要决定节缺失" not in caplog.text


def test_summary_missing_decision_section_warns_but_passes(caplog):
    """摘要缺决定节：warning 观测信号，但照用不阻断（不为格式漂移付重试 token——
    062 收官回验同款宽容方向；升级前落盘的旧缓存重组一次即自愈）。"""
    import logging as _logging

    bare = "会话内容摘要。"
    llm = ScriptedLLM([assistant(bare)])
    with caplog.at_level(_logging.WARNING):
        summary, upto = maybe_compress(llm, _overdue_messages(), None, 1)
    assert summary == bare                 # 摘要照用，不重生成
    assert "摘要决定节缺失" in caplog.text   # 但留下了可观测信号


# ── 077 决定节轨迹：撤回/取代状态只增不减（防「落了又洗」） ──


def test_retraction_track_lost_in_rolling_merge_warns(caplog):
    """ADR 077 主验收：旧摘要决定节已落「被取代」轨迹，新摘要把它洗掉——
    warning 观测 + 照用不阻断（对账纪律的程序兑现，只防「落了又洗」）。"""
    import logging as _logging

    old = "会话摘要。\n【关键决定与约束】\n被取代：闪存档 → chat 档\n已撤回：周报自动生成"
    washed = "会话摘要。\n【关键决定与约束】\n有效：用 chat 档"   # 轨迹整体被洗
    llm = ScriptedLLM([assistant(washed)])
    with caplog.at_level(_logging.WARNING):
        summary, _ = maybe_compress(llm, _overdue_messages(), old, 1)
    assert summary == washed                     # 宽容：照用不阻断
    assert "决定节轨迹丢失" in caplog.text        # 洗掉的两个轨迹都报
    assert "被取代" in caplog.text and "已撤回" in caplog.text


def test_retraction_track_kept_is_silent(caplog):
    """轨迹被滚动合并如实保留（对账纪律生效的正向路径）：零 warning。"""
    import logging as _logging

    old = "会话摘要。\n【关键决定与约束】\n被取代：闪存档 → chat 档"
    kept = "新摘要。\n【关键决定与约束】\n有效：用 chat 档\n被取代：闪存档 → chat 档"
    llm = ScriptedLLM([assistant(kept)])
    with caplog.at_level(_logging.WARNING):
        summary, _ = maybe_compress(llm, _overdue_messages(), old, 1)
    assert summary == kept
    assert "决定节轨迹丢失" not in caplog.text


def test_retraction_track_newly_appeared_is_silent(caplog):
    """轨迹无中生有（本批对话刚发生改主意）：合法新增，不是「丢失」。"""
    import logging as _logging

    old = "会话摘要。\n【关键决定与约束】\n有效：用闪存档"
    fresh_track = "新摘要。\n【关键决定与约束】\n被取代：闪存档 → chat 档\n有效：用 chat 档"
    llm = ScriptedLLM([assistant(fresh_track)])
    with caplog.at_level(_logging.WARNING):
        summary, _ = maybe_compress(llm, _overdue_messages(), old, 1)
    assert summary == fresh_track
    assert "决定节轨迹丢失" not in caplog.text


def test_summary_prompt_carries_retraction_discipline():
    """对账纪律与三段式必须长在 prompt 里（防后续改 prompt 顺手洗掉指令）：
    段头词、逐条落位纪律、「洗成曾有个计划」的反面教材锚点。"""
    from facta.memory.compressor import _SUMMARY_PROMPT

    for anchor in ("被取代：", "已撤回：", "不许静默消失", "曾讨论过该计划"):
        assert anchor in _SUMMARY_PROMPT, f"prompt 丢了 077 对账纪律锚点：{anchor}"


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
