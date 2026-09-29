"""S2b Run Store 验收：状态机单一终态 + seq 自增 + 取消门 + 会话内单锁。

S4b 增补：waiting_approval 挂起——阻塞裁决 / 取消视为拒绝 / 准入口径。
S8a 增补：单锁口径收窄到「一段对话一把」+ 全局并发上限。
"""

import threading
import time

from facta.server.run_store import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_WAITING,
    Run,
    RunStore,
)


def test_emit_increments_seq_and_pushes_to_subscriber():
    # 广播模型（评审修复轮）：subscribe 只收订阅后的事件——历史事件在
    # run.events 列表里，由 SSE 端点单独重放。单队列时代的旧测试在 emit
    # 后才 subscribe，队列空 → get() 永久阻塞（全套 pytest 挂起的根因）。
    run = Run(run_id="r1")
    q = run.subscribe()          # 先订阅
    run.emit("run.started")      # 再 emit → 广播进队列
    run.emit("text.delta", {"delta": "你好"})
    e1, e2 = run.events          # emit 不回传事件（要当回调直接用），从 events 取

    assert [e1.seq, e2.seq] == [1, 2]
    # 订阅者可依序读回同一条事件（对象身份一致）
    assert q.get(timeout=1) is e1
    assert q.get(timeout=1) is e2


def test_finish_emits_terminal_event_then_sentinel():
    run = Run(run_id="r1")
    q = run.subscribe()              # 先订阅
    run.finish(STATUS_COMPLETED)    # finish 广播 run.completed + None 哨兵

    assert run.status == STATUS_COMPLETED
    assert q.get(timeout=1).type == "run.completed"   # 终态事件先进流
    assert q.get(timeout=1) is None                   # 再哨兵：流结束

    # 晚到的订阅者（终态后才连）：subscribe 补发哨兵（评审修复轮修复）
    late = run.subscribe()
    assert late.get(timeout=1) is None

    # 单一终态不变量：终态后再 finish 不覆盖、不再发事件
    run.finish(STATUS_CANCELLED)
    assert run.status == STATUS_COMPLETED


def test_request_cancel_only_effective_before_terminal():
    run = Run(run_id="r1")
    assert run.request_cancel() is True
    assert run.cancel_requested is True

    run.finish(STATUS_COMPLETED)
    assert run.request_cancel() is False   # 终态后取消是无效操作


def test_store_active_run_is_single_inflight():
    store = RunStore()
    run = store.create()
    run.status = STATUS_RUNNING

    assert store.active_run() is run

    run.finish(STATUS_COMPLETED)
    assert store.active_run() is None      # 终态后不再是 in-flight


def test_create_if_idle_rejects_same_session_while_inflight():
    store = RunStore()
    first = store.create_if_idle("s1")
    assert isinstance(first, Run)

    # 同会话第一个还 pending（未终态）→ 第二个被拒（会话内单锁）
    assert isinstance(store.create_if_idle("s1"), str)

    first.finish(STATUS_COMPLETED)
    assert isinstance(store.create_if_idle("s1"), Run)   # 终态后可再创建


def test_create_if_idle_allows_other_sessions_up_to_cap():
    # S8a：单锁口径从「全进程一把」收窄到「一段对话一把」——
    # 「长任务期间另开会话聊」的正确性就靠这条。
    store = RunStore(max_in_flight=2)
    assert isinstance(store.create_if_idle("s1"), Run)
    assert isinstance(store.create_if_idle("s2"), Run)
    # 并发上限（资源约束）：第三个会话也被拒，但原因不同于「该会话在忙」
    rejected = store.create_if_idle("s3")
    assert isinstance(rejected, str) and "上限" in rejected
    busy = store.create_if_idle("s1")                   # 会话忙是另一条文案
    assert isinstance(busy, str) and "在运行" in busy
    assert store.list_runs("s1") and store.list_runs("s3") == []
    assert store.active_run("s2") is not None


# ---------- S4b：waiting_approval 确认挂起 ----------

def _start_confirm(run: Run) -> tuple[threading.Thread, dict]:
    """后台线程发起确认挂起，等到 confirm_pending 生效（模拟 worker 阻塞）。"""
    result: dict = {}

    def worker():
        result["approved"] = run.request_confirm("run_command", {"command": "rm x"})

    t = threading.Thread(target=worker)
    t.start()
    deadline = time.time() + 2
    while not run.confirm_pending and time.time() < deadline:
        time.sleep(0.01)
    return t, result


def test_request_confirm_blocks_until_user_resolves():
    run = Run(run_id="r1", status=STATUS_RUNNING)
    t, result = _start_confirm(run)

    assert run.confirm_pending is True
    assert run.status == STATUS_WAITING                 # running → waiting
    assert run.resolve_confirm(True) is True
    t.join(timeout=2)

    assert result["approved"] is True
    assert run.status == STATUS_RUNNING                 # 裁决后回到原状态
    # 两条事件进 append-only 流（断线重放的原料）
    assert [e.type for e in run.events] == ["confirm.request", "confirm.resolved"]
    assert run.events[0].data == {"tool": "run_command", "arguments": {"command": "rm x"}}
    assert run.events[1].data == {"tool": "run_command", "approved": True}


def test_request_confirm_reject_path():
    run = Run(run_id="r1", status=STATUS_RUNNING)
    t, result = _start_confirm(run)

    assert run.resolve_confirm(False) is True
    t.join(timeout=2)

    assert result["approved"] is False
    assert run.events[-1].data["approved"] is False


def test_request_confirm_cancel_counts_as_reject():
    run = Run(run_id="r1", status=STATUS_RUNNING)
    t, result = _start_confirm(run)

    assert run.request_cancel() is True                 # 等待中仍可取消
    t.join(timeout=2)

    assert result["approved"] is False                  # 取消视为拒绝
    assert run.events[-1].data["approved"] is False


def test_resolve_confirm_without_pending_is_noop():
    run = Run(run_id="r1")
    assert run.resolve_confirm(True) is False           # 没挂起 → confirm 端点 409 的判据


def test_waiting_counts_as_inflight_for_session_lock():
    store = RunStore()
    first = store.create_if_idle("s1")
    assert isinstance(first, Run)
    first.status = STATUS_WAITING                       # 挂起等确认也算在跑

    assert isinstance(store.create_if_idle("s1"), str)  # 会话锁不放行
    assert store.active_run() is first
