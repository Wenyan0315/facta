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


def _make_ctx(reply: str = "你好！", llm=None, registry=None) -> AppContext:
    # 最小 AppContext：ScriptedLLM 回纯文本（不点菜），registry 传 None 也可。
    # todos 指到临时目录（mkdtemp 每次唯一，测试间不串）——避免调用 todos
    # 端点的测试踩到 None 路径。llm/registry 可注入（S4b 确认流端到端用）；
    # agent 包 registry（S5a：_run_worker/ensure_persona 消费 ctx.agent）
    import tempfile
    from pathlib import Path

    from agent.memory.todos import TodoStore
    from agent.orchestrator.agent import Agent
    from agent.tools.registry import ToolRegistry

    return AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=llm or ScriptedLLM([Message(role="assistant", content=reply)]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        session=Session(),
        registry=registry,
        agent=Agent(
            name="test", system_prompt="测试人设", registry=registry or ToolRegistry()
        ),
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


def test_graph_page_serves_html():
    # 知识图谱面板（S7b）：第三入口真页面，含「知识图谱」标题
    resp = _make_client().get("/graph")
    assert resp.status_code == 200
    assert "知识图谱" in resp.text


def test_ensure_persona_three_branches():
    # 装配不变量：会话必须带 agent 的 system_prompt 开工——Web 入口曾跑过无人设会话
    from agent.core.types import Message
    from agent.memory.store import Session
    from agent.orchestrator.agent import DEFAULT_SYSTEM_PROMPT, Agent
    from agent.orchestrator.assemble import ensure_persona
    from agent.tools.registry import ToolRegistry

    agent = Agent(name="test", system_prompt=DEFAULT_SYSTEM_PROMPT, registry=ToolRegistry())

    # 空会话：种人设
    fresh = Session()
    ensure_persona(fresh, agent)
    assert fresh.messages[0].role == "system"
    assert fresh.messages[0].content == DEFAULT_SYSTEM_PROMPT

    # 历史遗留的无 system 会话：头部补插 + 摘要游标随位移 +1
    legacy = Session()
    legacy.messages.append(Message(role="user", content="旧消息"))
    legacy.summarized_upto = 3
    ensure_persona(legacy, agent)
    assert [m.role for m in legacy.messages] == ["system", "user"]
    assert legacy.summarized_upto == 4

    # 正常会话（已有 system）：不动
    normal = Session()
    normal.messages.append(Message(role="system", content="人设"))
    normal.messages.append(Message(role="user", content="你好"))
    ensure_persona(normal, agent)
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
    # （真实复踩：新会话里中文提问收到英文回复）。修复后归档即补种人设
    # （S5a 起人设来自 ctx.agent），新 active 落盘/内存都带 system。
    ctx = _make_ctx()
    ctx.session.messages.append(Message(role="system", content=ctx.agent.system_prompt))
    ctx.session.messages.append(Message(role="user", content="第一场对话"))

    client = TestClient(create_app(ctx))
    assert client.post("/api/sessions/new").status_code == 200

    assert [m.role for m in ctx.session.messages] == ["system"]   # 内存：新会话带人设
    assert ctx.session.messages[0].content == ctx.agent.system_prompt
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


def test_ensure_persona_merges_duplicate_system_messages():
    # 换血 bug 时期残留自愈：头部多条 system 合并为一条，游标左移
    from agent.core.types import Message
    from agent.memory.store import Session
    from agent.orchestrator.agent import Agent
    from agent.orchestrator.assemble import ensure_persona
    from agent.tools.registry import ToolRegistry

    s = Session()
    s.messages = [Message(role="system", content="人设A"), Message(role="system", content="人设B"),
                  Message(role="system", content="人设C"), Message(role="user", content="你好")]
    s.summarized_upto = 4
    ensure_persona(s, Agent(name="test", system_prompt="人设X", registry=ToolRegistry()))
    assert [m.role for m in s.messages] == ["system", "user"]
    assert s.messages[0].content == "人设A"   # 保留第一条
    assert s.summarized_upto == 2              # 4 - 2 条重复


def test_switch_replaces_memory_completely(tmp_path):
    # 换血无条件清：空会话切换不残留旧消息（4 条 system 的根因）
    from agent.memory.store import save_session

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    target = sessions_dir / "20260913-101956.json"
    save_session(Session(messages=[Message(role="system", content="目标人设"),
                                   Message(role="user", content="目标会话")]), target)

    ctx = _make_ctx()   # 当前空会话（仅启动时的 system？不——空 Session 无消息）
    client = TestClient(create_app(ctx))
    assert client.post("/api/sessions/20260913-101956.json/switch").status_code == 200

    # 换血后内存 = 目标会话原样，不与切换前的任何残留拼接
    assert [m.role for m in ctx.session.messages] == ["system", "user"]
    assert ctx.session.messages[1].content == "目标会话"
    assert sum(1 for m in ctx.session.messages if m.role == "system") == 1


def test_todos_api_roundtrip():
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


def test_todos_update_and_delete_api():
    # 修改（PUT）与删除（DELETE）端点
    client = TestClient(create_app(_make_ctx()))

    todo = client.post("/api/todos", json={"text": "写错的"}).json()
    updated = client.put(f"/api/todos/{todo['id']}", json={"text": "改对的"}).json()
    assert updated["text"] == "改对的"

    assert client.delete(f"/api/todos/{todo['id']}").json()["deleted"] == todo["id"]
    assert client.get("/api/todos").json() == []
    assert client.delete("/api/todos/99").status_code == 404
    assert client.put("/api/todos/99", json={"text": "x"}).status_code == 404


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


# ---------- S4b：L2 确认端点 ----------

def _confirm_llm(command: str, final_reply: str) -> ScriptedLLM:
    # 剧本：先点菜 run_command（非白名单命令 → 挂起弹窗），收工具结果后收尾
    return ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "run_command",
             "arguments": json.dumps({"command": command})},
        ]),
        Message(role="assistant", content=final_reply),
    ])


