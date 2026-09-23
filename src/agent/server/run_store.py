"""Run Store：内存版任务状态机 + 事件序列（S2b）。

支撑「Run 三接口分离」：任务状态不依赖单次 HTTP 请求的生命周期——
浏览器断开，后台 Run 继续跑，事件落在这里，等重连后按 seq 重放。

课程术语（Run/Step/Attempt 三层执行模型）：
    Run     一次任务，对应 run_id（本模块显式建模的对象）
    Step    一轮「决策→工具→观察」循环（run_turn 内部的 for 轮）
    Attempt 一次模型调用的重试（网关 RobustLLM 内部，已由 M7.5 承载）
v1 只显式建模 Run；Step/Attempt 概念先落在注释里，等 checkpoint 需求
（长任务断点续跑）出现时再建实体——YAGNI。

不变量：一个 Run 只有一个终态（completed/failed/cancelled 三选一）。
事件 append-only（只增不改）——「模型可见即已记录」事件流思想的 v0，
S3 审计流事件化（素材库#8）在此继续长。
"""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field

SCHEMA_VERSION = "1"   # 事件协议版本护栏：未知 type/字段出现时，旧客户端忽略而非崩溃

# Run 状态机：三个活跃态 + 三个互斥终态
STATUS_PENDING = "pending"       # 已创建，后台线程尚未真正启动
STATUS_RUNNING = "running"       # 执行中
STATUS_WAITING = "waiting_approval"   # S4b：挂起等用户裁决 L2 确认（活跃态，非终态）
STATUS_COMPLETED = "completed"   # 正常终态
STATUS_FAILED = "failed"         # 模型挂/异常终态
STATUS_CANCELLED = "cancelled"   # 用户取消终态

_TERMINAL = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}
_IN_FLIGHT = {STATUS_PENDING, STATUS_RUNNING, STATUS_WAITING}   # 单锁口径：等待确认也算在跑


@dataclass
class RunEvent:
    """Run 内的一条已发生事实。seq 是它的位置键（排序/去重/重放）。

    run_id / schema_version 不属于事件本身——由 SSE 编码层从 Run 补充，
    让事件保持「内容 + 序号」的纯粹性（同一个事件对象可被多个协议消费）。
    """

    seq: int
    type: str
    data: dict = field(default_factory=dict)


