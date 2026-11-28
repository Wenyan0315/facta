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

import logging
import queue
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:   # 循环引用护栏：run_log import 本模块的 RunEvent
    from facta.server.run_log import RunLog

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"   # 事件协议版本护栏：未知 type/字段出现时，旧客户端忽略而非崩溃

# Run 状态机：三个活跃态 + 四个互斥终态
STATUS_PENDING = "pending"       # 已创建，后台线程尚未真正启动
STATUS_RUNNING = "running"       # 执行中
STATUS_WAITING = "waiting_approval"   # S4b：挂起等用户裁决 L2 确认（活跃态，非终态）
STATUS_COMPLETED = "completed"   # 正常终态
STATUS_FAILED = "failed"         # 模型挂/异常终态
STATUS_CANCELLED = "cancelled"   # 用户取消终态
STATUS_INTERRUPTED = "interrupted"   # M5：孤儿轮——服务被杀留下的未收尾轮次，从盘上重建时按此收口

_TERMINAL = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED}
_IN_FLIGHT = {STATUS_PENDING, STATUS_RUNNING, STATUS_WAITING}   # 准入口径：等待确认也算在跑（人在裁决，会话仍被占着）


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
    reply_text 是 headless 的完整回复文本（ADR 081）——preview 截 300 字
    不够给外部自动化，二者并存：preview 喂任务视图，reply_text 喂同步端点。
    """

    run_id: str
    # S8a：这个 Run 属于哪段对话。session_id 是并发隔离的单位——「同一会话只能
    # 有一个在跑的 Run」（并发会互踩 session.messages），不同会话可并行。
    session_id: str = ""
    status: str = STATUS_PENDING
    title: str = ""
    preview: str = ""
    reply_text: str = ""   # ADR 081：完整最终回复（headless 同步端点原料）
    events: list[RunEvent] = field(default_factory=list)
    created_at: float = 0.0   # M5：落盘元快照的排序键（跨重启保「新的在前」）
    cancel_requested: bool = False
    confirm_pending: bool = False   # S4b：是否正挂着一个待裁决的 L2 确认（confirm 端点判据）
    # ---- ADR 115（M1）：Task 归属 + steering 信箱 + per-Run 账单切片 ----
    task_id: str = ""        # 所属 Task；""= 未纳入任务（既有调用方逐字节不受影响）
    steer_pending: str = ""  # steering 信箱（累积式：两次纠偏都得进上下文，不互相覆盖）
    fuse_fired: bool = False   # 任务保险丝熔断标记（与用户取消区分开，_task_worker 判定读）
    usage: dict = field(default_factory=dict)   # per-Run 账单切片（UsageLedger 快照差，收尾时回填）
    usage_approx: bool = False   # 窗口内有并发 Run 在飞 → 归因为近似（显式标注，绝不静默串账）
    _subscribers: list = field(default_factory=list, repr=False)   # 广播模型（评审修复轮）：每连接独立队列——此前单队列，聊天页+任务页同时订阅同一 Run 时事件被随机分食（竞争消费）
    _seq: int = 0
    _confirm_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _confirm_decision: bool | None = field(default=None, repr=False)
    # S6b 并行确认锁：单槽位（confirm_pending/_confirm_decision）在多 spawn
    # 并行时会被踩——两个子 agent 同时触发确认，第二个 clear event 把第一个
    # 的裁决通道冲掉。锁串行化裁决：同一时刻只处理一个确认，其余排队——
    # 语义正确（人一次只能看一个确认弹窗，确认本就该串行）。
    _confirm_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # 059 事件写入锁：spawn 并行时各 worker 线程都会 emit（此前只有确认缝
    # 跨线程，059 起子 agent 的 sub.* 过程事件也常态跨线程）。_next_seq 是
    # `self._seq += 1` 非原子，并发下会重号（seq 是排序/去重/重放的位置键，
    # 重号=客户端可能漏读一条）。修共享收口点一次，不在调用侧各自加锁。
    # RLock：finish 在锁内串起「翻终态 → emit → 哨兵」需要重入 emit（059 原子性
    # 不变，只是终态序列整体纳入临界区）
    _emit_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # ADR 115 steering 信箱锁：「取出」与「置空」必须原子（ide_ctx 闭包锁同款
    # 理由），且与「投递」互斥——防止 append 与 take 交错丢件。独立于
    # _emit_lock：信箱不发事件，两把锁各管各的临界区。
    _steer_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # ADR 115：本轮终局 Session 挂给 _task_worker 判定读（续跑要看 session.plan
    # ——session 是 _run_worker 的局部变量，不挂出来宿主循环拿不到）
    _final_session: Any = field(default=None, repr=False)
    # ADR 076 收官同步点：worker 的异步 settle 跑在 SSE 流 sentinel 收口
    # 之后，测试无法再靠「读完事件流」推断收官完成——等这个 Event。
    # 生产逻辑零参与（只 set 不 wait）。
    _settle_done: threading.Event = field(default_factory=threading.Event, repr=False)
    # M5 承诺件 a：Run 明细落盘缝（RunLog；None=不持久化，直造 Run 的单测同旧）
    _log: Any = field(default=None, repr=False)
    # M5 承诺件 b：事件外发缝（app 层注入总线转发）。守卫在 emit 收口点——
    # 回调失败不拖垮 run_turn；回调不得回发本 Run 事件（嵌套 emit 会撕破
    # 「seq 分配到广播」的原子序，RLock 能跑但语义不许）
    on_event: Callable[[Run, RunEvent], None] | None = field(default=None, repr=False)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def emit(self, type: str, data: dict | None = None) -> None:
        """记录一条事件并广播给所有订阅者（append-only，seq 自增）。

        返回 None 而不是 RunEvent：emit 的一个主要用途是直接当 run_turn 的
        on_text/on_event 回调，而那两处要 Callable[..., None]——返回事件会让
        lambda 形式过不了类型检查。要读事件走 run.events。

        整体持锁（059）：seq 分配、events.append、广播三步必须原子——
        否则两个线程可能先各自拿到 seq 再乱序 append，重放段与实时流顺序打架。
        M5 起落盘与外发也在锁内（先落盘再广播，checkpoint 同款顺序——客户端
        看见的事实必然已在盘上），转发顺序与 seq 顺序一致。
        """
        event = RunEvent(seq=0, type=type, data=data or {})
        with self._emit_lock:
            event.seq = self._next_seq()
            self.events.append(event)
            if self._log is not None:
                self._log.append_event(self.run_id, event)
            for q in self._subscribers:
                q.put(event)
            if self.on_event is not None:
                try:
                    self.on_event(self, event)
                except Exception:   # 外发缝守卫在收口点（同 _emit_lock 理由）：转发失败不拖垮 run_turn
                    logger.warning("Run 事件转发失败（type=%s）", event.type, exc_info=True)

    def subscribe(self) -> queue.Queue:
        """新订阅者获得独立队列（广播模型）；断开时必须 unsubscribe 防泄漏。

        终态补发哨兵：Run 已结束时才连接的订阅者错过了已广播的 None——
        单队列时代靠积压天然送达，广播模型必须显式补（否则流永不收口，
        晚到的客户端死等心跳——TestClient 挂起事故的根因）。重放段已含
        run.completed 终态事件，补发哨兵后流可正常结束。
        """
        q: queue.Queue = queue.Queue()
        with self._emit_lock:
            self._subscribers.append(q)
            if self.status in _TERMINAL:
                q.put(None)   # 与 finish 的「翻终态→终态事件→哨兵」同锁排队：
                              # 要么排在整段之前（快照重放拿到终态事件），
                              # 要么排在整段之后（早期哨兵 + 快照已含终态事件）
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """SSE 连接断开时退订（generator finally 调用）；不退则队列引用滞留。
        与广播同锁：否则 emit 的迭代可能踩到并发 remove（跳件/炸迭代）。"""
        with self._emit_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def finish(self, status: str) -> None:
        """推进到终态并通知所有订阅者结束。只有一个终态——重复调用不覆盖。

        终态事件（run.completed/failed/cancelled）也进事件流：客户端靠它渲染
        最终结果，不能只靠「流断了」来推断成功还是失败。

        整段持 _emit_lock（RLock，emit 同锁可重入）：「翻终态 → 终态事件进队
        → 哨兵进队」必须对 subscribe 原子。否则 subscribe 落在翻状态与事件
        进队之间时，早期哨兵排到终态事件前面——重放段（快照）没它、实时段
        先见哨兵断流，run.completed 就丢了（CI 慢盘抓到的 flake：write_meta
        的磁盘 IO 把这个纳秒窗口拉成了毫秒级）。
        """
        if self.status not in _TERMINAL:
            with self._emit_lock:
                self.status = status
                if self._log is not None:
                    self._log.write_meta(self)   # M5：终版元快照先进盘（status/preview 就位）
                self.emit(f"run.{status}", {})   # 终态事件先进流（广播给全部订阅者）
                for q in list(self._subscribers):
                    q.put(None)            # 再 sentinel 结束流（只在首次终态推进时发一次）

    def request_cancel(self) -> bool:
        """请求取消：只在尚未终态时有效，终态后取消是无效操作。"""
        if self.status in _TERMINAL:
            return False
        self.cancel_requested = True
        return True

    def request_steer(self, text: str) -> bool:
        """steering 投递（ADR 115）：仅非终态生效；信箱累积而非覆盖。

        覆盖会静默丢掉用户前一条纠偏——两次改向都在下一次模型调用前进上下文。
        """
        if self.status in _TERMINAL:
            return False
        text = text.strip()
        if not text:
            return False
        with self._steer_lock:
            self.steer_pending = f"{self.steer_pending}\n{text}" if self.steer_pending else text
        return True

    def take_steer(self) -> str | None:
        """清空并取出信箱（ADR 115）：「取出」与「置空」原子，空信箱给 None。"""
        with self._steer_lock:
            taken, self.steer_pending = self.steer_pending or None, ""
        return taken

    def request_confirm(self, tool: str, arguments: dict, kind: str = "") -> bool:
        """S4b L2 裁决挂起：worker 线程内调用，阻塞到用户落子或取消。

        状态翻转 running→waiting_approval→running；confirm.request /
        confirm.resolved 都进 append-only 事件流——断线重放时，request
        无对应 resolved 则前端重新弹窗（确认不随断线丢失）。
        等待中取消视为拒绝：确认挂起不挡取消通道。

        kind（ADR 118 §3）：确认的「语义类别」，进事件载荷供 UI 出文案。
        缺省空串 = 普通确认（run_command 动态停 / make_plan 人审）——既有
        调用方一字不改，前端对空 kind 零变化。write 档任务首写停传
        WRITE_AUTH_GUARD（"write-auth"），UI 据此明示「批准后本任务后续写
        免停」（117 观察 3：档位语义此前无载体，只能靠人记住）。
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
            self.emit("confirm.request", {"tool": tool, "arguments": arguments, "kind": kind})
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

    M5 承诺件 a：`log` 挂上 RunLog 后，创建/emit/finish 逐事件落盘，
    get/list_runs 从盘上重建只读 Run（重启后下钻照旧可用）；`on_run_event`
    是事件外发缝（app 层注入任务总线转发）。两者都可不挂（单测同旧）。
    """

    def __init__(
        self,
        max_in_flight: int = 1,
        log: RunLog | None = None,
        on_run_event: Callable[[Run, RunEvent], None] | None = None,
    ) -> None:
        # S8a：并发上限。默认 1 = S2b 起的老语义（全进程只跑一个 Run），
        # 直接构造 RunStore() 的调用方行为不变；服务端从
        # FACTA_MAX_CONCURRENT_RUNS 注入更大的值以支持「长任务期间另开会话聊」。
        self._runs: dict[str, Run] = {}
        self._max_in_flight = max(1, int(max_in_flight))
        self._lock = threading.Lock()   # 保护 _runs 与「准入」检查+创建的原子性
        self.log = log
        self.on_run_event = on_run_event

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    def _adopt(self, run: Run) -> Run:
        """新建 Run 挂缝（M5）：落盘/外发两缝就位 + 元快照初版落盘。

        在 _runs 注册之后、_lock 之外做——写盘不该占着准入锁。
        """
        run.created_at = run.created_at or time.time()
        run._log = self.log
        run.on_event = self.on_run_event
        if self.log is not None:
            self.log.write_meta(run)
        return run

    def create(self, title: str = "", session_id: str = "") -> Run:
        """无条件创建（不走准入检查）——诊断/测试用的后门。"""
        run = Run(run_id=uuid.uuid4().hex, session_id=session_id, title=title)
        with self._lock:
            self._runs[run.run_id] = run
        return self._adopt(run)

    def create_if_idle(self, session_id: str, title: str = "") -> Run | str:
        """原子地「准入通过才创建」——检查与创建不可分割。

        准入两条（S8a 起单锁口径从「全进程一把」收窄到「一段对话一把」）：
          1. 同一会话已有 in-flight → 拒。真正的正确性约束：session.messages
             是共享可变列表，两个 Run 并发 append 会互相踩（工具历史/滚动摘要
             都会错位）。
          2. 全局 in-flight 数达上限 → 拒。资源约束：每个 Run 一个线程 + 一条
             模型长连接，无上限会让「开 20 个标签页」变成 20 路并发。

        返回 Run = 成功；返回 str = 被拒原因（调用方直接当 409 的 detail）。
        不用 None：那会丢掉「该会话在忙」vs「全局满了」的区分，而前端要按
        原因显示不同文案（前者提示切会话，后者只能等）。
        """
        with self._lock:
            in_flight = [r for r in self._runs.values() if r.status in _IN_FLIGHT]
            if any(r.session_id == session_id for r in in_flight):
                return "该会话已有任务在运行，请稍候再发"
            if len(in_flight) >= self._max_in_flight:
                return f"并发任务已达上限（{self._max_in_flight}），请等待其中一个完成"
            run = Run(run_id=uuid.uuid4().hex, session_id=session_id, title=title)
            self._runs[run.run_id] = run
        return self._adopt(run)

    def list_runs(self, session_id: str | None = None) -> list[Run]:
        """Run 清单，新的在前（任务视图原料）；给 session_id 则只列该会话的。

        dict 保插入序 = 创建序。M5 起合并盘上元快照（内存缺的 = 重启前的
        Run）：内存段（全部晚于本进程启动）在前、盘上段按 created_at 降序
        在后，跨重启仍保「新的在前」。列清单只读元快照不读事件。
        """
        with self._lock:
            runs = list(self._runs.values())[::-1]
        seen = {r.run_id for r in runs}
        if self.log is not None:
            disk = [m for m in self.log.list_metas() if m.get("run_id") not in seen]
            disk.sort(key=lambda m: m.get("created_at", 0.0), reverse=True)
            runs.extend(self._from_disk(meta, []) for meta in disk)
        if session_id is not None:
            runs = [r for r in runs if r.session_id == session_id]
        return runs

    def get(self, run_id: str) -> Run | None:
        """先问内存，再问盘（M5：重启后下钻照旧可用；盘上重建的 Run 只读回放）。"""
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None or self.log is None:
            return run
        loaded = self.log.load(run_id)
        if loaded is None:
            return None
        meta, events = loaded
        run = self._from_disk(meta, events)
        with self._lock:
            self._runs[run.run_id] = run   # 缓存：同一 Run 的多次下钻/订阅不再读盘
        return run

    def _from_disk(self, meta: dict, events: list[RunEvent]) -> Run:
        """元快照 + 事件序列 → 只读 Run（M5）。

        非终态元快照 = 服务被杀留下的孤儿轮：按 interrupted 收口（终态之一，
        不占准入槽、SSE 正常收口）——「还在跑」是假话，「失败」也不对。
        """
        status = meta.get("status", "")
        if status not in _TERMINAL:
            status = STATUS_INTERRUPTED
        run = Run(
            run_id=str(meta.get("run_id", "")),
            session_id=str(meta.get("session_id", "")),
            status=status,
            title=str(meta.get("title", "")),
            preview=str(meta.get("preview", "")),
            reply_text=str(meta.get("reply_text", "")),
            events=events,
            created_at=float(meta.get("created_at", 0.0)),
        )
        run.task_id = str(meta.get("task_id", ""))
        run._seq = events[-1].seq if events else 0
        return run

    def active_run(self, session_id: str | None = None) -> Run | None:
        """当前 in-flight 的 Run（供诊断/测试）；给 session_id 则限该会话。

        S8a 后「当前唯一」只在会话内成立（create_if_idle 原子保证），
        全局可同时有 max_in_flight 个。
        """
        with self._lock:
            for run in self._runs.values():
                if run.status not in _IN_FLIGHT:
                    continue
                if session_id is None or run.session_id == session_id:
                    return run
            return None

    def in_flight_count(self) -> int:
        """当前 in-flight 数（ADR 115 §4）：per-Run 账单归因的并发标注判据
        ——快照窗口内 >1 就标 usage_approx（不假装精确、不禁并发）。"""
        with self._lock:
            return sum(1 for r in self._runs.values() if r.status in _IN_FLIGHT)


class TaskBus:
    """任务级事件总线（M5 承诺件 b）：全局 seq + 环形缓冲的推送/续传面。

    与 Run 的事件流同款广播模型（每订阅者独立队列、unsubscribe 防泄漏），
    多两件：全局 seq + 环形缓冲供 Last-Event-ID 断线续传（since()）；不设
    终态（任务面是常驻流，心跳保活、客户端自行断开）。载荷复用 RunEvent
    ——「同一个事件对象可被多个协议消费」，SSE 编码层零改动。

    ponytail: 缓冲定长 500——断层（缓冲挤掉缺件）就发全量快照自愈（快照
    载荷本身是全量状态，幂等重放可容忍重复）；升级路径＝断层也要增量续传
    时再上落盘事件日志，触发信号＝实测出现「快照自愈抖动」投诉。
    """

    def __init__(self, maxlen: int = 500) -> None:
        self._buf: deque[RunEvent] = deque(maxlen=maxlen)
        self._subs: list[queue.Queue] = []
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def seq(self) -> int:
        """当前全局 seq（SSE 端点的续传哨兵）。"""
        with self._lock:
            return self._seq

    def emit(self, type: str, data: dict | None = None) -> None:
        """广播一条任务面事件（append-only；载荷=全量状态，幂等可重放）。"""
        event = RunEvent(seq=0, type=type, data=data or {})
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            self._buf.append(event)
            for q in self._subs:
                q.put(event)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def since(self, last_seq: int) -> list[RunEvent] | None:
        """续传段：缓冲完整覆盖 last_seq 之后 → 只补缺的事件；覆盖不了 → None。

        None = 断层（或 last_seq 落在缓冲外）——调用方发全量快照自愈，
        这比「假装续传了」诚实（宁可重放全量，不可漏件）。
        """
        with self._lock:
            if not self._buf:
                return [] if last_seq >= self._seq else None
            if last_seq + 1 < self._buf[0].seq:
                return None   # 缺件已被环形缓冲挤掉
            return [ev for ev in self._buf if ev.seq > last_seq]