def _resolve_confirm_when_pending(client, run_id: str, approve: bool):
    # worker 到确认挂起点有毫秒级竞态：轮询到端点不再是 409（无挂起）
    import time as _time

    resp = None
    for _ in range(100):   # 最多 5s
        resp = client.post(f"/api/runs/{run_id}/confirm", json={"approve": approve})
        if resp.status_code == 200:
            return resp
        _time.sleep(0.05)
    return resp


def test_confirm_endpoint_409_without_pending_404_unknown_run():
    client = _make_client()
    run_id = client.post("/api/runs", json={"text": "你好"}).json()["run_id"]
    _read_events(client, run_id)   # 读完事件流 = 已收尾，无挂起确认

    assert client.post(f"/api/runs/{run_id}/confirm", json={"approve": True}).status_code == 409
    assert client.post("/api/runs/nope/confirm", json={"approve": True}).status_code == 404


def test_confirm_approve_flow_end_to_end(tmp_path, monkeypatch):
    # 全链路：worker 挂起 → POST confirm(approve) → 命令真执行 → run 完成
    # 副作用验证（审计佐证思路）：看文件落没落地，不看模型嘴说
    from agent.tools.context import ToolContext
    from agent.tools.registry import ToolRegistry
    from agent.tools.terminal import register_terminal_tools

    registry = ToolRegistry()
    register_terminal_tools(registry, ToolContext(notes_dir=tmp_path, workspace_root=tmp_path))
    ctx = _make_ctx(llm=_confirm_llm("touch approved.txt", "已执行"), registry=registry)
    client = TestClient(create_app(ctx))

    run_id = client.post("/api/runs", json={"text": "建个文件"}).json()["run_id"]

    assert _resolve_confirm_when_pending(client, run_id, approve=True).status_code == 200

    events = _read_events(client, run_id)
    types = [e["type"] for e in events]
    assert "confirm.request" in types and "confirm.resolved" in types
    assert types[-1] == "run.completed"
    assert (tmp_path / "approved.txt").exists()   # 批准后命令真跑了


