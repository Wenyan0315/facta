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
from agent.tools.registry import ToolRegistry


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
