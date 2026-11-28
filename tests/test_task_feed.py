"""M5 承诺件 a+b：Run 明细持久化（重启后下钻可用）+ 任务级 SSE（推送/续传）。

不变量：
- a：Run 元快照 + 事件流落盘 data/tasks/runs/（挂账口径）；服务重启（新 app
  同根）后 /api/runs 摘要与 /api/runs/{id}/events 回放照旧；孤儿轮（未收尾）
  重建为 interrupted（终态，不占准入槽）；非法 run_id 越不过界碑。
- b：任务面推送的帧语义（快照/续传二选一、seq 去重、断层自愈）由
  task_event_stream 离线单测；接线（确认挂起即时推 task.updated(waiting-
  approval)、run.updated 跟随状态）经 app.state.task_bus 全程序列断言。
  不测 HTTP 级无限流：Starlette TestClient 的传输层缓冲整个响应体（无限流
  必挂），HTTP 面留给真机/浏览器验收。
"""

import json
import tempfile
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from facta.core.llm import ScriptedLLM
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext, ensure_persona
from facta.orchestrator.task import TASK_DONE
from facta.server.app import create_app, task_event_stream
from facta.server.run_log import RunLog
from facta.server.run_store import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    Run,
    RunEvent,
    RunStore,
    TaskBus,
)
from facta.tools.registry import ToolRegistry