def test_confirm_reject_flow_end_to_end(tmp_path, monkeypatch):
    # 拒绝不炸会话：拒绝提示作为工具结果回灌，模型收尾回答，run 正常完成
    from agent.tools.context import ToolContext
    from agent.tools.registry import ToolRegistry
    from agent.tools.terminal import register_terminal_tools

    registry = ToolRegistry()
    register_terminal_tools(registry, ToolContext(notes_dir=tmp_path, workspace_root=tmp_path))
    ctx = _make_ctx(llm=_confirm_llm("touch pwned.txt", "好的，我换个方案"), registry=registry)
    client = TestClient(create_app(ctx))

    run_id = client.post("/api/runs", json={"text": "建个文件"}).json()["run_id"]

    assert _resolve_confirm_when_pending(client, run_id, approve=False).status_code == 200

    events = _read_events(client, run_id)
    assert [e["type"] for e in events][-1] == "run.completed"   # 拒绝后仍正常收口
    tool_results = [e["data"]["result"] for e in events if e["type"] == "tool.result"]
    assert any("用户拒绝了" in r for r in tool_results)
    assert not (tmp_path / "pwned.txt").exists()   # 拒绝 = 根本没执行


# ---------- S7b：知识图谱面板端点 ----------


def _graph_ctx() -> AppContext:
    """带预填图的 ctx：两个实体一条边——panels 数据返回的最小非空样本。"""
    from agent.knowledge.graph import GraphStore

    ctx = _make_ctx()
    graph = GraphStore()
    graph.add_node("Embedding")
    graph.add_node("向量数据库")
    graph.add_edge("Embedding", "向量数据库", "依赖", "RAG.md")
    ctx.graph = graph
    return ctx


def test_graph_data_endpoint_returns_nodes_edges_stats():
    # 面板一次拉全量：nodes + edges（含出处）+ overview 统计，前端不再算统计
    client = TestClient(create_app(_graph_ctx()))

    data = client.get("/api/graph").json()
    assert [n["id"] for n in data["nodes"]] == ["Embedding", "向量数据库"]
    assert len(data["edges"]) == 1
    assert data["edges"][0]["relation"] == "依赖"
    assert data["edges"][0]["source_note"] == "RAG.md"   # 出处随边透出（可溯源）
    assert data["stats"]["nodes"] == 2
    assert data["stats"]["edges"] == 1


def test_graph_rebuild_forces_full_resync(tmp_path, monkeypatch):
    # 「重建图谱」= force 全量：清空指纹 → sync_graph。internal_llm 是
    # ScriptedLLM([])（抽取输出非法 JSON → failed），但端点行为可断言：
    # 指纹被清空（force 生效）、report/stats 结构返回、失败不记指纹
    import agent.server.app as app_module
    from agent.knowledge.graph import GraphStore

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# 概念A\n依赖概念B", encoding="utf-8")
    monkeypatch.setattr(app_module, "NOTES_DIR", notes)
    monkeypatch.setattr(app_module, "GRAPH_PATH", tmp_path / "graph.json")

    ctx = _make_ctx()
    ctx.graph = GraphStore()
    ctx.graph.note_hashes["a.md"] = "stale-fingerprint"   # 预置指纹，验证 force 清空
    client = TestClient(create_app(ctx))

    resp = client.post("/api/graph/rebuild")
    assert resp.status_code == 200
    body = resp.json()
    assert body["report"]["failed"] == 1   # ScriptedLLM 抽取失败（非法 JSON）
    assert set(body["report"]) == {"extracted", "unchanged", "removed", "skipped", "failed"}
    # force 清空后失败篇不记指纹 → 指纹保持空（下次 rebuild 会再试）
    assert ctx.graph.note_hashes == {}
