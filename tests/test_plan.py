"""S5b plan-then-act 验收：状态机 / fold / 序列化 / 工具端到端。

三拍板的可测化（027）：
- 模型自判：make_plan 是普通工具（needs_confirmation 标记走确认缝）
- append 修订：事件史不改写，view() fold 推导；孤儿事件跳过
- 显式终态制：finish 校验悬空、终态锁定、skipped/failed 必带 note
"""

import json

import pytest

from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.plan import StepStatus
from facta.memory.store import Session, load_session, save_session
from facta.orchestrator.agent import Agent
from facta.orchestrator.loop import RunResult, run_turn
from facta.tools.context import ToolContext
from facta.tools.plan import register_plan_tools
from facta.tools.registry import ToolRegistry


def _plan_registry(session: Session) -> ToolRegistry:
    """最小装配：registry + ctx（带 session）+ 计划三件。"""
    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=None, session=session)   # type: ignore[arg-type]
    register_plan_tools(registry, ctx)
    return registry


def _agent(session: Session) -> Agent:
    return Agent(name="test", system_prompt="sys", registry=_plan_registry(session))


@pytest.fixture(autouse=True)
def _isolated_learned(tmp_path, monkeypatch):
    """061：召回默认读真实 data/learned——那个目录会随真实会话固化增长，测试
    依赖可变仓库资产＝潜在 flaky（新条目里出现个 notes 就能翻掉一条 not in）。
    默认指向空目录（无候选命中），要验召回的测试自己写条目。只覆盖本文件：
    其余 7 个用到 make_plan 的测试不断言回灌正文（升级信号见 061 遗留 4）。
    """
    empty = tmp_path / "learned"
    empty.mkdir()
    monkeypatch.setattr("facta.tools.plan._LEARNED_DIR", empty)


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


# ---------- 060：失败台账（finish_plan 落账 + make_plan 查重软拦）----------


def _run(session: Session, llm: ScriptedLLM) -> None:
    result, _ = run_turn(
        session, "做事", agent=_agent(session), llm=llm,
        on_confirm=lambda name, args: True,   # 计划审批：批准
    )
    assert result is RunResult.COMPLETED


def test_finish_plan_with_failed_step_appends_ledger(tmp_path, monkeypatch):
    # 判定标准①：failed 收官 → 台账一行、字段自含（查重不回查会话）
    ledger = tmp_path / "plan_failures.jsonl"
    monkeypatch.setattr("facta.tools.plan._FAILURES_PATH", ledger)
    session = Session()
    _run(session, ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "甲"}, {"title": "乙"}]}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "好"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 2, "status": "failed", "note": "接口 404"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("finish_plan", {"summary": "甲成乙败"}),
        ]),
        Message(role="assistant", content="收官"),
    ]))
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["steps"] == ["甲", "乙"]
    assert rec["failed"] == [{"title": "乙", "note": "接口 404"}]
    assert rec["summary"] == "甲成乙败"
    assert "sid" not in rec   # Session 不持 id（S8a：身份=文件名）


def test_finish_plan_all_done_writes_nothing(tmp_path, monkeypatch):
    # 判定标准②：无 failed ⇒ 不落账（台账只记失败，成功不进索引）
    ledger = tmp_path / "plan_failures.jsonl"
    monkeypatch.setattr("facta.tools.plan._FAILURES_PATH", ledger)
    session = Session()
    _run(session, ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "甲"}]}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "好"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("finish_plan", {"summary": "完事"}),
        ]),
        Message(role="assistant", content="收官"),
    ]))
    assert not ledger.exists()


def test_failed_step_before_revision_still_recorded(tmp_path, monkeypatch):
    # 修订换表前的 failed 也入账：view() fold 只看得见最终表，事件史记得——
    # 失败经历是事实，title 靠逐事件 fold 当时表找回
    ledger = tmp_path / "plan_failures.jsonl"
    monkeypatch.setattr("facta.tools.plan._FAILURES_PATH", ledger)
    session = Session()
    _run(session, ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "甲"}, {"title": "乙"}]}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 2, "status": "failed", "note": "接口 404"}),
        ]),
        Message(role="assistant", content="", tool_calls=[   # 换表：乙不在新表
            _call("make_plan", {
                "steps": [{"title": "丙", "status": "pending"}],
                "reason": "乙路不通，改走丙",
            }),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "丙成"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("finish_plan", {"summary": "改走丙完成"}),
        ]),
        Message(role="assistant", content="收官"),
    ]))
    rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert rec["steps"] == ["丙"]   # 相似度比对用最终表
    assert rec["failed"] == [{"title": "乙", "note": "接口 404"}]


