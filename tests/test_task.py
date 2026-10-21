"""ADR 115（M1）数据层验收：Task 实体 / TaskStore / 续跑判定 / 保险丝 / 分账。

钉子测试也在这里：task.py 不 import server/（分层纪律），Run 状态字面量
与 run_store 常量同值靠 test_run_status_literal_pinned 锁死漂移。
"""

import time

from facta.core.telemetry import UsageLedger
from facta.memory.store import Session
from facta.orchestrator.task import (
    RUN_STATUS_COMPLETED,
    TASK_DONE,
    TASK_MAX_CONTINUATIONS,
    TASK_MAX_COST,
    TASK_MAX_WALL_CLOCK_S,
    TASK_QUEUED,
    Task,
    TaskStore,
    _is_task_done,
    _plan_has_failed,
    billing_add,
    billing_totals,
    continue_message,
    fuse_expired,
    should_continue,
    to_dict,
    usage_delta,
)
from facta.server.run_store import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED, Run


def _run(status: str = STATUS_COMPLETED) -> Run:
    run = Run(run_id="r1", session_id="s1")
    run.status = status
    return run


def _session_with_plan(*titles: str) -> Session:
    s = Session()
    s.plan.make_plan([{"title": t} for t in titles])
    return s


# ---- 钉子：分层纪律 ----


def test_run_status_literal_pinned():
    """task.py 的 RUN_STATUS_COMPLETED 必须与 run_store 常量同值。

    task 不 import server（倒依赖是层污染），跨模块一致靠这一根钉子。
    """
    assert RUN_STATUS_COMPLETED == STATUS_COMPLETED


# ---- 续跑判定（§3）----


def test_should_continue_open_plan_completed_run():
    task = Task(task_id="t1", goal="g")
    assert should_continue(task, _run(), _session_with_plan("a", "b")) is True


def test_should_continue_requires_completed_run():
    task = Task(task_id="t1", goal="g")
    assert should_continue(task, _run(STATUS_FAILED), _session_with_plan("a")) is False
    assert should_continue(task, _run(STATUS_CANCELLED), _session_with_plan("a")) is False


def test_should_continue_blocked_by_stuck():
    task = Task(task_id="t1", goal="g")
    run = _run()
    run.emit("stuck", {"tools": ["x"]})
    assert should_continue(task, run, _session_with_plan("a")) is False


def test_should_continue_blocked_by_cancel_or_fuse():
    session = _session_with_plan("a")
    cancelled = Task(task_id="t1", goal="g", cancel_requested=True)
    assert should_continue(cancelled, _run(), session) is False
    fused = Task(task_id="t1", goal="g", fuse_fired=True)
    assert should_continue(fused, _run(), session) is False


def test_should_continue_requires_active_plan():
    task = Task(task_id="t1", goal="g")
    # 无计划＝一发委派，不续跑
    assert should_continue(task, _run(), Session()) is False
    # 计划已全终态（含 failed）也不续跑——is_complete 把 FAILED 也算终态
    s = _session_with_plan("a")
    s.plan.update_step(1, "failed", "没成")
    assert should_continue(task, _run(), s) is False


# ---- done 自数判据（评审问题 4）----


def test_done_judgement_without_plan():
    assert _is_task_done(Session()) is True


def test_done_judgement_all_done_or_skipped():
    s = _session_with_plan("a", "b")
    s.plan.update_step(1, "done", "好了")
    s.plan.update_step(2, "skipped", "不需要了")
    assert _is_task_done(s) is True
    assert _plan_has_failed(s) is False


def test_done_judgement_failed_step_is_not_done():
    s = _session_with_plan("a", "b")
    s.plan.update_step(1, "done", "好了")
    s.plan.update_step(2, "failed", "没成")
    assert _is_task_done(s) is False   # 不能用 Plan.is_complete（它把 FAILED 算终态）
    assert _plan_has_failed(s) is True


def test_done_judgement_archive_does_not_absorb_failure():
    """finish_plan 归档不吸收失败（ADR 问题 4 补口，实机验收发现）。

    模型收官归档含 failed 的计划后 view() 为 None——判 done 就是给
    「用 finish_plan 洗白失败」开后门（真机第二轮实测踩中）。裁决回落到
    最后一份归档计划。
    """
    s = _session_with_plan("a", "b")
    s.plan.update_step(1, "done", "好了")
    s.plan.update_step(2, "failed", "没成")
    s.plan.finish_plan("收官")
    assert s.plan.view() is None   # 前提：活跃计划已归档
    assert _is_task_done(s) is False
    assert _plan_has_failed(s) is True


def test_done_judgement_last_archived_plan_wins():
    """多份归档只看最后一份：后续计划取代先前计划的失败（粒度裁定，ADR 补注）。"""
    s = _session_with_plan("旧计划")
    s.plan.update_step(1, "failed", "旧的没做成")
    s.plan.finish_plan("旧收官")
    s.plan.make_plan([{"title": "新计划"}])
    s.plan.update_step(1, "done", "补做成功")
    s.plan.finish_plan("新收官")
    assert _is_task_done(s) is True
    assert _plan_has_failed(s) is False


