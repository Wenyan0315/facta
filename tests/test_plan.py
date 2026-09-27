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


# ---------- 保险丝熔断后的收尾段（ADR 056）----------


def _fused_agent(session: Session, rounds: int) -> Agent:
    """轮次预算压到 rounds，逼 run_turn 走收尾段（默认 5 轮测不到它）。"""
    return Agent(
        name="test", system_prompt="sys",
        registry=_plan_registry(session), max_tool_rounds=rounds,
    )


def test_rounds_exhausted_closing_menu_keeps_plan_closeout_only():
    # 根因现场：预算耗尽时模型正想调 finish_plan，而收尾请求原本 tools=None
    # ⇒ 它结构上无法产出 tool_calls，只能把调用吐成正文（DSML 泄漏）。
    # 修法甲轻量版：收尾段只留收官两个菜（update_plan_step + finish_plan），
    # 计划板因此能被正常关闭。
    session = Session()
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "甲"}]}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "做完了"}),
        ]),
        Message(role="assistant", content="", tool_calls=[   # 收尾段点的
            _call("finish_plan", {"summary": "甲已完成"}),
        ]),
        Message(role="assistant", content="已收官：甲做完，无遗留。"),
    ])

    result, reply = run_turn(
        session, "做完甲这件事",
        agent=_fused_agent(session, 2), llm=llm,
        on_confirm=lambda name, args: True,   # 计划审批：批准
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content == "已收官：甲做完，无遗留。"
    # 调用序：2 轮工具循环 + 1 次收尾（递收官两个菜）+ 1 次文字总结（撤干净）
    assert len(llm.calls) == 4
    closing_menu = llm.tool_menus[2]
    assert closing_menu is not None
    assert [s["function"]["name"] for s in closing_menu] == [
        "update_plan_step", "finish_plan",
    ]
    assert llm.tool_menus[3] is None          # 收官跑完即撤菜单
    assert session.plan.active is None        # 计划板真被关闭（047 反方第 4 条的遗留损害）
    assert len(session.plan.archive) == 1


def test_rounds_exhausted_without_plan_announces_empty_menu_upfront():
    # 修法乙：无活跃计划时收尾照旧撤干净菜单，但**事前**告知预算已尽——
    # 不再靠泄漏后的 _DSML_LEAK_HINT 事后教训（那条 hint 要求走「标准
    # tool_calls 字段」，是条结构上不存在的出路，重试因此恒失败）
    session = Session()
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "x"}),
        ]),
        Message(role="assistant", content="预算用完了，这是已做的部分……"),
    ])

    result, reply = run_turn(
        session, "做点事",
        agent=_fused_agent(session, 1), llm=llm,
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content.startswith("预算用完了")
    assert llm.tool_menus[-1] is None         # 无计划 ⇒ 收尾不递任何菜
    assert any(
        "工具预算已用完" in (m.content or "") and "不会再执行任何工具调用" in (m.content or "")
        for m in llm.calls[-1]
    )
    # 告知只进投影、不进底片（时间戳/计划戳同款手法）：入史会堆垃圾并被摘要吸收
    assert all("工具预算已用完" not in (m.content or "") for m in session.messages)


def test_rounds_exhausted_closing_can_finalize_dangling_step():
    # 实机回归（2026-09-27 定向跑 r4）：只递 finish_plan 时模型点了它、被
    # 终态闸拒（有步骤悬空），而补终态的工具已不在菜单里 ⇒ 板子照样挂在
    # active，正是修法要消除的污染。两个菜都递，模型才能同批补终态 + 收官。
    session = Session()
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "甲"}, {"title": "乙"}]}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "做完了"}),
        ]),
        Message(role="assistant", content="", tool_calls=[   # 收尾段：同批补终态 + 收官
            # 同批多点菜必须带 index：merge_stream_chunks 按 index 归并，
            # 缺省全落 0 会被拼成一坨（test_parallel_spawn.py 同款约定）
            {**_call("update_plan_step", {"step_id": 2, "status": "skipped", "note": "预算用尽"}),
             "index": 0},
            {**_call("finish_plan", {"summary": "甲完成，乙因预算用尽跳过"}), "index": 1},
        ]),
        Message(role="assistant", content="甲已完成；乙因工具预算用尽跳过。"),
    ])

    result, reply = run_turn(
        session, "做完甲乙两件事",
        agent=_fused_agent(session, 2), llm=llm,
        on_confirm=lambda name, args: True,
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content.startswith("甲已完成")
    assert session.plan.active is None        # 悬空步骤在收尾段被补终态后真收官
    assert len(session.plan.archive) == 1
    tool_results = [m.content for m in session.messages if m.role == "tool"]
    assert not any("尚未终态化" in r for r in tool_results)   # 没撞上终态闸

