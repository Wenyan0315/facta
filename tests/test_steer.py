"""ADR 115（M1）steering 验收：信箱 / 内核拉缝 / 收尾段兜底 / 轮后判定。

steering 的本质是「纠偏不杀 Run」——与取消的分界线贯穿全部用例：
取消 return，纠偏继续本轮。
"""

from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.store import Session
from facta.orchestrator.agent import Agent
from facta.orchestrator.loop import RunResult, run_turn
from facta.orchestrator.task import (
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_WAITING_STEER,
    Task,
)
from facta.server.app import _drop_leftover_steer, _task_verdict
from facta.server.run_store import STATUS_COMPLETED, STATUS_FAILED, Run
from facta.tools.registry import ToolRegistry


def _agent() -> Agent:
    return Agent(name="test", system_prompt="sys", registry=ToolRegistry())


# ---- Run.steer_pending 信箱（累积式，取空原子）----


def test_steer_mailbox_accumulates():
    run = Run(run_id="r1", session_id="s1")
    assert run.request_steer("第一条纠偏") is True
    assert run.request_steer("第二条纠偏") is True
    # 累积而非覆盖：两次纠偏都得进上下文
    assert run.take_steer() == "第一条纠偏\n第二条纠偏"
    assert run.take_steer() is None   # 取空后再取给 None


def test_steer_rejected_on_terminal_and_empty():
    run = Run(run_id="r1", session_id="s1")
    assert run.request_steer("   ") is False   # 空白不进信箱
    run.finish(STATUS_COMPLETED)
    assert run.request_steer("迟到的纠偏") is False   # 终态不接受改写


# ---- 内核拉缝（检查点① drain → 底片 + 投影 + 事件，继续本轮）----


def test_steer_source_injects_into_context():
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    calls: list[str | None] = ["换个方向，先做 B", None, None]
    events: list[tuple[str, dict]] = []

    result, reply = run_turn(
        session,
        "做 A",
        llm=ScriptedLLM([Message(role="assistant", content="好，改做 B")]),
        agent=_agent(),
        steer_source=lambda: calls.pop(0),
        on_event=lambda t, d: events.append((t, d)),
    )

    assert result is RunResult.COMPLETED
    # 纠偏以 user 消息进底片——后续压缩与崩溃恢复都要看得见它
    assert any(m.role == "user" and m.content == "换个方向，先做 B" for m in session.messages)
    # 原提问也在（纠偏不抹历史）
    assert any(m.role == "user" and m.content == "做 A" for m in session.messages)
    assert ("steer.applied", {"text": "换个方向，先做 B"}) in events
    assert reply is not None and reply.content == "好，改做 B"


def test_steer_source_none_is_noop():
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    result, _ = run_turn(
        session,
        "你好",
        llm=ScriptedLLM([Message(role="assistant", content="好")]),
        agent=_agent(),
        steer_source=None,
    )
    assert result is RunResult.COMPLETED   # 零行为差异


# ---- 收尾段 steer 兜底（评审问题 7）：交互轮告知、task 轮留件续跑 ----


def test_leftover_steer_dropped_on_interactive_run():
    run = Run(run_id="r1", session_id="s1")
    run.request_steer("收尾段才到的纠偏")
    _drop_leftover_steer(run, task=None)
    assert any(e.type == "steer.dropped" for e in run.events)
    assert run.take_steer() is None


def test_leftover_steer_kept_for_task_run():
    task = Task(task_id="t1", goal="g")
    run = Run(run_id="r1", session_id="s1")
    run.request_steer("收尾段才到的纠偏")
    _drop_leftover_steer(run, task=task)
    # task 轮不取：_task_worker 要拿它起新 Run
    assert not any(e.type == "steer.dropped" for e in run.events)
    assert run.take_steer() == "收尾段才到的纠偏"


# ---- 轮后判定（_task_verdict，先人后机）----


def test_verdict_cancel_wins():
    task = Task(task_id="t1", goal="g", cancel_requested=True)
    run = Run(run_id="r1", session_id="s1")
    run.status = STATUS_COMPLETED
    assert _task_verdict(task, run, None) is None
    assert task.status == TASK_CANCELLED


def test_verdict_fuse_blocks():
    task = Task(task_id="t1", goal="g", fuse_fired=True)
    run = Run(run_id="r1", session_id="s1")
    run.status = STATUS_COMPLETED
    assert _task_verdict(task, run, None) is None
    assert task.status == TASK_BLOCKED
    assert task.blocked_reason   # blocked 必写原因（§3）


def test_verdict_failed_run():
    task = Task(task_id="t1", goal="g")
    run = Run(run_id="r1", session_id="s1")
    run.status = STATUS_FAILED
    assert _task_verdict(task, run, None) is None
    assert task.status == TASK_FAILED


def test_verdict_stuck_waits_for_steer():
    task = Task(task_id="t1", goal="g")
    run = Run(run_id="r1", session_id="s1")
    run.status = STATUS_COMPLETED
    run.emit("stuck", {"tools": ["x"]})
    assert _task_verdict(task, run, None) is None
    assert task.status == TASK_WAITING_STEER


def test_verdict_continues_on_open_plan():
    task = Task(task_id="t1", goal="g")
    run = Run(run_id="r1", session_id="s1")
    run.status = STATUS_COMPLETED
    session = Session()
    session.plan.make_plan([{"title": "a"}, {"title": "b"}])
    session.plan.update_step(1, "done", "好了")
    text = _task_verdict(task, run, session)
    assert text is not None and text.startswith("[自动续跑]")
    assert task.continuations == 1
    assert task.status != TASK_DONE   # 续跑不落终态


def test_verdict_steer_fallback_beats_done():
    """收尾段到达的 steer 优先于 done——拿它起新 Run 而不是直接收尾。

    投递必须在 Run 终态之前（终态拒收是正确语义）——真实时序正是如此：
    纠偏在收尾段到达、没被检查点①消化，Run 随后才落终态。
    """
    task = Task(task_id="t1", goal="g")
    run = Run(run_id="r1", session_id="s1")
    run.request_steer("先别停，还要改个东西")
    run.status = STATUS_COMPLETED
    text = _task_verdict(task, run, Session())
    assert text == "先别停，还要改个东西"


def test_verdict_done_without_plan():
    task = Task(task_id="t1", goal="g")
    run = Run(run_id="r1", session_id="s1")
    run.status = STATUS_COMPLETED
    assert _task_verdict(task, run, Session()) is None
    assert task.status == TASK_DONE


def test_verdict_failed_step_marks_failed():
    task = Task(task_id="t1", goal="g")
    run = Run(run_id="r1", session_id="s1")
    run.status = STATUS_COMPLETED
    session = Session()
    session.plan.make_plan([{"title": "a"}])
    session.plan.update_step(1, "failed", "没成")
    assert _task_verdict(task, run, session) is None
    assert task.status == TASK_FAILED
