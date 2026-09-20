"""plan 域（S5b）：plan-then-act 的数据与状态机。

语义属编排层（agent 视角的工作分解，021 重定义），物理归属记忆域：
生命周期=会话（跨轮持久、随 session.json 落盘），Session 持有它——
依赖方向（memory 不 import orchestrator）决定它住这里。

三拍板（见 027 开工前）：
- 触发：模型自判——make_plan 是普通工具，点不点模型定（复杂度是语义概念）
- 修订：append 事件史不改写——「当前计划」= view() fold 事件推导，
  「为什么跳了第 3 步」永远可查（事件史是复盘资产）
- 完成：显式终态制——全步骤终态化 + finish 收官声明，缺一不可

结构（值对象 → 单计划状态 → 会话棋盘三层）：
- Step/Plan/StepStatus/PlanEvent   值对象（frozen，纯数据可测试）
- PlanState   单个计划：事件史 + 校验 + fold 视图。create 后住 board.active，
              finish 后进 board.archive（status 留 "finished" 供复盘）
- PlanBoard   会话级棋盘：单活跃 + 归档 + 传输队列。make_plan 内部分叉
              created/revised（修订复用同工具，027）；finish_plan 做
              归档迁移。_pending 挂 board 不挂 state：finish 事件发生在
              归档迁移之前，挂 state 会漏（drain 只查 active 就丢收官事件）
              ——events 归 state（史），pending 归 board（传输）
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

MAX_STEPS = 10   # 步数上限：防模型一次发疯列 50 步；真需要拆任务就该 spawn 子任务


class StepStatus(Enum):
    """步骤状态机：pending 起步，三个终态互斥（027 拍板③显式终态制）。

    skipped 不是漏洞是一等公民（「发现第 3 步不需要了」是执行常态），
    但必须带 note；failed 与 done 的区别在结果不在态度（做了没成 ≠ 没做）。
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.FAILED)


@dataclass(frozen=True)
class Step:
    """计划的一步：最小结构只有四个字段，多一个都是过度设计。"""

    id: int
    title: str
    status: StepStatus = StepStatus.PENDING
    note: str = ""   # skipped/failed 必填（理由）；done 的结果一句话（可选）


@dataclass(frozen=True)
class Plan:
    """任务的工作分解视图。frozen=不可变：由事件流 fold 出来（view()），
    修订从不改对象——「当前计划」永远是推导值，不是存储值。
    """

    steps: tuple[Step, ...]

    def is_complete(self) -> bool:
        """完成判据（拍板③）：全部步骤显式终态化才算完。"""
        return all(s.status.is_terminal for s in self.steps)


@dataclass(frozen=True)
class PlanEvent:
    """计划事件：append-only 事件史的最小单元（type + 载荷，JSON 可落盘）。"""

    type: str    # plan.created / plan.revised / plan.step_updated / plan.finished
    data: dict


def _check_steps_shape(steps: list[dict], *, revision: bool) -> list[dict]:
    """步骤表形状校验（程序管的部分）：数量、title 非空、修订时 status 必填。

    修订表强制显式 status：迫使模型对照旧视图逐声明（继承 done 的写 done，
    重排的写 pending）——缺省会静默丢执行史，显式才可审计。
    """
    if not steps or len(steps) > MAX_STEPS:
        raise ValueError(f"步骤数须在 1..{MAX_STEPS}（当前 {len(steps)}）；更大的任务请拆分")
    for s in steps:
        title = str(s.get("title", "")).strip()
        if not title:
            raise ValueError("每一步必须有非空 title")
        if revision and not s.get("status"):
            raise ValueError("修订计划时每一步必须显式声明 status（对照旧计划继承）")
    return steps