_LEDGER_ROW = json.dumps({
    "ts": "2026-09-20T10:00:00+00:00",
    "steps": ["搜索 RAG 最新实践", "整理成笔记"],
    "failed": [{"title": "搜索 RAG 最新实践", "note": "网络被墙，搜索全超时"}],
    "summary": "搜索全灭，任务失败",
}, ensure_ascii=False)


def test_make_plan_softwarns_on_similar_history(tmp_path, monkeypatch):
    # 判定标准③④：预置台账（＝跨会话持久化，新会话读同一文件）→ 相似计划
    # 命中软拦——回灌带历史失败，但计划照常创建（「计划已创建」开头）
    ledger = tmp_path / "plan_failures.jsonl"
    ledger.write_text(_LEDGER_ROW + "\n", encoding="utf-8")
    monkeypatch.setattr("facta.tools.plan._FAILURES_PATH", ledger)
    session = Session()
    _run(session, ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "搜索 RAG 最新实践"}, {"title": "整理成笔记"}]}),
        ]),
        Message(role="assistant", content="好"),
    ]))
    hit = next(m.content for m in session.messages if m.role == "tool")
    assert hit.startswith("计划已创建")        # 软拦：照创，改向权归模型
    assert "历史相似失败" in hit
    assert "网络被墙，搜索全超时" in hit       # 当时 note 摘要回灌
    assert "agent 自述" in hit                 # 未经客观背书的标注（P0-7）


def test_make_plan_dissimilar_history_no_warning(tmp_path, monkeypatch):
    # 零命中：不相似的计划不带警告段
    ledger = tmp_path / "plan_failures.jsonl"
    ledger.write_text(_LEDGER_ROW + "\n", encoding="utf-8")
    monkeypatch.setattr("facta.tools.plan._FAILURES_PATH", ledger)
    session = Session()
    _run(session, ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "给周报排个版"}]}),
        ]),
        Message(role="assistant", content="好"),
    ]))
    hit = next(m.content for m in session.messages if m.role == "tool")
    assert "历史相似失败" not in hit


def test_make_plan_warns_despite_embellished_titles(tmp_path, monkeypatch):
    # 校准钉住（2026-09-28 m1 首跑漏报现场）：模型不会照抄题面措辞，会给
    # 步骤标题补括注/路径细节，对称 ratio 被稀释到 0.582——阈值 0.5 必须仍命中
    ledger = tmp_path / "plan_failures.jsonl"
    ledger.write_text(json.dumps({   # 与 m1 场景预置台账同措辞（漏报现场原件）
        "ts": "2026-09-20T10:00:00+00:00",
        "steps": ["搜索 RAG 的最新实践", "把结果整理成一篇笔记"],
        "failed": [{"title": "搜索 RAG 的最新实践", "note": "联网搜索全部超时"}],
        "summary": "搜索全灭，调研失败",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr("facta.tools.plan._FAILURES_PATH", ledger)
    session = Session()
    _run(session, ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [
                {"title": "搜索 RAG 的最新实践（检索个人知识库 + 联网补充）"},
                {"title": "把调研结果整理成一篇笔记并写入 data/notes/"},
            ]}),
        ]),
        Message(role="assistant", content="好"),
    ]))
    hit = next(m.content for m in session.messages if m.role == "tool")
    assert "历史相似失败" in hit


# ---------- 061：记忆召回跟随 plan 上下文（make_plan 回灌）----------


def _learned(tmp_path, monkeypatch, **buckets: str) -> None:
    """把 learned 三桶指到 tmp；关键字参数名＝桶名，值＝该桶文件正文。"""
    d = tmp_path / "recall_learned"
    d.mkdir()
    for name, text in buckets.items():
        (d / f"{name}.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr("facta.tools.plan._LEARNED_DIR", d)


def _plan_echo(args: dict) -> str:
    """跑一轮只含 make_plan 的对话，返回它的工具回灌串。"""
    session = Session()
    _run(session, ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[_call("make_plan", args)]),
        Message(role="assistant", content="好"),
    ]))
    return next(m.content for m in session.messages if m.role == "tool")


