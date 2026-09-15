"""S2b run_turn 内核验收：协作式取消（should_cancel）的两个检查点。

不变量：取消与模型不可用同属「本轮无产出」——都要掐掉半截工具轮、
user 消息留底片、返回 None。差别只在 cancelled 是 Run 级终态（不发事件），
error 已由内核发事件。
"""

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.store import Session
from agent.orchestrator.loop import run_turn


def test_should_cancel_immediately_keeps_user_and_returns_none():
    # 检查点①（首次模型调用前）就取消 → 不调模型、返回 None、user 留底片
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    reply = run_turn(
        session, "嗨", llm=ScriptedLLM([]), registry=None,
        should_cancel=lambda: True,
    )

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

    reply = run_turn(
        session, "现在几点", llm=llm, registry=None,
        should_cancel=should_cancel,
    )

    assert reply is None
    # trim 掐掉「没配工具结果的点菜消息」，孤儿 tool 不落盘
    assert [m.role for m in session.messages] == ["system", "user"]
