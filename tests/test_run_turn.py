"""S2b run_turn 内核验收：协作式取消（should_cancel）的三个检查点。

不变量：取消与模型不可用同属「本轮无产出」——都要掐掉半截工具轮、
user 消息留底片、返回 None。差别只在 cancelled 是 Run 级终态（不发事件），
error 已由内核发事件。
"""

from agent.core.llm import LLM, ScriptedLLM, StreamChunk
from agent.core.types import Message
from agent.memory.store import Session
from agent.orchestrator.agent import Agent
from agent.orchestrator.loop import RunResult, run_turn
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
    # 1 次 + 2 次重试全是泄漏 → 没有第 4 次调用：超限按普通回答
    # 诚实降级——泄漏文本原样入史（不装没事），用户至少看到真实输出
    llm = ScriptedLLM([
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content=_DSML_SAMPLE),
        Message(role="assistant", content=_DSML_SAMPLE),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    result, reply = run_turn(session, "再试", llm=llm, agent=_leak_agent())

    assert result is RunResult.COMPLETED
    assert len(llm.calls) == 3            # 上限即停：1 + 2 次重试
    assert reply is not None and reply.content == _DSML_SAMPLE
    # user 之后直接是降级回答——泄漏轮的坏消息一条都不入史
    assert [m.role for m in session.messages] == ["system", "user", "assistant"]


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
