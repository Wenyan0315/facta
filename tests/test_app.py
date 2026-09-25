"""FastAPI 壳验收：Run 生命周期三接口 + 事件流 + S8a 会话 CRUD。

S8a 起 ctx 不带 session/agent（多会话并发要求「一段对话一套 agent」），
改带 store（会话仓库）+ build_agent（工厂）——本文件的 _make_ctx 因此
从「塞一个 Session 进去」变成「造一个 tmp 目录的仓库 + 一个最小工厂」。
"""

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.store import Session, SessionStore
from agent.memory.todos import TodoStore
from agent.orchestrator.agent import Agent
from agent.orchestrator.assemble import AppContext, ensure_persona
from agent.server.app import create_app
from agent.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    """隔离真实磁盘状态：跑 Run 的测试会真落盘（会话 + 记忆）。

    持久状态的系统必须 fixture 隔离（M6.3「测试污染」血案的同款复发，
    那次是它第一次真烧掉用户数据）。S8a 后会话不再有 active 固定位，
    隔离对象从「app 模块的 MEMORY_PATH」变成「SessionStore 的根目录」——
    仓库由 _make_ctx 注入一次性 tmp 目录，这里补还住在模块级的路径常量
    （app.LEARNED_DIR 供记忆面板端点，assemble.LEARNED_DIR 供收官固化）。
    """
    monkeypatch.setattr("agent.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("agent.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")


def _make_ctx(reply: str = "你好！", llm=None, registry=None) -> AppContext:
    """最小 AppContext：ScriptedLLM 回纯文本（不点菜），registry 传 None 也可。

    build_agent 是工厂契约的最小实现：还一个 agent，并顺手保证会话带人设
    （与 assemble 里的真工厂同一条不变量，只是工具集换成注入的 registry）。
    todos 指到 mkdtemp（每次唯一，测试间不串）——避免 todos 端点踩 None。
    """
    tools = registry or ToolRegistry()

    def build_agent(session: Session) -> Agent:
        agent = Agent(name="test", system_prompt="测试人设", registry=tools)
        ensure_persona(session, agent)
        return agent

    return AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=llm or ScriptedLLM([Message(role="assistant", content=reply)]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=build_agent,
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


def _run_to_completion(client, text: str) -> str:
    """发一轮消息并读完事件流（= worker 已收官落盘），返回 session_id。"""
    body = client.post("/api/runs", json={"text": text}).json()
    _read_events(client, body["run_id"])
    return body["session_id"]


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


def test_run_without_session_id_opens_a_new_session():
    # 省略 session_id = 新开一段对话：前端首次发送不必先建会话，响应带回真实
    # id，客户端从此认它；这一轮的 Run 也记在这个 id 名下
    client = _make_client()
    sid = _run_to_completion(client, "你好")

    assert sid
    assert client.get("/api/runs").json()[0]["session_id"] == sid
    assert [s["id"] for s in client.get("/api/sessions").json()] == [sid]


def test_messages_endpoint_filters_to_storyline():
    # 历史回放只讲故事线：system=人设、tool=中间产物、空 content 纯点菜轮都滤掉
    ctx = _make_ctx()
    session = Session()
    session.messages.append(Message(role="system", content="人设"))
    session.messages.append(Message(role="user", content="你好"))
    session.messages.append(Message(role="assistant", content="", tool_calls=[
        {"id": "c1", "name": "get_current_time", "arguments": "{}"}
    ]))
    session.messages.append(Message(role="tool", tool_call_id="c1", content="12:00"))
    session.messages.append(Message(role="assistant", content="现在 12 点"))
    sid = ctx.store.create(session)
    client = TestClient(create_app(ctx))

    assert client.get(f"/api/sessions/{sid}/messages").json() == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "现在 12 点"},
    ]
    assert client.get("/api/sessions/20260913-101956/messages").status_code == 404