@dataclass
class Run:
    """一次任务的运行状态：run_id + 状态机 + 事件序列 + 取消标志 + 订阅队列。

    title/preview 是任务视图的展示字段（人读的标签，不参与状态机）：
    title=创建时的用户消息截断，preview=最终回复截断（完成时回填）。
    """

    run_id: str
    status: str = STATUS_PENDING
    title: str = ""
    preview: str = ""
    events: list[RunEvent] = field(default_factory=list)
    cancel_requested: bool = False
    confirm_pending: bool = False   # S4b：是否正挂着一个待裁决的 L2 确认（confirm 端点判据）
    _subscribers: list = field(default_factory=list, repr=False)   # 广播模型（评审修复轮）：每连接独立队列——此前单队列，聊天页+任务页同时订阅同一 Run 时事件被随机分食（竞争消费）
    _seq: int = 0
    _confirm_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _confirm_decision: bool | None = field(default=None, repr=False)
    # S6b 并行确认锁：单槽位（confirm_pending/_confirm_decision）在多 spawn
    # 并行时会被踩——两个子 agent 同时触发确认，第二个 clear event 把第一个
    # 的裁决通道冲掉。锁串行化裁决：同一时刻只处理一个确认，其余排队——
    # 语义正确（人一次只能看一个确认弹窗，确认本就该串行）。
    _confirm_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def emit(self, type: str, data: dict | None = None) -> RunEvent:
        """记录一条事件并广播给所有订阅者（append-only，seq 自增）。"""
        event = RunEvent(seq=self._next_seq(), type=type, data=data or {})
        self.events.append(event)
        for q in self._subscribers:
            q.put(event)
        return event

    def subscribe(self) -> queue.Queue:
        """新订阅者获得独立队列（广播模型）；断开时必须 unsubscribe 防泄漏。

        终态补发哨兵：Run 已结束时才连接的订阅者错过了已广播的 None——
        单队列时代靠积压天然送达，广播模型必须显式补（否则流永不收口，
        晚到的客户端死等心跳——TestClient 挂起事故的根因）。重放段已含
        run.completed 终态事件，补发哨兵后流可正常结束。
        """
        q: queue.Queue = queue.Queue()
        self._subscribers.append(q)
        if self.status in _TERMINAL:
            q.put(None)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """SSE 连接断开时退订（generator finally 调用）；不退则队列引用滞留。"""
        if q in self._subscribers:
            self._subscribers.remove(q)

    def finish(self, status: str) -> None:
        """推进到终态并通知所有订阅者结束。只有一个终态——重复调用不覆盖。

        终态事件（run.completed/failed/cancelled）也进事件流：客户端靠它渲染
        最终结果，不能只靠「流断了」来推断成功还是失败。
        """
        if self.status not in _TERMINAL:
            self.status = status
            self.emit(f"run.{status}", {})   # 终态事件先进流（广播给全部订阅者）
            for q in list(self._subscribers):
                q.put(None)            # 再 sentinel 结束流（只在首次终态推进时发一次）

    def request_cancel(self) -> bool:
        """请求取消：只在尚未终态时有效，终态后取消是无效操作。"""
        if self.status in _TERMINAL:
            return False
        self.cancel_requested = True
        return True

    def request_confirm(self, tool: str, arguments: dict) -> bool:
        """S4b L2 裁决挂起：worker 线程内调用，阻塞到用户落子或取消。

        状态翻转 running→waiting_approval→running；confirm.request /
        confirm.resolved 都进 append-only 事件流——断线重放时，request
        无对应 resolved 则前端重新弹窗（确认不随断线丢失）。
        等待中取消视为拒绝：确认挂起不挡取消通道。
        """
        # S6b 并行确认锁：整个「挂起→等裁决→复位」临界区串行化。并行
        # spawn 的多个确认请求排队处理——单槽位不会被并发 clear/write 踩踏
        # （旧代码两个子 agent 同时确认时，第二个 clear 会把第一个的裁决
        # 通道冲掉）。wait 循环在锁内，第二个确认等第一个裁决完才进入。
        with self._confirm_lock:
            prev, self.status = self.status, STATUS_WAITING
            self.confirm_pending = True
            self._confirm_event.clear()
            self._confirm_decision = None
            self.emit("confirm.request", {"tool": tool, "arguments": arguments})
            while True:
                if self._confirm_event.wait(timeout=0.2):
                    approved = bool(self._confirm_decision)
                    break
                if self.cancel_requested:
                    approved = False
                    break
            self.confirm_pending = False
            self.status = prev
            self.emit("confirm.resolved", {"tool": tool, "approved": approved})
            return approved

    def resolve_confirm(self, approved: bool) -> bool:
        """用户裁决落子（confirm 端点调用）；无 pending 确认时返回 False。"""
        if not self.confirm_pending:
            return False
        self._confirm_decision = approved
        self._confirm_event.set()
        return True


class RunStore:
    """内存版多 Run 容器（教学版）。

    升级触发信号：>1 进程/多实例部署时换 Redis/DB（课件 Run Store 段的
    「先内存打通事件协议，再换底层」）。
    """

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()   # 保护 _runs 与「单锁」检查+创建的原子性

    def create(self, title: str = "") -> Run:
        run = Run(run_id=uuid.uuid4().hex, title=title)
        with self._lock:
            self._runs[run.run_id] = run
        return run

    def create_if_idle(self, title: str = "") -> Run | None:
        """原子地「无 in-flight 才创建」——单锁的检查与创建不可分割。

        单锁理由：会话是单内存状态（session 是共享可变对象），并发两个 Run
        会互相踩 session.messages。多并发留给 S6（worktree/子 agent 隔离）。
        """
        with self._lock:
            if any(r.status in _IN_FLIGHT for r in self._runs.values()):
                return None
            run = Run(run_id=uuid.uuid4().hex, title=title)
            self._runs[run.run_id] = run
            return run

    def list_runs(self) -> list[Run]:
        """全部 Run，新的在前（任务视图原料）。

        dict 保插入序 = 创建序；v1 全在内存、重启即空（已知边界，外置触发
        信号=多实例部署）。
        """
        with self._lock:
            return list(reversed(self._runs.values()))

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def active_run(self) -> Run | None:
        """当前唯一 in-flight 的 Run（供诊断/测试；单锁由 create_if_idle 原子保证）。"""
        with self._lock:
            for run in self._runs.values():
                if run.status in _IN_FLIGHT:
                    return run
            return None
