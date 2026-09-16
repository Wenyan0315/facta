"""S2b FastAPI 壳验收：Run 生命周期三接口 + 事件流。"""

import json

import pytest
from fastapi.testclient import TestClient

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.store import Session
from agent.orchestrator.assemble import AppContext
from agent.server.app import create_app


@pytest.fixture(autouse=True)
def _isolate_session_file(tmp_path, monkeypatch):
    """隔离真实会话文件：每轮落盘上线后，跑 Run 的测试会写真 session.json
    ——持久状态的系统必须 fixture 隔离（M6.3「测试污染」血案的同款复发，
    本次是它第一次真烧掉用户数据）。"""
    import agent.server.app as app_module
    monkeypatch.setattr(app_module, "MEMORY_PATH", tmp_path / "session.json")
    monkeypatch.setattr(app_module, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(app_module, "LEARNED_DIR", tmp_path / "learned")


def _make_ctx(reply: str = "你好！") -> AppContext:
    # 最小 AppContext：ScriptedLLM 回纯文本（不点菜），registry 传 None 也可。
    # todos 指到临时目录（mkdtemp 每次唯一，测试间不串）——避免调用 todos
    # 端点的测试踩到 None 路径
    import tempfile
    from pathlib import Path

    from agent.memory.todos import TodoStore

    return AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content=reply)]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        session=Session(),
        registry=None,
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
    )


def _make_client(reply: str = "你好！") -> TestClient:
    return TestClient(create_app(_make_ctx(reply)))


def _read_events(client, run_id: str) -> list[dict]:
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def test_run_lifecycle_create_stream_complete_cancel():
    client = _make_client()

    # 1) 创建 Run → 202 + run_id
    resp = client.post("/api/runs", json={"text": "你好"})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    # 2) 订阅事件，读到完整生命周期
    events = _read_events(client, run_id)
    types = [e["type"] for e in events]
    assert types[0] == "run.started"
    assert "text.delta" in types
    assert types[-1] == "run.completed"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)   # seq 单调递增

    # 3) 已完成 → cancel 409
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 409

    # 4) 任务列表：title=用户消息截断、preview=最终回复（任务视图原料）
    runs = client.get("/api/runs").json()
    assert runs[0]["title"] == "你好"
    assert runs[0]["preview"] == "你好！"
    assert runs[0]["status"] == "completed"


def test_messages_endpoint_filters_to_storyline():
    # 历史回放只讲故事线：system=人设、tool=中间产物、空 content 纯点菜轮都滤掉
    ctx = _make_ctx()
    ctx.session.messages.append(Message(role="system", content="人设"))
    ctx.session.messages.append(Message(role="user", content="你好"))
    ctx.session.messages.append(Message(role="assistant", content="", tool_calls=[
        {"id": "c1", "name": "get_current_time", "arguments": "{}"}
    ]))
    ctx.session.messages.append(Message(role="tool", tool_call_id="c1", content="12:00"))
    ctx.session.messages.append(Message(role="assistant", content="现在 12 点"))
    client = TestClient(create_app(ctx))

    assert client.get("/api/messages").json() == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "现在 12 点"},
    ]


def test_runs_list_newest_first():
    from agent.server.run_store import STATUS_COMPLETED, RunStore

    store = RunStore()
    old = store.create(title="旧任务")
    old.finish(STATUS_COMPLETED)
    store.create(title="新任务")   # pending

    client = TestClient(create_app(_make_ctx(), store=store))
    runs = client.get("/api/runs").json()
    assert [r["title"] for r in runs] == ["新任务", "旧任务"]   # 新的在前
    assert runs[1]["status"] == "completed"


def test_tasks_page_serves_html():
    # 任务视图是真页面（曾是占位 JSON——真实使用反馈：点进去一片裸 JSON）
    resp = _make_client().get("/tasks")
    assert resp.status_code == 200
    assert "任务" in resp.text


def test_ensure_persona_three_branches():
    # 装配不变量：会话必须带 SYSTEM_PROMPT 开工——Web 入口曾跑过无人设会话
    from agent.core.types import Message
    from agent.memory.store import Session
    from agent.orchestrator.assemble import ensure_persona
    from agent.orchestrator.loop import SYSTEM_PROMPT

    # 空会话：种人设
    fresh = Session()
    ensure_persona(fresh)
    assert fresh.messages[0].role == "system"
    assert fresh.messages[0].content == SYSTEM_PROMPT

    # 历史遗留的无 system 会话：头部补插 + 摘要游标随位移 +1
    legacy = Session()
    legacy.messages.append(Message(role="user", content="旧消息"))
    legacy.summarized_upto = 3
    ensure_persona(legacy)
    assert [m.role for m in legacy.messages] == ["system", "user"]
    assert legacy.summarized_upto == 4

    # 正常会话（已有 system）：不动
    normal = Session()
    normal.messages.append(Message(role="system", content="人设"))
    normal.messages.append(Message(role="user", content="你好"))
    ensure_persona(normal)
    assert len(normal.messages) == 2
    assert normal.messages[0].content == "人设"


def test_run_persists_session_each_turn(tmp_path, monkeypatch):
    # 每轮落盘：Web 壳常驻无退出钩子——finally 落盘且先于终态哨兵
    # （读到 run.completed 时盘上必须有这轮的 user+assistant）
    import agent.server.app as app_module

    mem = tmp_path / "session.json"
    monkeypatch.setattr(app_module, "MEMORY_PATH", mem)
    client = TestClient(create_app(_make_ctx()))

    resp = client.post("/api/runs", json={"text": "记住这句"})
    _read_events(client, resp.json()["run_id"])   # 读完事件流 = worker 已收尾

    data = json.loads(mem.read_text(encoding="utf-8"))
    contents = [m.get("content") for m in data["messages"]]
    assert "记住这句" in contents and "你好！" in contents


