"""ADR 117 §1/§2 后台队列 + 任务级并发上限：调度器语义钉子。

不变量：
- 单一起口：worker 只由 _maybe_start_queued 起（入队/终态/启动首拍三触发
  点 + 60s 巡检兜底汇一处），FIFO = (created_at, task_id) 稳定排序
- 上限：running 达 TASK_MAX_CONCURRENT_TASKS 即止；终态释放后下一拍拉起
- 重启回落（requeue_on_boot）：running 干净→queued（next_text 清空代打）；
  熔断→blocked、取消→cancelled 不因重启洗白；人工态不动；幂等
- resume 入队走调度（不直起线程），next_text 承载注入消息
"""

import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import facta.server.app as app_mod
from facta.core.llm import ScriptedLLM
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext, ensure_persona
from facta.orchestrator.task import (
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_WAITING_STEER,
    TaskStore,
    requeue_on_boot,
)
from facta.server.app import create_app
from facta.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    """与 test_app 同款：TaskStore 默认根是真 data/tasks，测试必须隔离。"""
    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))


class _SyncThread:
    """start() 即同步执行：把调度起的 worker 线程变确定性（FIFO 断言不赌
    线程调度顺序）。只用于不碰 TestClient 的单测——TestClient 内部也用线程。"""

    def __init__(self, target=None, args=(), daemon=None, **kw):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _mk_tasks(n: int, tmp_path) -> TaskStore:
    """n 个 queued 任务，created_at 递增（FIFO 序稳定可断言）。"""
    store = TaskStore(root=tmp_path / "tasks")
    out = []
    for i in range(n):
        t = store.create(f"目标{i}", session_id=f"s{i}")
        t.created_at = float(i + 1)
        store.save(t)
        out.append(t)
    return store


# ---------- 调度器：FIFO + 上限 + 终态释放 ----------


def test_scheduler_starts_fifo_up_to_cap(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(app_mod, "_task_worker", lambda ctx, store, tasks, task: started.append(task.task_id))
    monkeypatch.setattr(app_mod, "TASK_MAX_CONCURRENT_TASKS", 2)
    store = _mk_tasks(4, tmp_path)

    app_mod._maybe_start_queued(None, None, store)

    # FIFO 选取 = 最早两个（集合断言即序证据——同一次调用内按序起）
    assert started == [t.task_id for t in sorted(store.list(), key=lambda t: t.created_at)[:2]]
    statuses = {t.task_id: t.status for t in store.list()}
    assert sum(1 for s in statuses.values() if s == TASK_RUNNING) == 2
    assert sum(1 for s in statuses.values() if s == TASK_QUEUED) == 2


def test_terminal_frees_slot_starts_next(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(app_mod, "_task_worker", lambda ctx, store, tasks, task: started.append(task.task_id))
    monkeypatch.setattr(app_mod, "TASK_MAX_CONCURRENT_TASKS", 1)
    store = _mk_tasks(2, tmp_path)

    app_mod._maybe_start_queued(None, None, store)
    assert len(started) == 1   # 第二个在排队

    # 第一个落终态（_task_worker 的 finally 会再触发调度——这里直接验证触发点语义）
    first = store.get(started[0])
    first.status = TASK_DONE
    store.save(first)
    app_mod._maybe_start_queued(None, None, store)
    assert len(started) == 2
    assert store.list()[0].status == TASK_RUNNING or store.get(started[1]).status == TASK_RUNNING


def test_scheduler_respects_running_from_previous_call(tmp_path, monkeypatch):
    """已 running 的占槽（计数口径=worker 起线程起）：只剩 1 个空位。"""
    started = []
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(app_mod, "_task_worker", lambda ctx, store, tasks, task: started.append(task.task_id))
    monkeypatch.setattr(app_mod, "TASK_MAX_CONCURRENT_TASKS", 2)
    store = _mk_tasks(3, tmp_path)
    legacy = store.create("老任务", session_id="sx")   # 手工置 running（模拟在飞）
    legacy.status = TASK_RUNNING
    store.save(legacy)

    app_mod._maybe_start_queued(None, None, store)
    assert len(started) == 1


# ---------- 重启回落（requeue_on_boot） ----------


def test_requeue_on_boot_semantics(tmp_path):
    store = TaskStore(root=tmp_path / "tasks")
    clean = store.create("干净在跑", session_id="s1", next_text="旧注入")
    clean.status = TASK_RUNNING
    fused = store.create("熔断在跑", session_id="s2")
    fused.status, fused.fuse_fired = TASK_RUNNING, True
    cancelled = store.create("取消在跑", session_id="s3")
    cancelled.status, cancelled.cancel_requested = TASK_RUNNING, True
    steer = store.create("等人改向", session_id="s4")
    steer.status = TASK_WAITING_STEER
    blocked = store.create("被挡", session_id="s5")
    blocked.status = TASK_BLOCKED
    for t in (clean, fused, cancelled, steer, blocked):
        store.save(t)

    moved = requeue_on_boot(store)
    assert moved == 3   # 只动 running 三个

    assert store.get(clean.task_id).status == TASK_QUEUED
    assert store.get(clean.task_id).next_text == ""   # 清空 → 起跑续跑代打，不重发 goal
    assert store.get(fused.task_id).status == TASK_BLOCKED      # 熔断不洗白
    assert "熔断" in store.get(fused.task_id).blocked_reason
    assert store.get(cancelled.task_id).status == TASK_CANCELLED  # 取消不留悬案
    assert store.get(steer.task_id).status == TASK_WAITING_STEER  # 人工态不动
    assert store.get(blocked.task_id).status == TASK_BLOCKED

    assert requeue_on_boot(store) == 0   # 幂等：重跑不产生新状态


def test_requeue_keeps_existing_queued_next_text(tmp_path):
    """已在队的任务回落不动——resume 时排好的注入消息不丢。"""
    store = TaskStore(root=tmp_path / "tasks")
    queued = store.create("排队中", session_id="s1", next_text="用户指令")
    queued.status = TASK_QUEUED
    store.save(queued)

    requeue_on_boot(store)
    assert store.get(queued.task_id).status == TASK_QUEUED
    assert store.get(queued.task_id).next_text == "用户指令"


# ---------- 巡检兜底（_queue_tick） ----------


def test_queue_tick_survives_exception_and_keeps_ticking(monkeypatch):
    """单次异常不卡死队列：tick 续跳（117 §1 自我挑刺 1）。"""
    calls = []

    def fake_sched(ctx, store, tasks):
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("第一跳炸了")
        if len(calls) >= 3:
            # 巡检语义已验足：让 tick 长睡退场（daemon 线程，进程退出即亡）
            monkeypatch.setattr(app_mod, "_QUEUE_TICK_S", 3600.0)

    monkeypatch.setattr(app_mod, "_maybe_start_queued", fake_sched)
    monkeypatch.setattr(app_mod, "_QUEUE_TICK_S", 0.01)
    threading.Thread(target=app_mod._queue_tick, args=(None, None, None), daemon=True).start()

    deadline = time.time() + 3
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert len(calls) >= 3   # 第一跳异常后仍续跳


# ---------- HTTP：入队触发 + resume 入队 ----------


def _make_client() -> TestClient:
    tools = ToolRegistry()

    def build_agent(session: Session, repo_root=None) -> Agent:
        agent = Agent(name="test", system_prompt="测试人设", registry=tools)
        ensure_persona(session, agent)
        return agent

    ctx = AppContext(
        provider="mock",
        ledger=UsageLedger(),
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="好")]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=build_agent,
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
    )
    return TestClient(create_app(ctx))


