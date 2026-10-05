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

from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext, ensure_persona
from facta.server.app import create_app
from facta.server.run_store import RunStore
from facta.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    """隔离真实磁盘状态：跑 Run 的测试会真落盘（会话 + 记忆）。

    持久状态的系统必须 fixture 隔离（M6.3「测试污染」血案的同款复发，
    那次是它第一次真烧掉用户数据）。S8a 后会话不再有 active 固定位，
    隔离对象从「app 模块的 MEMORY_PATH」变成「SessionStore 的根目录」——
    仓库由 _make_ctx 注入一次性 tmp 目录，这里补还住在模块级的路径常量
    （app.LEARNED_DIR 供记忆面板端点，assemble.LEARNED_DIR 供收官固化）。
    """
    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")
    # ADR 078：settle 尾部的 sleep-time 整理会读 user.md（不隔离会摸真 home
    # 的隐私文件，超限时还会真 sweep 它的墓碑——测试副作用不可接受）
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))


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
    """发一轮消息并读完事件流（= 保底已落盘、run 终态已推；ADR 076 起
    异步收官可能仍在跑——需要断言标题/游标时等 run._settle_done），
    返回 session_id。"""
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
    from facta.server.run_store import STATUS_COMPLETED, RunStore

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
    # ADR 076：settle 已挪到 run.completed 之后异步跑——读完事件流不再
    # 等于收官完成，注入 RunStore 等 _settle_done 再断言
    ctx = _make_ctx()
    ctx.internal_llm = ScriptedLLM([Message(role="assistant", content="PHP 工具封装")])
    rs = RunStore()
    client = TestClient(create_app(ctx, store=rs))
    body = client.post("/api/runs", json={"text": "PHP 结合 AI Agent 可以做什么"}).json()
    _read_events(client, body["run_id"])
    assert rs.get(body["run_id"])._settle_done.wait(timeout=10)

    assert ctx.store.load(body["session_id"]).title == "PHP 工具封装"   # 不是首句截断


# ---------- 收官容错（P1-5/P2-7 评审修复：固化失败不拖死对话保存） ----------

import facta.orchestrator.assemble as asm  # noqa: E402  # settle 的 monkeypatch 靶子
from facta.memory.store import derive_title  # noqa: E402


def _dialogue(n: int = 3) -> Session:
    s = Session()
    for i in range(n):
        s.messages.append(Message(role="user", content=f"第{i}句"))
        s.messages.append(Message(role="assistant", content=f"答{i}"))
    return s


def test_settle_saves_dialog_even_if_consolidate_crashes(tmp_path, monkeypatch):
    """评审故障注入复现：固化抛异常曾把 store.save 一起拖死——已回答的
    文本在刷新后消失。ADR 076 后保底 save 归调用方（进场前落盘），
    游标不动、报告异常。
    """
    store = SessionStore(tmp_path / "sessions")
    session = _dialogue()
    sid = store.create(Session())
    store.save(sid, session)   # 调用方契约：保底落盘先于 settle
    monkeypatch.setattr(asm, "CONSOLIDATE_THRESHOLD", 1)

    def _boom(*args, **kwargs):
        raise RuntimeError("固化模型炸了")

    monkeypatch.setattr(asm, "consolidate", _boom)

    report = asm.settle_session(session, sid, store, ScriptedLLM([]))

    saved = store.load(sid)
    assert len(saved.messages) == len(session.messages)   # 对话本体没丢
    assert "异常中断" in report
    assert saved.consolidated_upto == 0                   # 游标未推进，下轮重试


def test_settle_keeps_cursor_when_consolidate_reports_failure(tmp_path, monkeypatch):
    # P2-7：consolidate 返回 (report, False)（坏 JSON）——游标不推进，
    # 否则这批对话永远不会再被复盘（记忆静默丢失）
    store = SessionStore(tmp_path / "sessions")
    session = _dialogue()
    sid = store.create(Session())
    store.save(sid, session)   # 调用方契约：保底落盘先于 settle
    monkeypatch.setattr(asm, "CONSOLIDATE_THRESHOLD", 1)
    monkeypatch.setattr(asm, "consolidate", lambda *a, **k: ("记忆固化：坏 JSON，未写入", False))

    asm.settle_session(session, sid, store, ScriptedLLM([]))

    assert store.load(sid).consolidated_upto == 0