def test_runs_list_newest_first_and_filter_by_session():
    from agent.server.run_store import STATUS_COMPLETED, RunStore

    run_store = RunStore()
    old = run_store.create(title="会话A的任务", session_id="20260913-101956")
    old.finish(STATUS_COMPLETED)
    run_store.create(title="会话B的任务", session_id="20260915-230000")   # pending

    client = TestClient(create_app(_make_ctx(), store=run_store))
    runs = client.get("/api/runs").json()
    assert [r["title"] for r in runs] == ["会话B的任务", "会话A的任务"]   # 新的在前
    assert runs[1]["status"] == "completed"

    # 任务视图按会话过滤（「这段对话跑过哪些任务」）
    only_a = client.get("/api/runs", params={"session_id": "20260913-101956"}).json()
    assert [r["title"] for r in only_a] == ["会话A的任务"]


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


def test_notes_page_serves_html():
    # 知识语料面板（042）：第四入口真页面，含「知识语料」标题
    resp = _make_client().get("/notes")
    assert resp.status_code == 200
    assert "知识语料" in resp.text


# ---------- 会话收官（settle_session：补标题 → 增量固化 → 落盘） ----------


def test_run_persists_session_each_turn():
    # 每轮落盘：Web 壳常驻无退出钩子——worker 在 finally 里收官，且必须先于
    # 终态哨兵（读到 run.completed 时盘上必须有这轮的 user+assistant）
    ctx = _make_ctx()
    client = TestClient(create_app(ctx))
    sid = _run_to_completion(client, "记住这句")

    contents = [m.content for m in ctx.store.load(sid).messages]
    assert "记住这句" in contents and "你好！" in contents


def test_worker_seeds_persona_into_session():
    # 人设不变量（S8a 收口进 build_agent 工厂）：新会话跑一轮后，盘上第一条
    # 必须是 system 且只有一条——「有 agent 但没人设」在结构上不存在。
    # 真实复踩过：归档 clear 连 system 一起清，新会话里中文提问收到英文回复。
    ctx = _make_ctx()
    client = TestClient(create_app(ctx))
    sid = _run_to_completion(client, "你好")

    messages = ctx.store.load(sid).messages
    assert messages[0].role == "system"
    assert messages[0].content == "测试人设"
    assert sum(1 for m in messages if m.role == "system") == 1


def test_settle_sets_llm_title():
    # 收官补标题：internal_llm 第 1 次调用 = 提炼标题，写进会话文件
    #（清单读取零 LLM 调用，所以标签必须在收官时就落盘）
    ctx = _make_ctx()
    ctx.internal_llm = ScriptedLLM([Message(role="assistant", content="PHP 工具封装")])
    client = TestClient(create_app(ctx))
    sid = _run_to_completion(client, "PHP 结合 AI Agent 可以做什么")

    assert ctx.store.load(sid).title == "PHP 工具封装"   # 不是首句截断


# ---------- S8a 会话 CRUD（身份=文件名，无 active 特例） ----------


def test_new_session_endpoint_creates_placeholder():
    # 显式新建（前端「新对话」按钮）：空会话也进清单——刚点的新建不该凭空消失
    ctx = _make_ctx()
    client = TestClient(create_app(ctx))

    resp = client.post("/api/sessions")
    assert resp.status_code == 201
    sid = resp.json()["id"]

    items = client.get("/api/sessions").json()
    assert [s["id"] for s in items] == [sid]
    assert set(items[0]) == {"id", "title", "time", "collapsed", "running"}
    assert items[0]["title"] == "（空会话）"
    assert items[0]["running"] is False and items[0]["collapsed"] is False
    assert items[0]["time"]   # 身份即时间戳 → 展示时间从 id 解析，不另存字段


def test_rename_and_delete_session():
    ctx = _make_ctx()
    client = TestClient(create_app(ctx))
    sid = client.post("/api/sessions").json()["id"]

    # 重命名改的是 title 标签，身份（文件名）不动
    assert client.put(f"/api/sessions/{sid}", json={"text": "  改的名字  "}).status_code == 200
    assert client.get("/api/sessions").json()[0]["title"] == "改的名字"
    assert ctx.store.load(sid).title == "改的名字"

    assert client.delete(f"/api/sessions/{sid}").json() == {"ok": True, "deleted": sid}
    assert client.get("/api/sessions").json() == []