def _wait_status(client: TestClient, task_id: str, status: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/tasks/{task_id}").json()
        if d["status"] == status:
            return d
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内落 {status}：{d['status']}")


def test_create_enqueues_and_scheduler_starts(monkeypatch):
    """触发点①：POST /api/tasks 入队即调度（202 即返，worker 由调度器起）。"""
    started = []
    monkeypatch.setattr(
        app_mod, "_task_worker",
        lambda ctx, store, tasks, task: started.append((task.task_id, task.next_text)),
    )
    client = _make_client()
    resp = client.post("/api/tasks", json={"goal": "写个小工具"})
    assert resp.status_code == 202
    tid = resp.json()["task_id"]

    deadline = time.time() + 3
    while not started and time.time() < deadline:
        time.sleep(0.01)
    assert started and started[0][0] == tid
    assert "写个小工具" in started[0][1] and "make_plan" in started[0][1]   # goal + kickoff


def test_resume_enqueues_with_next_text(monkeypatch):
    """resume 走入队（不直起线程）：next_text = 用户指令；空文本 = 续跑代打。"""
    calls = []

    def fake_worker(ctx, store, tasks, task):
        calls.append((task.task_id, task.next_text))
        task.status = TASK_BLOCKED   # 模拟一轮后又落人工态，供再次 resume
        tasks.save(task)

    monkeypatch.setattr(app_mod, "_task_worker", fake_worker)
    client = _make_client()
    tid = client.post("/api/tasks", json={"goal": "长任务"}).json()["task_id"]
    _wait_status(client, tid, "blocked")

    resp = client.post(f"/api/tasks/{tid}/resume", json={"text": "按新方向继续"})
    assert resp.status_code == 200
    _wait_status(client, tid, "blocked")
    assert calls[-1] == (tid, "按新方向继续")

    # 空 text → 续跑注入消息代打（显式标注，不复用 038 崩溃续跑）
    client.post(f"/api/tasks/{tid}/resume", json={})
    _wait_status(client, tid, "blocked")
    assert calls[-1][0] == tid
    assert calls[-1][1].startswith("[自动续跑]")