def _make_ctx(reply: str = "你好！", llm=None, registry=None) -> AppContext:
    """最小 AppContext（test_app._make_ctx 同款形状，本文件自带一份）。
    conftest 的 autouse 隔离闸已把 TASKS_DIR 推进 tmp_path。"""
    tools = registry or ToolRegistry()

    def build_agent(session: Session, repo_root=None) -> Agent:
        agent = Agent(name="test", system_prompt="测试人设", registry=tools)
        ensure_persona(session, agent)
        return agent

    return AppContext(
        provider="mock",
        ledger=UsageLedger(),
        embedder=None,
        llm=llm or ScriptedLLM([Message(role="assistant", content=reply)]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=build_agent,
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
    )


def _next_frame(gen) -> dict:
    """从 SSE 帧生成器取下一帧并解出 data 载荷（帧 = id/event/data 三行）。"""
    raw = next(gen)
    data_line = next(ln for ln in raw.splitlines() if ln.startswith("data: "))
    return json.loads(data_line[len("data: "):])


def _wait_task_status(client, task_id: str, statuses, timeout: float = 5.0) -> str:
    """轮询任务状态直到命中（worker 在后台线程推进，测试线程顺序请求）。"""
    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        status = client.get(f"/api/tasks/{task_id}").json()["status"]
        if status in statuses:
            return status
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内到达 {statuses}（当前 {status}）")


def _resolve_confirm_when_pending(client, run_id: str, approve: bool):
    # worker 到确认挂起点有毫秒级竞态：轮询到端点不再是 409（test_app 同款）
    resp = None
    for _ in range(100):   # 最多 5s
        resp = client.post(f"/api/runs/{run_id}/confirm", json={"approve": approve})
        if resp.status_code == 200:
            return resp
        time.sleep(0.05)
    return resp


# ---------- 承诺件 a：Run 明细持久化 ----------


def test_run_events_persisted_and_replayable(tmp_path):
    # 单测面：emit 即落盘、finish 元快照终版、重建 Run 事件/状态/哨兵全对
    log = RunLog(tmp_path / "runs")
    store = RunStore(log=log)
    run = store.create(title="hello", session_id="s1")
    run.emit("run.started", {})
    run.emit("text.delta", {"delta": "你好"})
    run.finish(STATUS_COMPLETED)

    meta, events = log.load(run.run_id)
    assert meta["status"] == STATUS_COMPLETED
    assert meta["title"] == "hello"
    assert [e.seq for e in events] == [1, 2, 3]

    fresh = RunStore(log=log)   # 重启：新 store 同落盘根
    got = fresh.get(run.run_id)
    assert got is not None
    assert got.status == STATUS_COMPLETED and got.title == "hello"
    assert [e.type for e in got.events] == ["run.started", "text.delta", "run.completed"]
    assert got.subscribe().get(timeout=1) is None   # 终态补发哨兵，流正常收口


def test_run_detail_survives_restart():
    # 全链路（关闭 README「已知边界」第一行）：任务跑完 → 新 app（重启）→
    # 任务卡 run_ids 仍可下钻：摘要在 /api/runs，事件流照旧回放
    client = TestClient(create_app(_make_ctx()))
    task_id = client.post("/api/tasks", json={"goal": "打个招呼"}).json()["task_id"]
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE
    run_id = client.get(f"/api/tasks/{task_id}").json()["run_ids"][0]

    restarted = TestClient(create_app(_make_ctx()))   # 同一 data/tasks/ 根 = 重启

    runs = restarted.get("/api/runs").json()
    mine = [r for r in runs if r["run_id"] == run_id]
    # title = 首条消息截断（goal + kickoff 提示），这里只钉归属与状态
    assert mine and mine[0]["title"].startswith("打个招呼") and mine[0]["status"] == "completed"
    events = []
    with restarted.stream("GET", f"/api/runs/{run_id}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    types = [e["type"] for e in events]
    assert types[0] == "run.started" and types[-1] == "run.completed"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


def test_orphan_run_reconstructed_as_interrupted(tmp_path):
    # 孤儿轮（服务被杀留下的未收尾轮次）：重建为 interrupted（终态之一）
    # ——「还在跑」是假话（会占准入槽、SSE 不收口），「失败」也不对
    log = RunLog(tmp_path / "runs")
    run = Run(run_id=uuid.uuid4().hex, session_id="s1", status=STATUS_RUNNING, title="半截")
    log.write_meta(run)
    log.append_event(run.run_id, RunEvent(seq=1, type="run.started", data={}))

    store = RunStore(log=log)   # 重启
    got = store.get(run.run_id)
    assert got.status == "interrupted"
    assert [e.type for e in got.events] == ["run.started"]
    assert store.active_run() is None   # 孤儿轮不占准入槽
    assert got.subscribe().get(timeout=1) is None   # 按终态收口，SSE 不悬挂


def test_run_log_rejects_illegal_run_id(tmp_path):
    # 信任边界：run_id 来自 HTTP 路径，越不过界碑（防路径逃逸）
    log = RunLog(tmp_path / "runs")
    assert log.load("../../etc/passwd") is None
    assert log.load("") is None
    assert log.load("NOThex") is None
    assert list(tmp_path.iterdir()) == []   # 非法 id 不落任何文件


# ---------- 承诺件 b：任务级 SSE（帧语义离线测 + 接线经总线断言）----------


def test_task_stream_snapshot_then_live():
    # 新鲜连接（last_seq<0）第一帧 = 全量快照，之后实时增量按序出
    bus = TaskBus()
    gen = task_event_stream(bus, -1, lambda: {"tasks": ["t"], "runs": []})
    first = _next_frame(gen)
    assert first["type"] == "snapshot" and first["data"]["tasks"] == ["t"]

    bus.emit("task.updated", {"task_id": "x"})
    second = _next_frame(gen)
    assert second["type"] == "task.updated" and second["seq"] == 1
    gen.close()   # 常驻流由客户端关（GeneratorExit → finally 退订）


def test_task_stream_replay_and_resume_no_snapshot():
    # 续传（last_seq≥0）：只补缺的增量，无快照；哨兵之后的 seq 严格递增
    bus = TaskBus()
    for i in range(3):
        bus.emit("task.updated", {"i": i})

    gen = task_event_stream(bus, 0, lambda: {})
    frames = [_next_frame(gen) for _ in range(3)]
    gen.close()
    assert all(f["type"] != "snapshot" for f in frames)
    assert [f["seq"] for f in frames] == [1, 2, 3]

    gen2 = task_event_stream(bus, 2, lambda: {})   # 断线在 seq=2 之后重连
    again = _next_frame(gen2)
    gen2.close()
    assert again["seq"] == 3 and again["data"] == {"i": 2}   # 旧件不重复


def test_confirm_push_instant_via_bus(tmp_path):
    # 接线（关闭 20s 轮询毛刺的核心）：确认挂起即时推 task.updated(
    # waiting-approval)；run.updated 跟随 Run 状态。经 app.state.task_bus 的
    # 全程序列断言（HTTP 流面的帧语义已由上面两条离线覆盖）
    from facta.tools.context import ToolContext
    from facta.tools.terminal import register_terminal_tools

    registry = ToolRegistry()
    register_terminal_tools(registry, ToolContext(notes_dir=tmp_path, workspace_root=tmp_path))
    # 剧本：先点菜 run_command（非白名单 → 挂起确认），批准后收尾
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "run_command", "arguments": json.dumps({"command": "touch ok.txt"})},
        ]),
        Message(role="assistant", content="已执行"),
    ])
    app = create_app(_make_ctx(llm=llm, registry=registry))
    client = TestClient(app)

    task_id = client.post("/api/tasks", json={"goal": "建个文件"}).json()["task_id"]
    run_id = ""
    for _ in range(100):   # 等 run_ids 就位（worker 起轮）
        run_ids = client.get(f"/api/tasks/{task_id}").json()["run_ids"]
        if run_ids:
            run_id = run_ids[0]
            break
        time.sleep(0.05)
    assert run_id
    assert _resolve_confirm_when_pending(client, run_id, approve=True).status_code == 200
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE

    events = app.state.task_bus.since(0)   # 总线环形缓冲 = 全程序列（小样本覆盖）
    assert events is not None
    task_statuses = [e.data["status"] for e in events if e.type == "task.updated"]
    assert "waiting-approval" in task_statuses   # 确认挂起即时进推送面
    assert task_statuses[-1] == TASK_DONE
    run_statuses = [e.data["status"] for e in events if e.type == "run.updated"]
    assert "waiting_approval" not in run_statuses   # Run 状态词汇不与任务词汇混
    assert "completed" in run_statuses


# ---------- TaskBus 单测：续传/断层 ----------


def test_taskbus_since_resume_and_gap():
    bus = TaskBus(maxlen=3)
    assert bus.since(0) == []          # 空总线且无缺口 → 无缺件
    assert bus.seq == 0

    for i in range(5):
        bus.emit("task.updated", {"i": i})
    assert bus.seq == 5
    assert [e.seq for e in bus.since(2)] == [3, 4, 5]   # 缓冲完整覆盖 → 补缺
    assert bus.since(5) == []                            # 已追平
    assert bus.since(1) is None                          # 缺件被挤出缓冲 → 断层（快照自愈）
    assert bus.since(0) is None                          # 同上


def test_taskbus_subscribe_unsubscribe_broadcast():
    bus = TaskBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.emit("task.updated", {"a": 1})
    assert q1.get(timeout=1) is q2.get(timeout=1)   # 广播给每个订阅者各一份（同一事件对象）
    bus.unsubscribe(q1)
    bus.emit("task.updated", {"a": 2})
    assert q2.get(timeout=1).data == {"a": 2}
    assert q1.empty()   # 退订后不再收件