class PlanState:
    """单个计划：事件史 + 校验 + fold 视图。

    自足单元（史自己管）；传输（_pending）归 board——见模块 docstring。
    所有变更方法：先校验后追加，非法转移抛 ValueError（工具层转错误串
    回灌，模型自纠）。事件里 status 存字符串（JSON 落盘），白名单校验
    在入口做，fold 不重复设防（单一闸门）。
    """

    def __init__(self) -> None:
        self.status: str = "active"           # active / finished（终局标记，供复盘读）
        self.events: list[PlanEvent] = []      # 全量事件史（append-only，落盘）

    # ---- 变更入口（board 调用；校验过的事件同时返回给 board 入传输队列）----

    def create(self, steps: list[dict]) -> PlanEvent:
        """新建计划：id 程序分配 1..N（编号是定位键，程序是唯一可信作者——
        时间戳同款哲学），强制全 pending（执行史从零开始）。
        """
        _check_steps_shape(steps, revision=False)
        ev = PlanEvent(
            "plan.created",
            {"steps": [{"id": i, "title": s["title"].strip()} for i, s in enumerate(steps, 1)]},
        )
        self.events.append(ev)
        return ev

    def revise(self, reason: str, steps: list[dict]) -> PlanEvent:
        """修订计划：换表（新 id 重排），新表自带 status/note——模型从旧视图
        继承的显式声明。reason 记进事件：修订不带原因 = 审计断档。
        """
        _check_steps_shape(steps, revision=True)
        table = []
        for i, s in enumerate(steps, 1):
            status = StepStatus(str(s["status"]))   # 白名单外值在此炸（入口闸门）
            table.append({
                "id": i,
                "title": s["title"].strip(),
                "status": status.value,
                "note": str(s.get("note", "")),
            })
        ev = PlanEvent("plan.revised", {"reason": reason, "steps": table})
        self.events.append(ev)
        return ev

    def update_step(self, step_id: int, status: str, note: str = "") -> PlanEvent:
        """状态回写：先校验后追加。校验读 view() 的 fold 结果——校验与
        视图同源，不会出现「校验一个状态、显示另一个」的裂缝。
        """
        current = self.view()
        old = next((s for s in current.steps if s.id == step_id), None)
        if old is None:
            raise ValueError(
                f"步骤 #{step_id} 不在当前计划里（现有：{[s.id for s in current.steps]}）；"
                "若计划已修订，请以最新计划为准"
            )
        try:
            new = StepStatus(status)
        except ValueError:
            raise ValueError(
                f"未知状态「{status}」，合法值：{[s.value for s in StepStatus]}"
            ) from None
        if old.status.is_terminal:
            raise ValueError(
                f"步骤 #{step_id} 已是终态 {old.status.value}，不可再改；计划有变请走 make_plan 修订"
            )
        if new.is_terminal and not note.strip():
            raise ValueError(f"终态 {new.value} 必须带 note（结果/跳过理由/失败原因）")

        ev = PlanEvent("plan.step_updated", {"id": step_id, "status": new.value, "note": note})
        self.events.append(ev)
        return ev

    def finish(self, summary: str) -> PlanEvent:
        """收官：显式终态制的程序闸——有 pending/in_progress 悬空即拒（模型
        去补终态或修订），全终态才放行。summary 收进事件（收官声明入史）。
        """
        view = self.view()
        dangling = [s.id for s in view.steps if not s.status.is_terminal]
        if dangling:
            raise ValueError(
                f"步骤 {dangling} 尚未终态化（pending/in_progress 悬空），"
                "先逐个 update_plan_step 到 done/skipped/failed 再收官"
            )
        ev = PlanEvent("plan.finished", {"summary": summary})
        self.events.append(ev)
        self.status = "finished"
        return ev

    # ---- 读侧 ----

    def view(self) -> Plan:
        """fold 事件史出当前视图（纯函数语义：不改任何状态）。

        修订换表后，旧 step_updated 事件指向的 id 可能已不在新表——
        跳过（当时合法的历史事实，不因后来的修订变非法）。不做缓存：
        事件几十条 × dict 操作 = 微秒级，缓存要管失效时机，YAGNI。
        """
        steps: dict[int, Step] = {}
        for ev in self.events:
            if ev.type == "plan.created":
                steps = {s["id"]: Step(id=s["id"], title=s["title"]) for s in ev.data["steps"]}
            elif ev.type == "plan.revised":
                steps = {
                    s["id"]: Step(
                        id=s["id"], title=s["title"],
                        status=StepStatus(s["status"]), note=s.get("note", ""),
                    )
                    for s in ev.data["steps"]
                }
            elif ev.type == "plan.step_updated":
                old = steps.get(ev.data["id"])
                if old is not None:   # 修订重排后的孤儿事件：跳过
                    steps[old.id] = replace(
                        old,
                        status=StepStatus(ev.data["status"]),
                        note=ev.data.get("note", ""),
                    )
        return Plan(steps=tuple(steps[i] for i in sorted(steps)))

    # ---- 序列化（session.json 落盘；_pending 不落盘——它是本轮传输队列）----

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "events": [{"type": e.type, "data": e.data} for e in self.events],
        }

    @classmethod
    def from_dict(cls, d: dict) -> PlanState:
        state = cls()
        state.status = d.get("status", "active")
        state.events = [PlanEvent(e["type"], e["data"]) for e in d.get("events", [])]
        return state


