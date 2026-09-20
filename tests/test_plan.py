"""S5b plan-then-act 验收：状态机 / fold / 序列化 / 工具端到端。

三拍板的可测化（027）：
- 模型自判：make_plan 是普通工具（needs_confirmation 标记走确认缝）
- append 修订：事件史不改写，view() fold 推导；孤儿事件跳过
- 显式终态制：finish 校验悬空、终态锁定、skipped/failed 必带 note
"""

import json

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.plan import StepStatus
from agent.memory.store import Session, load_session, save_session
from agent.orchestrator.agent import Agent
from agent.orchestrator.loop import RunResult, run_turn
from agent.tools.context import ToolContext
from agent.tools.plan import register_plan_tools
from agent.tools.registry import ToolRegistry


def _plan_registry(session: Session) -> ToolRegistry:
    """最小装配：registry + ctx（带 session）+ 计划三件。"""
    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=None, session=session)   # type: ignore[arg-type]
    register_plan_tools(registry, ctx)
    return registry


def _agent(session: Session) -> Agent:
    return Agent(name="test", system_prompt="sys", registry=_plan_registry(session))


# ---------- 值对象与状态机 ----------


def test_stepstatus_terminal_semantics():
    assert not StepStatus.PENDING.is_terminal
    assert not StepStatus.IN_PROGRESS.is_terminal
    for s in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.FAILED):
        assert s.is_terminal


def test_create_forces_all_pending_and_program_assigned_ids():
    board = Session().plan
    board.make_plan([{"title": "甲"}, {"title": "乙"}])
    view = board.view()
    assert [s.id for s in view.steps] == [1, 2]   # id 归程序管
    assert all(s.status is StepStatus.PENDING for s in view.steps)


def test_update_step_rejects_unknown_id():
    board = Session().plan
    board.make_plan([{"title": "甲"}])
    try:
        board.update_step(9, "done", "x")
        raise AssertionError("应拒绝")
    except ValueError as e:
        assert "不在当前计划" in str(e)


def test_update_step_rejects_bad_status_with_legal_values():
    board = Session().plan
    board.make_plan([{"title": "甲"}])
    try:
        board.update_step(1, "finnished", "x")   # 拼错的
        raise AssertionError("应拒绝")
    except ValueError as e:
        assert "合法值" in str(e)


def test_terminal_status_locked():
    board = Session().plan
    board.make_plan([{"title": "甲"}])
    board.update_step(1, "done", "完成")
    try:
        board.update_step(1, "in_progress")
        raise AssertionError("应拒绝")
    except ValueError as e:
        assert "终态" in str(e) and "make_plan" in str(e)   # 拒绝要指路


def test_skipped_and_failed_require_note():
    board = Session().plan
    board.make_plan([{"title": "甲"}, {"title": "乙"}])
    for status in ("skipped", "failed"):
        try:
            board.update_step(1, status, "")
            raise AssertionError("应拒绝")
        except ValueError as e:
            assert "note" in str(e)
    board.update_step(1, "skipped", "不需要了")   # 带理由放行
    board.update_step(2, "in_progress")           # 非终态 note 可选


def test_finish_rejects_dangling_steps_then_archives():
    board = Session().plan
    board.make_plan([{"title": "甲"}, {"title": "乙"}])
    board.update_step(1, "done", "好")
    try:
        board.finish_plan("做完了吗")
        raise AssertionError("应拒绝（步骤 2 悬空）")
    except ValueError as e:
        assert "2" in str(e)
    assert board.active is not None   # 拒绝后计划留在 active 继续改
    board.update_step(2, "skipped", "做了发现不必要")
    board.finish_plan("甲完成，乙取消")
    assert board.active is None
    assert len(board.archive) == 1
    assert board.archive[0].status == "finished"
    assert board.view() is None   # 无活跃 → 投影不注入