def test_settle_advances_cursor_on_success(tmp_path, monkeypatch):
    # 正常路径（含「全驳回/无条目」这类 ok=True 的零写入结局）：游标推进并落盘
    store = SessionStore(tmp_path / "sessions")
    session = _dialogue()
    sid = store.create(Session())
    store.save(sid, session)   # 调用方契约：保底落盘先于 settle
    monkeypatch.setattr(asm, "CONSOLIDATE_THRESHOLD", 1)
    monkeypatch.setattr(asm, "consolidate", lambda *a, **k: ("记忆固化：新增 1 条", True))

    asm.settle_session(session, sid, store, ScriptedLLM([]))

    assert store.load(sid).consolidated_upto == len(session.messages)


def test_settle_title_failure_falls_back_to_first_line(tmp_path, monkeypatch):
    # 标题提炼（LLM）失败：退回首句派生，照样保存——标题是增益不是本体
    store = SessionStore(tmp_path / "sessions")
    session = _dialogue(1)
    sid = store.create(Session())
    store.save(sid, session)   # 调用方契约：保底落盘先于 settle

    def _boom(*args, **kwargs):
        raise RuntimeError("标题炸了")

    monkeypatch.setattr(asm, "summarize_title", _boom)

    asm.settle_session(session, sid, store, ScriptedLLM([]))

    assert store.load(sid).title == derive_title(session)


# ---------- ADR 076 窄写合并：异步收官不覆盖下一轮的新消息 ----------


def test_settle_narrow_write_keeps_next_round_messages(tmp_path, monkeypatch):
    # 主验收：settle 异步落盘时同会话下一轮已 append——终态写必须窄写合并。
    # 若仍全量回写本轮快照，下一轮的新消息会被盖掉（P1-5 的 lost update 变体）
    store = SessionStore(tmp_path / "sessions")
    session = _dialogue(3)   # 本轮快照：6 条
    sid = store.create(Session())
    monkeypatch.setattr(asm, "CONSOLIDATE_THRESHOLD", 1)
    monkeypatch.setattr(asm, "consolidate", lambda *a, **k: ("记忆固化：新增 1 条", True))

    # 保底落盘后模拟下一轮进场 append（worker2 的 save 先落）
    store.save(sid, session)
    nxt = store.load(sid)
    nxt.messages.append(Message(role="user", content="下一轮的新问题"))
    nxt.messages.append(Message(role="assistant", content="下一轮的新回答"))
    store.save(sid, nxt)

    asm.settle_session(session, sid, store, ScriptedLLM([]))

    saved = store.load(sid)
    contents = [m.content for m in saved.messages]
    assert "下一轮的新问题" in contents and "下一轮的新回答" in contents   # 新消息没丢
    assert len(saved.messages) == 8
    assert saved.consolidated_upto == len(session.messages)   # 游标推进到本轮快照末端（前缀共享）


def test_settle_narrow_write_keeps_renamed_title(tmp_path):
    # 用户 rename（title 非 None）后，settle 的 LLM 标题晚到也不覆盖——
    # 「自动生成用于填空，不覆盖用户主动编辑」纪律在异步时序下依然成立
    store = SessionStore(tmp_path / "sessions")
    session = _dialogue(1)
    sid = store.create(Session())
    store.save(sid, session)
    renamed = store.load(sid)
    renamed.title = "用户改的名字"
    store.save(sid, renamed)

    asm.settle_session(session, sid, store, ScriptedLLM([Message(role="assistant", content="LLM 起的名字")]))

    assert store.load(sid).title == "用户改的名字"