def test_session_guards():
    client = _make_client()
    assert client.put("/api/sessions/20260913-101956", json={"text": "x"}).status_code == 404
    assert client.delete("/api/sessions/20260913-101956").status_code == 404
    assert client.put("/api/sessions/20260913-101956", json={"text": "  "}).status_code == 400


def test_illegal_session_id_is_400_not_500():
    # 会话 id 是 HTTP 路径来的外部输入：格式非法必须 400——store.path 抛的
    # ValueError 是信任边界兜底，不该以 500 的形式漏给客户端
    client = _make_client()
    assert client.get("/api/sessions/abc/messages").status_code == 400
    assert client.put("/api/sessions/abc", json={"text": "x"}).status_code == 400
    assert client.delete("/api/sessions/abc").status_code == 400


def test_write_endpoints_409_while_session_running():
    # 准入策略代替锁：worker 整轮独占这段对话（load→改→save），此时任何外部
    # 写都会在 worker 落盘时被覆盖（lost update）。与其用锁把写排队到几十秒后，
    # 不如直接 409 告诉用户「这段对话正在被写」。
    from agent.server.run_store import STATUS_RUNNING, RunStore

    ctx = _make_ctx()
    sid = ctx.store.create(Session())
    run_store = RunStore()
    run_store.create_if_idle(sid).status = STATUS_RUNNING

    client = TestClient(create_app(ctx, store=run_store))
    assert client.put(f"/api/sessions/{sid}", json={"text": "x"}).status_code == 409
    assert client.delete(f"/api/sessions/{sid}").status_code == 409
    # 同一会话的第二轮也被准入挡住（create_if_idle 返回拒绝理由 → 409）
    assert client.post("/api/runs", json={"text": "再来一轮", "session_id": sid}).status_code == 409
    assert client.get("/api/sessions").json()[0]["running"] is True


# ---------- 人设保证（ensure_persona 三分支，S8a 收口进工厂） ----------


def test_ensure_persona_three_branches():
    # 装配不变量：会话必须带 agent 的 system_prompt 开工——Web 入口曾跑过无人设会话
    from agent.orchestrator.agent import DEFAULT_SYSTEM_PROMPT

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


def test_ensure_persona_merges_duplicate_system_messages():
    # 换血 bug 时期残留自愈：头部多条 system 合并为一条，游标左移
    s = Session()
    s.messages = [Message(role="system", content="人设A"), Message(role="system", content="人设B"),
                  Message(role="system", content="人设C"), Message(role="user", content="你好")]
    s.summarized_upto = 4
    ensure_persona(s, Agent(name="test", system_prompt="人设X", registry=ToolRegistry()))
    assert [m.role for m in s.messages] == ["system", "user"]
    assert s.messages[0].content == "人设A"   # 保留第一条
    assert s.summarized_upto == 2              # 4 - 2 条重复


# ---------- 个人待办 ----------


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


# ---------- 取消与未知 run ----------


def test_cancel_interrupts_running_run():
    # 用注入的 store 造一个正在运行的 Run——避免真线程跑太快、cancel 追不上的竞态
    from agent.server.run_store import STATUS_RUNNING, RunStore

    store = RunStore()
    run = store.create_if_idle("20260913-101956")
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


def test_confirm_approve_flow_end_to_end(tmp_path):
    # 全链路：worker 挂起 → POST confirm(approve) → 命令真执行 → run 完成
    # 副作用验证（审计佐证思路）：看文件落没落地，不看模型嘴说
    from agent.tools.context import ToolContext
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


def test_confirm_reject_flow_end_to_end(tmp_path):
    # 拒绝不炸会话：拒绝提示作为工具结果回灌，模型收尾回答，run 正常完成
    from agent.tools.context import ToolContext
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