# 与 data/learned/constraints.md 里 r2 病灶的相邻记忆同形（ADR 061 地面真值 8）
_SPAWN_RULE = "- [2026-09-23] spawn_step 不接受 tools 参数，需用 spawn_subagent 并行派发。\n"


def test_recall_hits_tool_declared_in_plan(tmp_path, monkeypatch):
    # 判定标准①：计划声明的工具名 → 召回含该标识符的条目，带桶标与原行格式
    _learned(tmp_path, monkeypatch, constraints=_SPAWN_RULE)
    out = _plan_echo({"steps": [{"title": "并发跑三个子任务"}], "tools": ["spawn_subagent"]})
    assert "📌 与本计划相关的长时记忆" in out
    assert f"[constraints] {_SPAWN_RULE.rstrip()}" in out


def test_recall_silent_without_ascii_keys(tmp_path, monkeypatch):
    # 判定标准②：全中文标题 + 未声明 tools ⇒ 无候选键 ⇒ 回灌逐字节回到 061 前。
    # 这条同时是本案已知最大局限的显式登记（ADR 061 反方 2）：中文计划不产键
    _learned(tmp_path, monkeypatch, constraints=_SPAWN_RULE)
    out = _plan_echo({"steps": [{"title": "给周报排个版"}]})
    assert out.startswith("计划已创建")
    assert "长时记忆" not in out            # 无召回段 ⇒ 回灌与 061 前逐字节一致
    assert "它会自动回写状态）" in out      # 原有回灌形状完好


def test_recall_matches_identifiers_in_titles(tmp_path, monkeypatch):
    # 候选键也来自步骤标题：省略 tools 声明的计划照样召回（057 之前存量兼容）
    _learned(tmp_path, monkeypatch,
             other="- [2026-09-23] run_turn 定义在 src/facta/orchestrator/loop.py。\n")
    out = _plan_echo({"steps": [{"title": "读 loop.py 弄清 run_turn 的投影"}]})
    assert "[other] - [2026-09-23] run_turn 定义在" in out


def test_recall_capped_and_ordered_by_matched_keys(tmp_path, monkeypatch):
    # 判定标准③：命中 4 条只出 3 条，且「匹配到的键数」多的排最前
    _learned(tmp_path, monkeypatch, constraints="\n".join([
        "- [2026-09-01] run_turn 有列表身份陷阱。",                 # 1 键
        "- [2026-09-02] run_turn 与 make_plan 都过投影。",           # 2 键 ⇒ 排首
        "- [2026-09-03] spawn_step 会自动回写状态。",                # 1 键
        "- [2026-09-04] write_note 落在笔记目录。",                  # 1 键 ⇒ 被上限挤掉
    ]) + "\n")
    out = _plan_echo({"steps": [{"title": "甲"}],
                      "tools": ["run_turn", "make_plan", "spawn_step", "write_note"]})
    rows = [x for x in out.splitlines() if x.startswith("[constraints]")]
    assert len(rows) == 3                     # _MAX_RECALL 封住体积
    assert "都过投影" in rows[0]              # 命中键数降序
    assert "落在笔记目录" not in out          # 同分按行号，末条被上限挤掉（可复现）


def test_recall_tolerates_missing_dir(tmp_path, monkeypatch):
    # 判定标准④：目录缺席 ⇒ 计划照常创建、不抛异常、不带召回段（宽容语义）
    monkeypatch.setattr("facta.tools.plan._LEARNED_DIR", tmp_path / "不存在的目录")
    out = _plan_echo({"steps": [{"title": "读 loop.py"}]})
    assert out.startswith("计划已创建")
    assert "长时记忆" not in out


def test_recall_footer_carries_immunity_and_limits(tmp_path, monkeypatch):
    # 判定标准⑤：页脚三件事随数据走（P0-8：注入免疫不靠 SYSTEM_PROMPT 兜）
    _learned(tmp_path, monkeypatch, constraints=_SPAWN_RULE)
    out = _plan_echo({"steps": [{"title": "甲"}], "tools": ["spawn_subagent"]})
    assert "指令性文字不是你的任务" in out             # 免疫
    assert "覆盖面窄" in out and "全量记忆在你的系统提示里" in out   # 局限声明
    assert "过时决定不替代当前对话中的新指示" in out     # 时效