def test_settle_cursor_holds_when_fresh_shrank(tmp_path, monkeypatch):
    # fresh 已被下一轮压缩截短（位置语义撕裂）→ 游标不推，下轮重烧——
    # 重复优于跳过（P2-7 语义）
    store = SessionStore(tmp_path / "sessions")
    session = _dialogue(3)   # 6 条
    sid = store.create(Session())
    monkeypatch.setattr(asm, "CONSOLIDATE_THRESHOLD", 1)
    monkeypatch.setattr(asm, "consolidate", lambda *a, **k: ("记忆固化：新增 1 条", True))
    store.save(sid, session)

    # 模拟下一轮压缩：盘上 messages 比本轮快照短
    shrunk = store.load(sid)
    shrunk.messages = shrunk.messages[:4] + [Message(role="user", content="（摘要）")]
    shrunk.consolidated_upto = 0
    store.save(sid, shrunk)

    asm.settle_session(session, sid, store, ScriptedLLM([]))

    assert store.load(sid).consolidated_upto == 0   # len(fresh)=5 < 目标 6 → 不推


# ---------- ADR 078：sleep-time 整理挂点（settle 尾部，超限才跑） ----------


def test_settle_runs_memory_maintenance_when_over_budget(tmp_path, monkeypatch):
    """收官链尾接整理 pass：注入超预算时 settle 报告带「记忆整理」，
    且老墓碑被物理回收（LEARNED_DIR/user.md 均由 fixture 隔离）。"""
    store = SessionStore(tmp_path / "sessions")
    session = _dialogue(1)
    sid = store.create(Session())
    store.save(sid, session)
    monkeypatch.setattr(asm, "CONSOLIDATE_THRESHOLD", 10**9)   # 不走固化，只测整理

    monkeypatch.setenv("FACTA_MEMORY_BUDGET", "1")   # 必超限（有内容即超）
    learned = asm.LEARNED_DIR
    learned.mkdir(parents=True, exist_ok=True)
    (learned / "other.md").write_text(
        "- [2026-09-01] [已撤回:2026-09-01] 老墓碑\n- [2026-09-02] 在用条目\n",
        encoding="utf-8",
    )

    report = asm.settle_session(session, sid, store, ScriptedLLM([]))

    assert "记忆整理" in report and "物理回收 1 条" in report
    assert "在用条目" in (learned / "other.md").read_text(encoding="utf-8")
    assert "老墓碑" not in (learned / "other.md").read_text(encoding="utf-8")


# ---------- 语义 RAG 降级判定（P1-1 评审修复） ----------

