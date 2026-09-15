"""S2b Run Store 验收：状态机单一终态 + seq 自增 + 取消门 + 单锁。"""

from agent.server.run_store import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    Run,
    RunStore,
)


def test_emit_increments_seq_and_pushes_to_subscriber():
    run = Run(run_id="r1")
    e1 = run.emit("run.started")
    e2 = run.emit("text.delta", {"delta": "你好"})

    assert [e1.seq, e2.seq] == [1, 2]
    assert run.events == [e1, e2]
    # 订阅者可依序读回同一条事件（对象身份一致）
    assert run.subscribe().get() is e1
    assert run.subscribe().get() is e2


def test_finish_emits_terminal_event_then_sentinel():
    run = Run(run_id="r1")
    run.finish(STATUS_COMPLETED)

    assert run.status == STATUS_COMPLETED
    assert run.subscribe().get().type == "run.completed"   # 终态事件先进流
    assert run.subscribe().get() is None                   # 再哨兵：流结束

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


def test_create_if_idle_rejects_while_inflight():
    store = RunStore()
    first = store.create_if_idle()
    assert first is not None

    # 第一个还 pending（未终态）→ 第二个被拒（单锁）
    assert store.create_if_idle() is None

    first.finish(STATUS_COMPLETED)
    assert store.create_if_idle() is not None   # 终态后可再创建