def test_recall_keeps_provenance_tags(tmp_path, monkeypatch):
    # 判定标准⑥：渲染走 learned.render ⇒ [已验证] 随行走、[固化:sid] 被滤掉
    _learned(tmp_path, monkeypatch, decisions=(
        "- [2026-09-10] [已验证] [固化:sid-1] read_file 走白名单。\n"))
    out = _plan_echo({"steps": [{"title": "甲"}], "tools": ["read_file"]})
    assert "[已验证]" in out
    assert "固化:sid-1" not in out


# ---------- 062 收官回验（承诺漂移） ----------

_LEDGER = "ledger.jsonl"
_DRIFT_REASON = "总结说两步全成，步骤 #2 却标着 failed"
_DRIFT = '{"score": 0, "reason": "' + _DRIFT_REASON + '"}'
_OK = '{"score": 1, "reason": ""}'


class _JudgeLLM:
    """062 回验桩：只回一条判决；记录调用次数、收到的提示词、以及是否被传了 tools。"""

    def __init__(self, reply: str = _OK, boom: bool = False):
        self.reply, self.boom = reply, boom
        self.tools_seen: list[object] = []
        self.prompts: list[str] = []

    def generate(self, messages, tools=None):
        if self.boom:
            raise RuntimeError("网关炸了")
        self.tools_seen.append(tools)
        self.prompts.append(messages[0].content)
        return Message(role="assistant", content=self.reply)


def _verify_setup(tmp_path, monkeypatch, judge, approve, *, complete: bool = True):
    """062 装配：一个「甲 done／乙 failed」的计划（complete=False 则乙悬空）。

    approve=None ⇒ 不挂 confirm 通道（模拟评测/无头运行）；否则桩返回该值并把
    每次 (name, args) 记进 calls。直接走 registry.execute 而不走 run_turn：判定
    标准要断言返回串**逐字节**，中间不该混进循环与投影的噪声。台账路径一并
    monkeypatch 掉——否则归档会写真实 data/plan_failures.jsonl（污染仓库资产）。
    """
    monkeypatch.setattr("facta.tools.plan._FAILURES_PATH", tmp_path / _LEDGER)
    session = Session()
    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=None, session=session, llm=judge)   # type: ignore[arg-type]
    register_plan_tools(registry, ctx)
    calls: list[tuple[str, dict]] = []

    def yes(name: str, args: dict) -> bool:        # make_plan 的人审：一律批准
        return True

    def stub(name: str, args: dict) -> bool:       # finish_plan 的漂移裁决桩
        calls.append((name, args))
        return bool(approve)

    registry.execute("make_plan", '{"steps": [{"title": "甲"}, {"title": "乙"}]}', yes)
    registry.execute("update_plan_step", '{"step_id": 1, "status": "done", "note": "成了"}', yes)
    if complete:
        registry.execute("update_plan_step", '{"step_id": 2, "status": "failed", "note": "404"}', yes)
    return session, registry, calls, (stub if approve is not None else None)


def test_finish_plan_drift_rejected_keeps_plan_active(tmp_path, monkeypatch):
    # 判定标准①：漂移 + 人拒 ⇒ 不归档、计划留在 active（整改路可用）。
    # 指路必须是**真能走通**的路：此时全步骤已终态，update_plan_step 改不动
    # （终态锁定），所以只有 make_plan 修订／补产出／改如实总结三条
    judge = _JudgeLLM(_DRIFT)
    session, registry, calls, confirm = _verify_setup(tmp_path, monkeypatch, judge, False)
    out = registry.execute("finish_plan", '{"summary": "两步全部完成"}', confirm)
    assert session.plan.active is not None and not session.plan.archive
    assert "计划收官被用户拒绝" in out and "仍然活跃" in out
    assert "update_plan_step 改不动" in out          # 不指死路
    assert "make_plan 修订" in out and "如实的 summary" in out
    assert calls == [("finish_plan", {"summary": "两步全部完成", "drift": _DRIFT_REASON})]
    assert judge.tools_seen == [None]                # 判定标准⑦：内部调用绝不传 tools