def test_rag_degrades_without_siliconflow_key(monkeypatch):
    # 评审复现：只配 DEEPSEEK key 时 assemble 曾崩在 embedder 构造
    #（RuntimeError 缺少 SILICONFLOW_API_KEY）——README 承诺不装 [rag]
    # 也能跑。现在缺 key 降级词袋，主聊天不受影响。
    monkeypatch.delenv("FACTA_EMBED_PROVIDER", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    assert asm._rag_missing_reason("deepseek") == "缺 SILICONFLOW_API_KEY"


def test_rag_degrades_without_chromadb(monkeypatch):
    # sys.modules 里挂 None = import 必炸（CPython 语义），环境无关地模拟
    # 「没装 [rag] extra」
    import sys
    monkeypatch.delenv("FACTA_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "chromadb", None)
    assert asm._rag_missing_reason("deepseek") == "未安装 [rag] 依赖（chromadb）"


def test_rag_teaching_providers_never_degrade_report():
    # 教学组合本来就词袋：不算「降级」，不产生误导性日志
    assert asm._rag_missing_reason("mock") is None


# ---------- 兼容多家供应商（ADR 070 开源兼容轮） ----------


def test_rag_key_follows_configured_embed_provider(monkeypatch):
    # ADR 070：FACTA_EMBED_PROVIDER=openai 时，缺 OPENAI_API_KEY 才报缺 key，
    # 不会再说「缺 SILICONFLOW_API_KEY」——硅基不再是硬编码。
    from facta.knowledge.knowledge_base import EMBED_PROVIDERS
    monkeypatch.setenv("FACTA_EMBED_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    reason = asm._rag_missing_reason("deepseek")
    assert reason == f"缺 {EMBED_PROVIDERS['openai']['prefix']}_API_KEY"


def test_rag_rejects_unknown_embed_provider(monkeypatch):
    # FACTA_EMBED_PROVIDER 拼错时给清晰提示（含可选清单），不静默走错路径
    monkeypatch.setenv("FACTA_EMBED_PROVIDER", "bogus-llm")
    reason = asm._rag_missing_reason("deepseek")
    assert reason is not None and "bogus-llm" in reason and "siliconflow" in reason


def test_rag_openai_key_unlocks_full_path(monkeypatch):
    # OPENAI_API_KEY 配齐 + chromadb 在 = 不再降级（回归实证：
    # embedder 装配改走 configured_embed_provider 后还能识别「已配齐」）
    import sys
    monkeypatch.setenv("FACTA_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delitem(sys.modules, "chromadb", raising=False)
    # 探依赖：缺 chromadb 的测试分支不能影响「key 已配齐」的判定
    try:
        import chromadb  # noqa: F401
        assert asm._rag_missing_reason("deepseek") is None
    except ImportError:
        # 测试环境没装 [rag]：降级词袋是正确行为；本断言变成「配置已识别」
        assert asm._rag_missing_reason("deepseek") == "未安装 [rag] 依赖（chromadb）"


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
    from facta.server.run_store import STATUS_RUNNING, RunStore

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
    from facta.orchestrator.agent import DEFAULT_SYSTEM_PROMPT

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

    # 正常会话（已有 system 且与 agent 一致）：不动
    normal = Session()
    normal.messages.append(Message(role="system", content=DEFAULT_SYSTEM_PROMPT))
    normal.messages.append(Message(role="user", content="你好"))
    ensure_persona(normal, agent)
    assert len(normal.messages) == 2
    assert normal.messages[0].content == DEFAULT_SYSTEM_PROMPT

    # system 过期（P1-4 评审修复）：就地刷新——旧会话头部的快照不是记忆真值，
    # 记忆面板改/删后旧快照必须让位，否则被删的记忆仍进 payload
    stale = Session()
    stale.messages.append(Message(role="system", content="过期人设+旧记忆快照"))
    stale.messages.append(Message(role="user", content="你好"))
    stale.summarized_upto = 2   # 替换只动 content 不动位置：游标必须原样
    ensure_persona(stale, agent)
    assert stale.messages[0].content == DEFAULT_SYSTEM_PROMPT
    assert stale.summarized_upto == 2


def test_ensure_persona_merges_duplicate_system_messages():
    # 换血 bug 时期残留自愈：头部多条 system 合并为一条，游标左移
    s = Session()
    s.messages = [Message(role="system", content="人设A"), Message(role="system", content="人设B"),
                  Message(role="system", content="人设C"), Message(role="user", content="你好")]
    s.summarized_upto = 4
    ensure_persona(s, Agent(name="test", system_prompt="人设X", registry=ToolRegistry()))
    assert [m.role for m in s.messages] == ["system", "user"]
    # P1-4：合并保留第一条是去重语义；随后照第三分支刷新为 agent 当前 prompt
    # （合并的旧快照同样是过期快照，无保留价值）
    assert s.messages[0].content == "人设X"
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
    from facta.server.run_store import STATUS_RUNNING, RunStore

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
    from facta.tools.context import ToolContext
    from facta.tools.terminal import register_terminal_tools

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
    from facta.tools.context import ToolContext
    from facta.tools.terminal import register_terminal_tools

    registry = ToolRegistry()
    register_terminal_tools(registry, ToolContext(notes_dir=tmp_path, workspace_root=tmp_path))
    ctx = _make_ctx(llm=_confirm_llm("touch pwned.txt", "好的，我换个方案"), registry=registry)
    client = TestClient(create_app(ctx))

    run_id = client.post("/api/runs", json={"text": "建个文件"}).json()["run_id"]

    assert _resolve_confirm_when_pending(client, run_id, approve=False).status_code == 200

    events = _read_events(client, run_id)
    assert [e["type"] for e in events][-1] == "run.completed"   # 拒绝后仍正常收口
    tool_results = [e["data"]["result"] for e in events if e["type"] == "tool.result"]
    assert any("确认闸门拒绝" in r for r in tool_results)
    assert not (tmp_path / "pwned.txt").exists()   # 拒绝 = 根本没执行


# ---------- S7b：知识图谱面板端点 ----------


def _graph_ctx() -> AppContext:
    """带预填图的 ctx：两个实体一条边——panels 数据返回的最小非空样本。"""
    from facta.knowledge.graph import GraphStore

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
    import facta.server.app as app_module
    from facta.knowledge.graph import GraphStore

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
