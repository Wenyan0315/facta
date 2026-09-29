"""S2b run_turn 内核验收：协作式取消（should_cancel）的三个检查点。

不变量：取消与模型不可用同属「本轮无产出」——都要掐掉半截工具轮、
user 消息留底片、返回 None。差别只在 cancelled 是 Run 级终态（不发事件），
error 已由内核发事件。
"""

from agent.core.jev import RouteDecision
from agent.core.llm import LLM, ScriptedLLM, StreamChunk
from agent.core.types import Message
from agent.memory.store import Session
from agent.orchestrator.agent import Agent
from agent.orchestrator.loop import (
    RunResult,
    _is_dsml_leak,
    _merge_with_leak_guard,
    _salvage_dsml_leak,
    run_turn,
)
from agent.tools.registry import Tool, ToolRegistry


def _bare_agent() -> Agent:
    """无菜单 agent：原 run_turn(registry=None) 的等价物（S5a）。"""
    return Agent(name="test", system_prompt="sys", registry=ToolRegistry())


class _SlowStreamLLM(LLM):
    """吐三块的假流：专测检查点③（流式生成中途取消）。

    closed 标记在 finally 里置位——验证取消时底层生成器被主动 close
    （真实实现里这一步负责收连接，不再等 GC）。
    """

    name = "slow_stream"

    def __init__(self) -> None:
        self.closed = False

    def generate(self, messages, tools=None):
        return Message(role="assistant", content="不该走到这里")

    def generate_stream(self, messages, tools=None):
        try:
            yield StreamChunk(content="第一块 ")
            yield StreamChunk(content="第二块 ")
            yield StreamChunk(content="第三块")
        finally:
            self.closed = True


def test_should_cancel_immediately_keeps_user_and_returns_none():
    # 检查点①（首次模型调用前）就取消 → 不调模型、返回 CANCELLED、user 留底片
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    result, reply = run_turn(
        session, "嗨", llm=ScriptedLLM([]), agent=_bare_agent(),
        should_cancel=lambda: True,
    )

    assert result is RunResult.CANCELLED
    assert reply is None
    assert [m.role for m in session.messages] == ["system", "user"]   # 无假回复


def test_should_cancel_mid_round_trims_half_tool_round():
    # 第一轮点菜 → 检查点②（工具执行前）取消 → 半截工具轮被掐掉
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            {"id": "call_1", "name": "get_current_time", "arguments": "{}"}
        ]),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    # 检查点① 放行（False）、检查点② 取消（True）
    should_cancel = iter([False, True]).__next__

    result, reply = run_turn(
        session, "现在几点", llm=llm, agent=_bare_agent(),
        should_cancel=should_cancel,
    )

    assert result is RunResult.CANCELLED
    assert reply is None
    # trim 掐掉「没配工具结果的点菜消息」，孤儿 tool 不落盘
    assert [m.role for m in session.messages] == ["system", "user"]


def test_should_cancel_mid_stream_closes_underlying_generator():
    # 检查点③（流式中途）：第一块到手后取消 → 底层流被 close、只外发
    # 取消前的块、掐半截轮、返回 CANCELLED——模型长生成不必等自然边界
    llm = _SlowStreamLLM()
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    texts: list[str] = []
    # 检查点①放行 + 第一块检查放行，第二块检查时取消
    should_cancel = iter([False, False, True]).__next__

    result, reply = run_turn(
        session, "讲个长故事", llm=llm, agent=_bare_agent(),
        on_text=texts.append,
        should_cancel=should_cancel,
    )

    assert result is RunResult.CANCELLED
    assert reply is None
    assert texts == ["第一块 "]                        # 只收到取消前的块
    assert llm.closed is True                          # 底层生成器被主动 close
    assert [m.role for m in session.messages] == ["system", "user"]


# ---------- DSML 泄漏守卫（S6c 实机验收抓到的模型格式故障） ----------

# 实机样本还原（deepseek-flash）：内部函数调用格式裸文本吐进 content，
# 全角竖线——ScriptedLLM 时代测不出，实机编排链验收抓到
_DSML_SAMPLE = '<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="finish_plan">'


