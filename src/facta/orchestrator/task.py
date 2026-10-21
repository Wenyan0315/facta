"""Task 一等实体（ADR 115 §1/§3/§4）：长期委派的记账本 + 续跑判定。

Task 1 : 1 Session : N Run——Task 是一段长期委派，Session 是它的上下文
（038 底片与 checkpoint 恢复沿用），每次执行/续跑 = 同一 Session 上的**新 Run**。
自动续跑是编排层循环（app.py 的 _task_worker），不进内核：run_turn 的
「一轮」语义、审计/确认/沙箱/围栏/checkpoint 全部不动（114 架构红线）。

状态机（waiting-approval 为**派生映射**，不落 Task 字段——序列化时由 app 层
读在飞 Run 的 status 映射，避免双份状态机）：

    queued → running → waiting-steer | blocked → done | failed | cancelled

分层纪律：本模块不 import server/（Web 壳在上层，倒依赖是层污染）。
should_continue 只读 run 的 status/events 字段（鸭子类型）；Run 状态字面量
与 run_store 常量同值，用 tests/test_task.py 的钉子测试锁死漂移。

落盘（TaskStore）：内存 dict 为读取快路径，data/tasks/<id>.json 为持久真值源。
落盘时机 = **状态迁移 + Run 边界**（不是「只在状态迁移」——那样 run_ids/
billing 在连续续跑期间永远不落盘，与「可追溯」自相矛盾，评审问题 5）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from facta.memory.plan import StepStatus
from facta.paths import TASKS_DIR

# ---- 保险丝常量（§3；env FACTA_* 可覆写，与 _STUCK_LIMIT 同款就位）----

TASK_MAX_CONTINUATIONS = int(os.environ.get("FACTA_TASK_MAX_CONTINUATIONS", "20"))
TASK_MAX_WALL_CLOCK_S = float(os.environ.get("FACTA_TASK_MAX_WALL_CLOCK_S", "7200"))
TASK_MAX_COST = (
    float(os.environ["FACTA_TASK_MAX_COST"])
    if os.environ.get("FACTA_TASK_MAX_COST")
    else None   # 缺省不限成本（076 逐轮 settle 的反方登记见 ADR，成本上限留给自用观察后再定）
)

# 续跑起新 Run 被 create_if_idle 拒（全局槽位满）时的退避重试（评审问题 3）：
# 2s × 15 次 ≈ 30s 总窗。不改 RunStore 并发语义、不为任务预留槽位（§3 裁定）
CONTINUE_RETRY_INTERVAL_S = 2.0
CONTINUE_RETRY_TIMES = 15

# ---- Task 状态常量 ----

TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_WAITING_STEER = "waiting-steer"
TASK_BLOCKED = "blocked"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

TASK_TERMINAL = {TASK_DONE, TASK_FAILED, TASK_CANCELLED}

# Run 状态字面量（与 server/run_store.py 同值；不 import——见模块头「分层纪律」，
# 漂移由 tests/test_task.py::test_run_status_literal_pinned 钉住）
RUN_STATUS_COMPLETED = "completed"

# 首条消息的极简 kickoff 提示（§3：人设化归 M2 skill，M1 只一句，不新建人设）
KICKOFF_HINT = "（复杂任务请先用 make_plan 列出计划，再逐步推进。）"


@dataclass
class Task:
    """一段长期委派的账目。只记账不执行——执行永远是 Run 的事。

    repo_root 在 M1 **只登记不产生围栏**（§1 明写）：chat 模式的任务不因此
    产生文件围栏（围栏仍归 WORKSPACE_ROOT），项目隔离是 M2 的事。
    artifacts 不落字段：产物 = Run 引用（run_ids）+ 会话引用（session_id），
    序列化时现推（114 裁定「不新建存储」），不搞第二真值源。
    """

    task_id: str
    goal: str
    session_id: str = ""
    repo_root: str = ""
    status: str = TASK_QUEUED
    run_ids: list[str] = field(default_factory=list)
    billing: dict = field(default_factory=dict)   # {"runs": {rid: usage}, "approx_runs": [rid]}
    continuations: int = 0
    started_at: float = 0.0   # 时长保险丝基线（resume 时重置）
    cost_base: float = 0.0    # 成本保险丝基线（resume 时重置为当时合计）
    created_at: float = 0.0
    updated_at: float = 0.0
    cancel_requested: bool = False
    fuse_fired: bool = False
    blocked_reason: str = ""   # 落 blocked 的原因（否则「在跑」与「被拒」分不清）


def usage_delta(before: Any, after: Any) -> dict[str, float]:
    """UsageLedger 两次快照的差值 → 本轮成本切片。

    UsageLedger 全字段为数字（telemetry.py），机械逐字段相减即可；
    快照 = dataclasses.replace 拷贝（app 层做）。
    """
    return {f.name: getattr(after, f.name) - getattr(before, f.name) for f in fields(before)}


def billing_add(task: Task, run_id: str, usage: dict, approx: bool) -> None:
    """Run 收尾时把账单切片并入任务（Run 边界落盘由调用方接着做）。"""
    task.billing.setdefault("runs", {})[run_id] = usage
    if approx:
        approx_runs = task.billing.setdefault("approx_runs", [])
        if run_id not in approx_runs:
            approx_runs.append(run_id)


def billing_totals(task: Task) -> dict[str, float]:
    """合计 = Σ run.usage（推导值，不落盘——落盘的只有明细，合计永不漂移）。"""
    total: dict[str, float] = {}
    for usage in task.billing.get("runs", {}).values():
        for key, value in usage.items():
            total[key] = total.get(key, 0) + value
    return total


def to_dict(task: Task) -> dict:
    """显式挑字段序列化（与 /api/runs 同纪律：新增字段不漏进响应）。"""
    run_ids = list(task.run_ids)
    return {
        "task_id": task.task_id,
        "goal": task.goal,
        "session_id": task.session_id,
        "repo_root": task.repo_root,
        "status": task.status,
        "run_ids": run_ids,
        "artifacts": {"session_id": task.session_id, "run_ids": run_ids},   # 产物=引用，不新建存储
        "billing": {
            "totals": billing_totals(task),
            "approx_runs": list(task.billing.get("approx_runs", [])),
        },
        "continuations": task.continuations,
        "started_at": task.started_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "cancel_requested": task.cancel_requested,
        "fuse_fired": task.fuse_fired,
        "blocked_reason": task.blocked_reason,
    }


class TaskStore:
    """内存 dict 快路径 + data/tasks/<id>.json 持久真值源。

    目录创建归这里（mkdir parents）；原子写（tmp + os.replace）防半截 JSON。
    升级信号：多实例部署换外置存储（与 RunStore 同一取舍）。
    """

    def __init__(self, root: Path | None = None) -> None:
        # 懒取 TASKS_DIR（函数体里读全局）：测试 monkeypatch 模块常量即可隔离
        self.root = root or TASKS_DIR
        self._tasks: dict[str, Task] = {}

    def _path(self, task_id: str) -> Path:
        # task_id 是程序生成的 uuid hex，不像会话 id 那样有自由字符串入口；
        # 从磁盘加载的路径仍做一次兜底（信任边界）
        if not task_id or any(c not in "0123456789abcdef" for c in task_id):
            raise ValueError(f"非法 task_id：{task_id}")
        return self.root / f"{task_id}.json"

    def create(self, goal: str, session_id: str = "", repo_root: str = "") -> Task:
        now = time.time()
        task = Task(
            task_id=uuid.uuid4().hex,
            goal=goal,
            session_id=session_id,
            repo_root=repo_root,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.task_id] = task
        self.save(task)   # 创建即落盘（状态迁移的第一次）
        return task

    def get(self, task_id: str) -> Task | None:
        """先问内存，再问磁盘（重启后仍可 resume/查询——落盘的意义所在）。"""
        task = self._tasks.get(task_id)
        if task is not None:
            return task
        try:
            path = self._path(task_id)
        except ValueError:
            return None
        if not path.is_file():
            return None
        task = Task(**json.loads(path.read_text(encoding="utf-8")))
        self._tasks[task_id] = task
        return task

    def list(self) -> list[Task]:
        """清单，新的在前（与 /api/runs 同款语义）。磁盘文件是超集（含重启前的）。"""
        ids = {p.stem for p in self.root.glob("*.json")} | set(self._tasks)
        tasks = [t for tid in ids if (t := self.get(tid)) is not None]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def save(self, task: Task) -> None:
        """快照落盘（调用时机 = 状态迁移 + Run 边界，见 ADR §1）。原子写。

        落盘格式 = dataclass 字段（asdict），与 API 视图 to_dict **刻意分开**：
        to_dict 是显式挑字段的响应形状（billing 折成 totals、artifacts 现推），
        拿它落盘会让 get 的 `Task(**json)` 炸在多余键与错形状上。落盘字段的
        真值源就是 Task 定义本身——加字段 asdict 自动跟上，不第二处维护清单。
        """
        task.updated_at = time.time()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(task.task_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


# ---- 续跑判定（§3）与 done 判据（评审问题 4）----


def should_continue(task: Task, run: Any, session: Any) -> bool:
    """四条全满足才续跑。run 用鸭子类型（分层纪律，见模块头）。

    1. run.status == completed（读 Run 置态，不信 RunResult 回传）
    2. 本轮未触发 stuck（原地踏步 → 等人改向，不该自动续）
    3. 活跃计划存在且未全终态（计划是续跑的契约；无计划＝一发）
    4. 未触保险丝且未取消
    """
    if task.cancel_requested or task.fuse_fired:
        return False
    if run.status != RUN_STATUS_COMPLETED:
        return False
    if any(getattr(e, "type", "") == "stuck" for e in run.events):
        return False
    plan = session.plan.view()
    return plan is not None and not plan.is_complete()


def fuse_expired(task: Task) -> str | None:
    """保险丝判据（§3）：到期返回原因，未到期 None。

    看守线程与轮间判定共用这一份（单一判据不漂移）。成本口径 = 任务启动/
    resume 以来的增量（cost_base 是基线，resume 重置——否则一恢复就再熔断）。
    """
    if task.continuations >= TASK_MAX_CONTINUATIONS:
        return f"续跑次数达上限（{TASK_MAX_CONTINUATIONS}）"
    if time.time() - task.started_at > TASK_MAX_WALL_CLOCK_S:
        return f"任务时长超上限（{TASK_MAX_WALL_CLOCK_S:.0f} 秒）"
    if TASK_MAX_COST is not None:
        spent = billing_totals(task).get("llm_cost", 0.0) - task.cost_base
        if spent >= TASK_MAX_COST:
            return f"任务成本超上限（{TASK_MAX_COST}）"
    return None


def _verdict_plan(session: Any) -> Any:
    """任务裁决用的计划快照：优先活跃计划，无活跃看最后一份归档。

    归档不吸收失败（ADR 问题 4 裁定的补口，实机验收发现）：finish_plan 收官
    后 plan.view() 为 None，只读活跃计划会把「含 failed 的计划一归档」洗白成
    done——finish_plan 变成失败后门。与 tools/plan.py 的 _record_failures
    同款 archive[-1] 姿势：收官时那一轮的计划就是本次裁决对象；更早归档的
    计划视为被后续计划取代（多计划粒度裁定，ADR 115 补注）。
    """
    plan = session.plan.view()
    if plan is not None:
        return plan
    archive = session.plan.archive
    return archive[-1].view() if archive else None


def _is_task_done(session: Any) -> bool:
    """done 自数判据：全 done/skipped 才算完；无计划的委派一发即 done。

    不能用 Plan.is_complete()——它把 FAILED 也算终态（plan.py:44-46），
    拿它当 done 会把「含失败步骤的任务」误判成成功（评审问题 4）。
    """
    plan = _verdict_plan(session)
    if plan is None:
        return True
    return all(s.status in (StepStatus.DONE, StepStatus.SKIPPED) for s in plan.steps)


def _plan_has_failed(session: Any) -> bool:
    """计划含 failed 步骤 → 任务 failed（§1 状态机：出现 failed 即 failed）。"""
    plan = _verdict_plan(session)
    return plan is not None and any(s.status is StepStatus.FAILED for s in plan.steps)


def continue_message(session: Any) -> str:
    """续跑注入消息（§3）：显式标注的续跑指令，**刻意不用 user_text=None**
    ——那是 038 崩溃续跑语义（不追加提问、heal 悬挂调用），混用污染边界。
    """
    plan = session.plan.view()
    ids = [str(s.id) for s in plan.steps if not s.status.is_terminal] if plan else []
    if not ids:
        # ponytail: 降级文案而非空串拼接（原文会输出「未终态步骤 ，」）——
        # 此路径实际难达（should_continue 保证有未终态步骤才续跑），resume
        # 代打也可能撞上，宁可啰嗦不产出病句
        return "[自动续跑] 计划已无未终态步骤，请核对完成情况；若已全部完成请明确声明。"
    marks = "/".join(f"#{i}" for i in ids)
    return f"[自动续跑] 计划尚有未终态步骤 {marks}，请继续推进；若已全部完成请明确声明。"