class PlanBoard:
    """会话级棋盘：单活跃 + 归档 + 传输队列。

    生命周期收口在这里：make_plan 分叉 created/revised（模型心智最简——
    「要改计划就点 make_plan」）、finish 后归档迁移（状态机语义不进工具
    闭包）。session.plan 恒持有一个 board（空板也合法：无活跃计划）。
    """

    def __init__(self) -> None:
        self.active: PlanState | None = None
        self.archive: list[PlanState] = []
        self._pending: list[PlanEvent] = []   # 本轮待转发（run_turn drain → on_event）

    def _emit(self, state: PlanState, ev: PlanEvent) -> None:
        """一体两面追加：state.events.append 在变更方法内已完成，board 只补
        传输队列——漏一边就是「改了状态没发事件」或「前端和模型看到的不同」。
        """
        self._pending.append(ev)

    def make_plan(self, steps: list[dict], reason: str = "") -> str:
        """分叉入口：无活跃 → create；有活跃 → revise（修订复用同工具）。
        返回结果标签（"created"/"revised"）给工具层组提示。
        """
        if self.active is None:
            state = PlanState()
            self._emit(state, state.create(steps))
            self.active = state
            return "created"
        if not reason.strip():
            raise ValueError("已有活跃计划，再次 make_plan 是修订——必须带 reason 说明为什么改")
        self._emit(self.active, self.active.revise(reason, steps))
        return "revised"

    def update_step(self, step_id: int, status: str, note: str = "") -> None:
        if self.active is None:
            raise ValueError("当前没有活跃计划，先 make_plan 再回写步骤状态")
        ev = self.active.update_step(step_id, status, note)
        self._emit(self.active, ev)

    def finish_plan(self, summary: str) -> None:
        """收官 + 归档迁移：校验失败抛 ValueError（计划留在 active 继续改），
        成功则 state 进 archive、active 置 None（新任务从 create 重新开始）。
        """
        if self.active is None:
            raise ValueError("当前没有活跃计划")
        ev = self.active.finish(summary)
        self._emit(self.active, ev)
        self.archive.append(self.active)
        self.active = None

    def view(self) -> Plan | None:
        """活跃计划视图；无活跃 → None（调用方据此不注入投影）。"""
        return self.active.view() if self.active is not None else None

    def drain(self) -> list[PlanEvent]:
        """取走待转发事件（run_turn 每次工具执行后调）。"""
        out, self._pending = self._pending, []
        return out

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        return {
            "active": self.active.to_dict() if self.active is not None else None,
            "archive": [s.to_dict() for s in self.archive],
        }

    @classmethod
    def from_dict(cls, d: dict) -> PlanBoard:
        board = cls()
        if d.get("active"):
            board.active = PlanState.from_dict(d["active"])
        board.archive = [PlanState.from_dict(s) for s in d.get("archive", [])]
        return board