def test_finish_plan_drift_approved_archives_with_trace(tmp_path, monkeypatch):
    # 判定标准②：漂移 + 人批 ⇒ 照常归档（060 台账照跑），漂移进返回串 ⇒
    # registry 审计收口把 result 落盘，零新仪器就留了痕
    judge = _JudgeLLM(_DRIFT)
    session, registry, _, confirm = _verify_setup(tmp_path, monkeypatch, judge, True)
    out = registry.execute("finish_plan", '{"summary": "两步全部完成"}', confirm)
    assert session.plan.active is None and len(session.plan.archive) == 1
    assert out.startswith("任务收官（全部步骤已终态化，计划转入归档）：两步全部完成")
    assert _DRIFT_REASON in out and "用户已确认接受" in out
    ledger = json.loads((tmp_path / _LEDGER).read_text(encoding="utf-8"))
    assert ledger["summary"] == "两步全部完成"        # 060 台账未被本刀打断


def test_finish_plan_drift_without_confirm_degrades_to_notice(tmp_path, monkeypatch):
    # 判定标准③：无 confirm 通道 ⇒ 降级为**告知**（照旧归档），刻意不照搬
    # registry 的「无 confirm 按拒绝」——没人能解锁时拒收官＝板子永久挂 active
    judge = _JudgeLLM(_DRIFT)
    session, registry, calls, none_confirm = _verify_setup(tmp_path, monkeypatch, judge, None)
    out = registry.execute("finish_plan", '{"summary": "两步全部完成"}', none_confirm)
    assert session.plan.active is None and calls == []
    assert "本次运行没有人审通道" in out and _DRIFT_REASON in out


@pytest.mark.parametrize("judge", [
    _JudgeLLM(_OK),                                 # 判为一致 ⇒ 放行
    _JudgeLLM('{"score": "0", "reason": "x"}'),     # score 不是 int ⇒ 解析器返 None
    _JudgeLLM("我看没问题"),                          # 整段非 JSON、也抠不出块
    _JudgeLLM('判决：{"score": 1, "reason": ""} 完毕'),  # 散文包裹 ⇒ 走正则抠块兜底
    _JudgeLLM(boom=True),                            # LLM 抛异常
    None,                                            # ctx.llm 缺席（既有测试全是这一档）
], ids=["一致", "score非int", "非JSON", "散文包裹", "网关异常", "无llm"])
def test_finish_plan_lenient_when_verdict_unusable(tmp_path, monkeypatch, judge):
    # 判定标准④+⑥：回验器任何故障一律放行，且**不调用 confirm**（不拿故障去烦人）。
    # 返回串与 062 前逐字节一致 ⇒ 无漂移路径零回归；confirm 桩返回 False 却照样
    # 归档，也反证了 finish_plan **没有**被挂上 needs_confirmation（拍板 4）
    session, registry, calls, confirm = _verify_setup(tmp_path, monkeypatch, judge, False)
    out = registry.execute("finish_plan", '{"summary": "两步全部完成"}', confirm)
    assert session.plan.active is None and calls == []
    assert out == "任务收官（全部步骤已终态化，计划转入归档）：两步全部完成"


def test_finish_plan_dangling_skips_verification(tmp_path, monkeypatch):
    # 判定标准⑤：悬空计划被 is_complete() 前置挡住 ⇒ 一次 LLM 都不发起（不白花），
    # 走既有 dangling 拒绝串，逐字节不变
    judge = _JudgeLLM(_DRIFT)
    session, registry, calls, confirm = _verify_setup(
        tmp_path, monkeypatch, judge, False, complete=False)
    out = registry.execute("finish_plan", '{"summary": "全做完了"}', confirm)
    assert judge.prompts == [] and calls == []
    assert out == ("计划操作被拒：步骤 [2] 尚未终态化（pending/in_progress 悬空），"
                   "先逐个 update_plan_step 到 done/skipped/failed 再收官")
    assert session.plan.active is not None


def test_verify_prompt_reuses_single_source_of_truth(tmp_path, monkeypatch):
    # 061 遗留 5 的共用约束：回验读的「计划原文」必须复用 board.view() + format_view
    # 这**同一份**真值源（不另起第二份计划快照）⇒ 提示词里应出现视图渲染的记号与 note
    judge = _JudgeLLM(_OK)
    _, registry, _, confirm = _verify_setup(tmp_path, monkeypatch, judge, False)
    registry.execute("finish_plan", '{"summary": "两步全部完成"}', confirm)
    prompt = judge.prompts[0]
    assert "● 1. 甲 —— 成了" in prompt and "✗ 2. 乙 —— 404" in prompt   # format_view 渲染
    assert "收官总结：两步全部完成" in prompt
    assert '"score": 1' in prompt and "一句话" in prompt     # 二元判决 + 理由限长