def test_archive_new_session_sets_llm_title(tmp_path):
    # S2 验收修复轮：归档时用 LLM 提炼标题写进归档文件（列表读取零 LLM 调用）
    from agent.memory.store import load_session

    ctx = _make_ctx()
    # internal_llm 第 1 次调用 = 提炼标题；后续 consolidate 调用吃兜底（parse 失败不写）
    ctx.internal_llm = ScriptedLLM([Message(role="assistant", content="PHP 工具封装")])
    ctx.session.messages.append(Message(role="user", content="PHP 结合 AI Agent 可以做什么"))
    ctx.session.messages.append(Message(role="assistant", content="PHP 当工具层，决策交给大模型"))

    client = TestClient(create_app(ctx))
    assert client.post("/api/sessions/new").status_code == 200

    # fixture 已把 SESSIONS_DIR monkeypatch 到 tmp_path/sessions
    archived = list((tmp_path / "sessions").glob("*.json"))
    assert len(archived) == 1
    assert load_session(archived[0]).title == "PHP 工具封装"   # 不是首句截断


def test_empty_session_new_archives_nothing(tmp_path):
    # S2 验收修复轮#3：空会话不进仓库——此前「点新会话」把空会话无条件归档，
    # 列表长出「（空会话）」垃圾记录（会话数量感「变多」的一半根因）
    client = TestClient(create_app(_make_ctx()))   # ctx.session 无任何消息
    assert client.post("/api/sessions/new").status_code == 200
    assert list((tmp_path / "sessions").glob("*.json")) == []


def test_new_session_reseeds_persona_after_clear(tmp_path):
    # S2 验收修复轮#4：归档 clear 连 system 一起清——第二场会话曾变裸会话
    # （真实复踩：新会话里中文提问收到英文回复）。修复后归档即补种人设，
    # 新 active 落盘/内存都带 system。
    from agent.orchestrator.loop import SYSTEM_PROMPT

    ctx = _make_ctx()
    ctx.session.messages.append(Message(role="system", content=SYSTEM_PROMPT))
    ctx.session.messages.append(Message(role="user", content="第一场对话"))

    client = TestClient(create_app(ctx))
    assert client.post("/api/sessions/new").status_code == 200

    assert [m.role for m in ctx.session.messages] == ["system"]   # 内存：新会话带人设
    assert ctx.session.messages[0].content == SYSTEM_PROMPT
    # 落盘：新 active 文件同样带人设（读回验证）
    from agent.memory.store import load_session
    assert load_session(tmp_path / "session.json").messages[0].role == "system"


def test_switch_with_empty_current_does_not_archive_empty(tmp_path):
    # 切换时若当前会话为空：不归档空会话、只消耗目标（move 语义），归档数只减不增
    from agent.memory.store import save_session

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    target = sessions_dir / "20260913-101956.json"
    save_session(
        Session(messages=[
            Message(role="user", content="目标会话内容"),
            Message(role="assistant", content="回复"),
        ]),
        target,
    )

    client = TestClient(create_app(_make_ctx()))   # 当前会话为空
    assert client.post("/api/sessions/20260913-101956.json/switch").status_code == 200

    assert list(sessions_dir.glob("*.json")) == []   # 目标已切回、空会话未被归档


def test_sessions_list_includes_current_on_top(tmp_path):
    # 「时隐时现」修复：列表 = 当前 active（current:true 置顶）+ 归档——
    # 当前会话常驻可见，不再随切回/归档消失重现
    from agent.memory.store import save_session

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    save_session(
        Session(messages=[Message(role="user", content="归档过的对话")]),
        sessions_dir / "20260915-230000.json",
    )

    ctx = _make_ctx()
    ctx.session.messages.append(Message(role="system", content="人设"))
    ctx.session.messages.append(Message(role="user", content="正在聊的对话"))
    client = TestClient(create_app(ctx))

    items = client.get("/api/sessions").json()
    assert items[0] == {"name": "active", "title": "正在聊的对话", "time": "", "current": True}
    assert items[1]["name"] == "20260915-230000.json"
    assert "current" not in items[1]


def test_todos_api_roundtrip(tmp_path):
    # 个人待办三端点：添加 201 → 列表 → 勾销 → 404（未知 id）
    client = TestClient(create_app(_make_ctx()))

    resp = client.post("/api/todos", json={"text": "查阳澄湖天气"})
    assert resp.status_code == 201
    todo_id = resp.json()["id"]

    assert [t["text"] for t in client.get("/api/todos").json()] == ["查阳澄湖天气"]

    done = client.post(f"/api/todos/{todo_id}/complete").json()
    assert done["done"] is True
    assert client.get("/api/todos").json()[0]["done"] is True

    assert client.post("/api/todos/99/complete").status_code == 404


def test_cancel_interrupts_running_run():
    # 用注入的 store 造一个正在运行的 Run——避免真线程跑太快、cancel 追不上的竞态
    from agent.server.run_store import STATUS_RUNNING, RunStore

    store = RunStore()
    run = store.create_if_idle()
    run.status = STATUS_RUNNING

    client = TestClient(create_app(_make_ctx(), store=store))
    assert client.post(f"/api/runs/{run.run_id}/cancel").status_code == 200
    assert run.cancel_requested is True


def test_events_unknown_run_404():
    assert _make_client().get("/api/runs/nope/events").status_code == 404


def test_cancel_unknown_run_404():
    assert _make_client().post("/api/runs/nope/cancel").status_code == 404