# ---- 保险丝判据（§3，看守与轮间共用一份）----


def test_fuse_continuations():
    task = Task(task_id="t1", goal="g", started_at=time.time(), continuations=TASK_MAX_CONTINUATIONS)
    assert "续跑" in (fuse_expired(task) or "")
    task.continuations = 0
    assert fuse_expired(task) is None


def test_fuse_wall_clock(monkeypatch):
    monkeypatch.setattr("facta.orchestrator.task.TASK_MAX_WALL_CLOCK_S", 1.0)
    task = Task(task_id="t1", goal="g", started_at=time.time() - 10)
    assert "时长" in (fuse_expired(task) or "")
    task.started_at = time.time()
    assert fuse_expired(task) is None


def test_fuse_cost_with_base(monkeypatch):
    monkeypatch.setattr("facta.orchestrator.task.TASK_MAX_COST", 1.0)
    task = Task(task_id="t1", goal="g", started_at=time.time())
    billing_add(task, "r1", {"llm_cost": 2.0}, approx=False)
    assert "成本" in (fuse_expired(task) or "")
    # cost_base 基线（resume 重置）：已花的不算新预算
    task.cost_base = 2.0
    assert fuse_expired(task) is None


def test_fuse_defaults_are_sane():
    assert TASK_MAX_CONTINUATIONS >= 1
    assert TASK_MAX_WALL_CLOCK_S > 0
    assert TASK_MAX_COST is None or TASK_MAX_COST > 0


# ---- 分账（§2）----


def test_usage_delta():
    before = UsageLedger()
    after = UsageLedger()
    after.llm_calls = 3
    after.llm_cost = 1.5
    after.tokens_in = 100
    delta = usage_delta(before, after)
    assert delta["llm_calls"] == 3
    assert delta["llm_cost"] == 1.5
    assert delta["tokens_in"] == 100
    assert delta["tokens_out"] == 0


def test_billing_add_and_totals():
    task = Task(task_id="t1", goal="g")
    billing_add(task, "r1", {"llm_cost": 1.0, "llm_calls": 2}, approx=False)
    billing_add(task, "r2", {"llm_cost": 0.5, "llm_calls": 1}, approx=True)
    totals = billing_totals(task)
    assert totals["llm_cost"] == 1.5
    assert totals["llm_calls"] == 3
    assert task.billing["approx_runs"] == ["r2"]   # 近似标注只进名单，不改数字
    # 同一 Run 重复入账不重复记近似
    billing_add(task, "r2", {"llm_cost": 0.5, "llm_calls": 1}, approx=True)
    assert task.billing["approx_runs"] == ["r2"]


# ---- 续跑注入消息 ----


def test_continue_message_lists_open_steps():
    s = _session_with_plan("a", "b")
    text = continue_message(s)
    assert "#1" in text and "#2" in text
    assert text.startswith("[自动续跑]")


def test_continue_message_no_open_steps_degrades():
    s = _session_with_plan("a")
    s.plan.update_step(1, "done", "好了")
    text = continue_message(s)
    assert "已无未终态步骤" in text   # 不产出「未终态步骤 ，」病句


# ---- TaskStore 持久化（内存快路径 + 磁盘真值源）----


def test_task_store_roundtrip(tmp_path):
    store = TaskStore(root=tmp_path)
    task = store.create("做成这件事", session_id="20260101-000000", repo_root="/x")
    assert task.status == TASK_QUEUED
    assert (tmp_path / f"{task.task_id}.json").is_file()

    task.status = TASK_DONE
    task.run_ids.append("r1")
    billing_add(task, "r1", {"llm_cost": 0.3}, approx=False)
    store.save(task)

    # 新实例从磁盘懒加载（重启后仍可 resume/查询）
    fresh = TaskStore(root=tmp_path)
    loaded = fresh.get(task.task_id)
    assert loaded is not None
    assert loaded.status == TASK_DONE
    assert loaded.run_ids == ["r1"]
    assert billing_totals(loaded)["llm_cost"] == 0.3


def test_task_store_get_rejects_bad_id(tmp_path):
    store = TaskStore(root=tmp_path)
    assert store.get("../../etc/passwd") is None
    assert store.get("no-such-id") is None


def test_to_dict_shape(tmp_path):
    store = TaskStore(root=tmp_path)
    task = store.create("g", session_id="s9")
    task.run_ids.append("r1")
    d = to_dict(task)
    # 显式挑字段：新增字段不漏进响应（/api/runs 同纪律）
    assert set(d) == {
        "task_id", "goal", "session_id", "repo_root", "status", "run_ids",
        "artifacts", "billing", "continuations", "started_at", "created_at",
        "updated_at", "cancel_requested", "fuse_fired", "blocked_reason",
    }
    # 产物=引用，不新建存储
    assert d["artifacts"] == {"session_id": "s9", "run_ids": ["r1"]}
    assert d["billing"]["totals"] == {}