def test_make_plan_after_finish_starts_fresh_create():
    board = Session().plan
    board.make_plan([{"title": "甲"}])
    board.update_step(1, "done", "好")
    board.finish_plan("一号任务完成")
    kind = board.make_plan([{"title": "新任务"}])   # 归档后新建，不是修订
    assert kind == "created"
    assert len(board.archive) == 1   # 一号任务仍在归档


# ---------- append 修订与 fold ----------


def test_revision_requires_reason_and_inherits_status():
    board = Session().plan
    board.make_plan([{"title": "甲"}, {"title": "乙"}])
    board.update_step(1, "done", "好")
    try:
        board.make_plan([{"title": "甲"}])   # 有活跃计划，无 reason
        raise AssertionError("应拒绝")
    except ValueError as e:
        assert "reason" in str(e)
    board.make_plan(
        [{"title": "甲", "status": "done", "note": "好"}, {"title": "丙", "status": "pending"}],
        reason="乙换成丙",
    )
    view = board.view()
    assert [s.title for s in view.steps] == ["甲", "丙"]
    assert view.steps[0].status is StepStatus.DONE   # 继承
    assert view.steps[0].note == "好"


def test_fold_skips_orphan_events_after_revision():
    # 修订重排后，旧 step_updated 指向的 id 不在新表——跳过不报错
    board = Session().plan
    board.make_plan([{"title": "甲"}, {"title": "乙"}, {"title": "丙"}])
    board.update_step(3, "in_progress")
    board.make_plan(
        [{"title": "甲", "status": "pending"}, {"title": "乙", "status": "pending"}],
        reason="砍掉丙",
    )
    view = board.view()   # 旧 id=3 的 in_progress 事件变孤儿
    assert len(view.steps) == 2
    assert all(s.status is StepStatus.PENDING for s in view.steps)


def test_view_is_pure_fold():
    board = Session().plan
    board.make_plan([{"title": "甲"}])
    board.update_step(1, "in_progress")
    v1, v2 = board.view(), board.view()   # 连续两次 fold 结果全等
    assert v1 == v2
    assert board.active is not None and board.active.events  # 事件史没被 fold 动过


# ---------- 序列化 ----------


def test_session_roundtrip_preserves_plan_and_clears_pending(tmp_path):
    session = Session()
    session.plan.make_plan([{"title": "甲"}, {"title": "乙"}])
    session.plan.update_step(1, "done", "好")
    assert session.plan.drain()   # 传输队列有事件（尚未消费）
    path = tmp_path / "session.json"
    save_session(session, path)
    loaded = load_session(path)
    assert loaded.plan.view() is not None
    assert loaded.plan.view().steps[0].status is StepStatus.DONE   # 事件史全量恢复
    assert loaded.plan.drain() == []   # _pending 不落盘：重启后无陈旧事件


def test_old_session_without_plan_section_loads_empty_board(tmp_path):
    # S5b 之前的会话文件没有 plan 段——宽进为空板，不炸
    path = tmp_path / "session.json"
    path.write_text(json.dumps({
        "version": 1, "title": None, "messages": [],
        "memory": {"summary": None, "summarized_upto": 1},
    }), encoding="utf-8")
    loaded = load_session(path)
    assert loaded.plan.view() is None


def test_max_steps_enforced():
    board = Session().plan
    try:
        board.make_plan([{"title": f"步{i}"} for i in range(11)])
        raise AssertionError("应拒绝")
    except ValueError as e:
        assert "1..10" in str(e)


# ---------- 工具层端到端（run_turn 全链路）----------