def _leak_agent() -> Agent:
    """带一个真工具的 agent：泄漏 → 重试 → 改走标准点菜的链路需要真菜单。"""
    reg = ToolRegistry()
    reg.register(Tool(
        name="get_current_time", description="", parameters={},
        func=lambda: "2026-09-24 12:00",
    ))
    return Agent(name="test", system_prompt="sys", registry=reg)


def test_dsml_leak_once_retries_then_recovers():
    # 实机场景（S6c 验收）：泄漏 → 泄漏消息不入底片、提示注入投影重试
    # → 模型改说人话 → 正常收尾
    llm = ScriptedLLM([
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content="好的，任务已收官。"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    texts: list[str] = []
    result, reply = run_turn(
        session, "收官", llm=llm, agent=_leak_agent(), on_text=texts.append,
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content == "好的，任务已收官。"
    # 泄漏消息不入底片：system/user/最终回答，中间没有坏消息的坑位
    assert [m.role for m in session.messages] == ["system", "user", "assistant"]
    # 重试轮投影尾部带泄漏提示（ScriptedLLM.calls 快照可断言「模型看到了什么」）
    assert len(llm.calls) == 2
    hint = llm.calls[1][-1]
    assert hint.role == "system" and "调用格式" in hint.content
    # 流式输出诚实告知用户正在重试（已吐出的泄漏块收不回，但用户不懵）
    assert any("自动重试" in t for t in texts)


def test_dsml_leak_retry_recovers_to_tool_calls():
    # 泄漏后的重试轮里模型改走标准 tool_calls——工具调用意图接回来，
    # 链路继续（这正是实机故障现场：finish_plan 意图丢失导致计划未收官）
    llm = ScriptedLLM([
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "get_current_time", "arguments": "{}"},
        ]),
        Message(role="assistant", content="当前时间是 2026-09-24 12:00。"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    result, reply = run_turn(session, "几点了", llm=llm, agent=_leak_agent())

    assert result is RunResult.COMPLETED
    assert reply is not None and "12:00" in reply.content
    # 底片：泄漏零坑位，点菜→工具→回答链路完整
    assert [m.role for m in session.messages] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert all("DSML" not in (m.content or "") for m in session.messages)


def test_dsml_leak_persists_degrades_honestly():
    # 1 次 + 2 次重试全是泄漏 → 没有第 4 次调用：超限降级（ADR 047 甲案，
    # 推翻 033 的「原样吐出」）——markup 截掉、明确告知哪个工具没执行成，
    # 诚实降级仍成立，但用户不必再从裸格式串里刨内容
    llm = ScriptedLLM([
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content=_DSML_SAMPLE),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    texts: list[str] = []
    result, reply = run_turn(
        session, "再试", llm=llm, agent=_leak_agent(), on_text=texts.append,
    )

    assert result is RunResult.COMPLETED
    assert len(llm.calls) == 3            # 上限即停：1 + 2 次重试
    assert reply is not None
    assert not _is_dsml_leak(reply)       # 降级后的消息不再是泄漏形态
    assert "DSML" not in reply.content    # 裸标记一个字都不留
    assert "finish_plan" in reply.content  # 明说丢失的工具名
    assert "输出格式故障" in reply.content
    assert any("输出格式故障" in t for t in texts)   # 流式侧同样被告知
    # user 之后直接是降级回答——泄漏轮的坏消息一条都不入史
    assert [m.role for m in session.messages] == ["system", "user", "assistant"]


class _DirectRouter:
    """route() 恒 direct：M10 把上下文依赖的单词跟话判成纯聊天的实机形状。

    066 案发还原（2026-09-29 会话 20260929-095616）：「今天天气怎样」→
    模型答完问「告诉我城市名」→「上海」被路由判 direct → tools=None。
    """

    def route(self, user_text: str) -> RouteDecision:
        return RouteDecision(kind="direct")


def test_dsml_leak_with_none_menu_escalates_retry_menu():
    # 066：direct 路由（tools=None）下泄漏——重试必须升全量菜单，否则 hint
    # 说的「通过标准 tool_calls 字段发起」结构上不存在（056 收尾段同款
    # 死锁的第二位置）。升级后模型点菜 → 工具真执行，不是孤儿 tool_calls
    llm = ScriptedLLM([
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "get_current_time", "arguments": "{}"},
        ]),
        Message(role="assistant", content="当前时间是 2026-09-24 12:00。"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(
        name="test", system_prompt="sys",
        registry=_leak_agent().registry, router=_DirectRouter(),
    )

    result, reply = run_turn(session, "上海", llm=llm, agent=agent)

    assert result is RunResult.COMPLETED
    assert reply is not None and "12:00" in reply.content
    # 首调无菜单（路由 direct）；泄漏重试升到全量——模型这才点得了菜
    assert llm.tool_menus[0] is None
    assert [s["function"]["name"] for s in (llm.tool_menus[1] or [])] == ["get_current_time"]
    assert [m.role for m in session.messages] == [
        "system", "user", "assistant", "tool", "assistant",
    ]


def test_dsml_leak_with_menu_retries_same_menu():
    # 066 只升 None：有菜单时（全量或窄）泄漏重试沿用同一份菜单——
    # 已有 tools 字段承载点菜，056 的菜单收窄设计不被推翻
    llm = ScriptedLLM([
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content="好的，任务已收官。"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    run_turn(session, "收官", llm=llm, agent=_leak_agent())

    assert llm.tool_menus[0] == llm.tool_menus[1]   # 同一份菜单重投，不升级


def test_merge_with_leak_guard_without_fallback_keeps_none():
    # fallback 缺省＝旧行为：tools=None 的重试仍是 None——收尾段 final
    # 调用点依赖此语义（它的 tool_calls 无执行路径，升了会造孤儿）
    llm = ScriptedLLM([
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content=_DSML_SAMPLE),
    ])

    out = _merge_with_leak_guard(
        llm, [Message(role="system", content="sys")], None, None, None,
    )

    assert llm.tool_menus == [None, None, None]   # 1 + 2 次重试全程无菜单
    assert not _is_dsml_leak(out)                 # 超限降级（047）


def test_close_out_none_menu_leak_escalates_but_final_does_not():
    # 收尾段两个调用点的 066 落点：无活跃计划时 closing=None → 泄漏升全量；
    # 收官菜跑完后的 final 调用恒不给 fallback（孤儿风险）。max_rounds=1
    # 逼出 _close_out 路径
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[          # 轮 1：点菜
            {"id": "c1", "name": "get_current_time", "arguments": "{}"},
        ]),
        Message(role="assistant", content=_DSML_SAMPLE),           # 收尾调 1：泄漏
        Message(role="assistant", content="", tool_calls=[         # 升级后重试：又点菜
            {"id": "c2", "name": "get_current_time", "arguments": "{}"},
        ]),
        Message(role="assistant", content="收官总结。"),             # final：文字
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(
        name="test", system_prompt="sys",
        registry=_leak_agent().registry, max_tool_rounds=1,
    )

    result, reply = run_turn(session, "干个活", llm=llm, agent=agent)

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content == "收官总结。"
    # 菜单序列：轮 1 全量 → 收尾调 1 无（closing=None，无计划）→
    # 泄漏重试升全量 → final 无（撤菜单，刻意不升）
    assert llm.tool_menus[0] is not None
    assert llm.tool_menus[1] is None
    assert llm.tool_menus[2] is not None
    assert llm.tool_menus[3] is None
    # 升级后点的菜真执行了：两次点菜两次工具结果，无孤儿 tool_calls
    assert [m.role for m in session.messages] == [
        "system", "user", "assistant", "tool", "assistant", "tool", "assistant",
    ]


# ---------- 降级改写本身（ADR 047，对着落盘的真实样本设计） ----------

# 三条真实样本（截自 data/evals/frozen-2026*.json，正文按需截短、markup 结构
# 逐字保留）。选它们是因为泄漏有两型，修法必须同时成立：
# 纯 markup 型（7/8 条）与混合型（1/8 条，前半是完好的自然语言回答）。
_LEAK_FINISH_PLAN = (
    '<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="finish_plan">\n'
    '<｜｜DSML｜｜ parameter name="summary" string="true">'
    '三步全部完成：①当前时间 2026-09-26 15:11:54（周六）；'
    '②已添加待办 #1「记录当前时间」；③笔记库中 RAG 相关文件名只有 RAG.md。'
    '</｜｜DSML｜｜ parameter>\n'
    '</｜｜DSML｜｜ invoke>\n'
    '</｜｜DSML｜｜ calls>'
)
_LEAK_EMPTY_PARAMS = (
    '<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="list_notes">\n\n'
    '</｜｜DSML｜｜ invoke>\n'
    '</｜｜DSML｜｜ calls>'
)
_MIXED_PROSE = (
    "数据齐了，我算一下两地比价，然后写笔记。\n\n"
    "| 日期 | 港股 09988 收盘(港元) | 涨跌 |\n|---|---|---|\n"
    "| 9/25 周五 | 108.40 | −1.45% |\n\n"
    "周变动：港股自 9/18 收盘口径全周约 **−1.35%**。\n\n"
    "现在写入笔记：\n\n"
)
_LEAK_MIXED = _MIXED_PROSE + (
    '<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="write_note">\n'
    '<｜｜DSML｜｜ parameter name="path" string="true">阿里巴巴股价分析.md'
    '</｜｜DSML｜｜ parameter>\n'
    '<｜｜DSML｜｜ parameter name="content" string="true">'
    '# 阿里巴巴股价分析\n\n【免责声明】本文不构成投资建议。'
    '</｜｜DSML｜｜ parameter>\n'
    '</｜｜DSML｜｜ invoke>\n'
    '</｜｜DSML｜｜ calls>'
)


def test_salvage_pure_markup_leaves_only_notice():
    # 纯 markup 型（真实样本 7/8 条）：正文全在未执行的调用参数里，
    # 截断后只剩告知——丢的是「没做成的事的参数」，不是交付物
    out = _salvage_dsml_leak(
        Message(role="assistant", content=_LEAK_FINISH_PLAN), None
    )

    assert not _is_dsml_leak(out)
    assert out.content.startswith("【输出格式故障】")
    assert "finish_plan" in out.content
    # 参数里的 summary 正文不得冒充交付物（那是没执行成的收官陈述）
    assert "三步全部完成" not in out.content


def test_salvage_mixed_type_preserves_prose_verbatim():
    # 混合型（真实样本 frozen-20260926T061616Z r1）：前半完好的行情表必须
    # 逐字保住——一刀切截断会把局部故障升级成整轮零交付
    out = _salvage_dsml_leak(Message(role="assistant", content=_LEAK_MIXED), None)

    assert out.content.startswith(_MIXED_PROSE.rstrip())
    assert "108.40" in out.content and "−1.35%" in out.content
    assert not _is_dsml_leak(out)
    assert "write_note" in out.content
    # 没写进库的笔记正文不能留在回答里冒充交付物（这就是乙案被否的理由）
    assert "免责声明" not in out.content
    assert "阿里巴巴股价分析.md" not in out.content


def test_salvage_empty_params_still_names_tool():
    # 空参调用（真实样本 list_notes）：提不到参数也要能报出工具名
    out = _salvage_dsml_leak(
        Message(role="assistant", content=_LEAK_EMPTY_PARAMS), None
    )

    assert "list_notes" in out.content
    assert not _is_dsml_leak(out)


def test_salvage_dedups_repeated_tool_names():
    # 同一批泄漏里同名工具多次调用（真实样本：web_search ×2）只报一次
    two = _LEAK_EMPTY_PARAMS.replace("list_notes", "web_search")
    out = _salvage_dsml_leak(
        Message(role="assistant", content=two + "\n" + two), None
    )

    assert out.content.count("web_search") == 1


def test_salvage_keeps_usage_and_emits_notice_to_stream():
    # replace 而非原地改：token 账目要跟着走；告知经 on_text 外发，
    # 流式视图与落盘至少「都说明了故障」
    texts: list[str] = []
    reply = Message(
        role="assistant", content=_LEAK_FINISH_PLAN, usage={"total_tokens": 42}
    )

    out = _salvage_dsml_leak(reply, texts.append)

    assert out.usage == {"total_tokens": 42}
    assert reply.content == _LEAK_FINISH_PLAN      # 原消息没被悄悄改写
    assert texts and "输出格式故障" in texts[0]
    assert all("DSML" not in t for t in texts)     # 告知不含标记字面量


def test_salvage_notice_never_contains_leak_markers():
    # 拍板 4：告知里写了标记字面量，落盘记录会被自动化扫描再判成泄漏
    for sample in (_LEAK_FINISH_PLAN, _LEAK_MIXED, _LEAK_EMPTY_PARAMS):
        out = _salvage_dsml_leak(Message(role="assistant", content=sample), None)
        assert "｜｜DSML｜｜" not in out.content


def test_dsml_halfwidth_lookalike_not_treated_as_leak():
    # 半角仿制品（||DSML||）不触发守卫：标记精确匹配全角 ｜｜DSML｜｜，
    # 普通回答里提到 DSML 字样不该被拦截重试
    llm = ScriptedLLM([
        Message(role="assistant", content="||DSML|| 只是回答正文里的普通字样。"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    result, reply = run_turn(session, "说说 DSML", llm=llm, agent=_leak_agent())

    assert result is RunResult.COMPLETED
    assert len(llm.calls) == 1            # 一次调用即收尾：没当泄漏
    assert reply is not None and "普通字样" in reply.content


# ---------- P0-6 无进展检测（LongHorizon 基线 agent 原地重试 400+ 步的形态） ----------


def _ordering(batch_id: str) -> Message:
    """一批点菜：同名同参数（stuck 签名只看名字+参数，id 不算数）。"""
    return Message(role="assistant", content="", tool_calls=[
        {"id": batch_id, "name": "get_current_time", "arguments": "{}"}
    ])


def test_stuck_detection_breaks_identical_tool_batches():
    # 连续 3 批完全相同的点菜 → 第 3 批触发熔断（默认上限 3，streak>=2）
    llm = ScriptedLLM([_ordering("call_1"), _ordering("call_2"), _ordering("call_3")])
    # 剧本耗尽后 ScriptedLLM 兜底纯文本——正好当强制收尾的最终回答
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    events: list[tuple[str, dict]] = []

    result, reply = run_turn(
        session, "现在几点", llm=llm, agent=_bare_agent(),
        on_event=lambda t, d: events.append((t, d)),
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content          # 兜底文本收尾
    assert ("stuck", {"tools": ["get_current_time"]}) in events
    assert all(t != "max_rounds" for t, _ in events)    # 熔断已自带事件，不再发噪声
    # 熔断判定在入史之前：第 3 批点菜不进底片（无孤儿 tool_calls），
    # 前 2 批各配一条 tool 结果，最后是强制收尾的 assistant 回答
    assert [m.role for m in session.messages] == [
        "system", "user", "assistant", "tool", "assistant", "tool", "assistant",
    ]
    assert len(llm.calls) == 4                          # 3 批点菜 + 1 次强制收尾


def test_single_repeat_does_not_trip_stuck():
    # 误报空间：只重复 1 次（streak=1 < 上限-1）不熔断，模型第 3 轮正常收尾
    llm = ScriptedLLM([
        _ordering("call_1"),
        _ordering("call_2"),
        Message(role="assistant", content="好了不查了"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    events: list[tuple[str, dict]] = []

    result, reply = run_turn(
        session, "现在几点", llm=llm, agent=_bare_agent(),
        on_event=lambda t, d: events.append((t, d)),
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content == "好了不查了"
    assert all(t not in ("stuck", "max_rounds") for t, _ in events)