def _call(name: str, args: dict) -> dict:
    return {"id": f"call_{name}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def test_plan_lifecycle_end_to_end_with_confirm():
    # 两轮跑：第一轮建计划+做步骤1；第二轮断言轮首投影带计划块（跨轮续跑）
    session = Session()
    events: list[tuple[str, dict]] = []
    on_event = lambda t, d: events.append((t, d))   # noqa: E731

    llm1 = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "查资料"}, {"title": "写总结"}]}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "查完了"}),
        ]),
        Message(role="assistant", content="第一轮：资料查完，总结下一轮做"),
    ])
    result1, _ = run_turn(
        session, "帮我查点资料然后总结",
        agent=_agent(session), llm=llm1,
        on_event=on_event,
        on_confirm=lambda name, args: True,   # 计划审批：批准
    )
    assert result1 is RunResult.COMPLETED
    assert session.plan.active is not None   # 计划活着，跨轮继续

    # 第二轮：轮首投影注入计划块（模型新开一轮看得见蓝图与进度）
    llm2 = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 2, "status": "skipped", "note": "用户说不用了"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("finish_plan", {"summary": "资料已查，总结取消"}),
        ]),
        Message(role="assistant", content="任务完成，资料在这里……"),
    ])
    result2, _ = run_turn(
        session, "继续",
        agent=_agent(session), llm=llm2,
        on_event=on_event,
        on_confirm=lambda name, args: True,
    )

    assert result2 is RunResult.COMPLETED
    # 事件序列：每个工具的 plan 事件在 tool_result 之前（因果序）
    types = [t for t, _ in events]
    assert types == [
        "tool_started", "plan.created", "tool_result",
        "tool_started", "plan.step_updated", "tool_result",
        "tool_started", "plan.step_updated", "tool_result",
        "tool_started", "plan.finished", "tool_result",
    ]
    # 收官后：归档、无活跃
    assert session.plan.active is None
    assert len(session.plan.archive) == 1
    # 第二轮的轮首 payload 带计划块：步骤1 done（●）步骤2 pending（○）都看得见
    plan_block = next(
        m.content for m in llm2.calls[0] if m.role == "system" and "当前任务计划" in (m.content or "")
    )
    assert "●" in plan_block and "○" in plan_block
    # 工具结果回灌带最新视图（双视图决策：同轮行进导航）
    tool_results = [m.content for m in session.messages if m.role == "tool"]
    assert any("查资料" in r and "●" in r for r in tool_results)


def test_plan_confirm_rejected_no_events_no_active_plan():
    session = Session()
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "甲"}]}),
        ]),
        Message(role="assistant", content="好的，那我直接做"),
    ])
    events: list[tuple[str, dict]] = []

    result, _ = run_turn(
        session, "做个事",
        agent=_agent(session), llm=llm,
        on_event=lambda t, d: events.append((t, d)),
        on_confirm=lambda name, args: False,   # 掌舵点：否决
    )

    assert result is RunResult.COMPLETED
    assert session.plan.active is None   # 否决 → 计划未生效
    assert [t for t, _ in events] == ["tool_started", "tool_result"]   # 无 plan.* 事件
    tool_results = [m.content for m in session.messages if m.role == "tool"]
    assert any("拒绝" in r for r in tool_results)   # 回灌可见拒绝原因


def test_dangling_finish_feeds_back_for_self_correction():
    # finish_plan 有悬空 → 错误串回灌 → 模型补终态 → 再收官（反馈环全链路）
    session = Session()
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "甲"}, {"title": "乙"}]}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "好"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("finish_plan", {"summary": "完事"}),   # 步骤 2 还悬空
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 2, "status": "done", "note": "也好了"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("finish_plan", {"summary": "全部完成"}),
        ]),
        Message(role="assistant", content="搞定"),
    ])

    result, _ = run_turn(
        session, "做完甲乙两件事",
        agent=_agent(session), llm=llm,
        on_confirm=lambda name, args: True,
    )
    assert result is RunResult.COMPLETED
    tool_results = [m.content for m in session.messages if m.role == "tool"]
    assert any("尚未终态化" in r for r in tool_results)   # 第一次收官被拒（程序闸）
    assert session.plan.active is None   # 第二次过了
    assert len(session.plan.archive) == 1
