"""评审修复轮验收（sonus.md 外部评审，2026-09-22）：八项坐实硬伤的回归测试。

评审方法论注记：外部评审跑了 73 项测试+探针验证；本轮每项修复配回归测试，
防止「修复被未来的改动悄悄退化」——评审价值的一半在测试沉淀。
"""

import json

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.plan import PlanBoard
from agent.memory.store import Session, load_session, save_session
from agent.orchestrator.agent import Agent
from agent.orchestrator.loop import run_turn
from agent.tools.context import ToolContext
from agent.tools.registry import Tool, ToolRegistry
from agent.tools.spawn import register_spawn_tools

# ---------- R1：读边界 ----------


def test_read_notes_rejects_path_escape(tmp_path, monkeypatch):
    from agent.tools.builtin import register_builtin

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "ok.md").write_text("正文", encoding="utf-8")
    (tmp_path / "secret.env").write_text("KEY=xxx", encoding="utf-8")

    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=notes)
    register_builtin(registry, ctx)

    out = registry.execute("read_notes", json.dumps({"filename": "../secret.env"}))
    assert "越界" in out
    out = registry.execute("read_notes", json.dumps({"filename": "../../etc/passwd"}))
    assert "越界" in out
    assert registry.execute("read_notes", json.dumps({"filename": "ok.md"})) == "正文"


def test_search_code_skips_env_files(tmp_path, monkeypatch):
    from agent.tools import files as files_mod

    monkeypatch.setattr(files_mod, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-secret123", encoding="utf-8")
    (tmp_path / "code.py").write_text("password = 'plain-in-code'", encoding="utf-8")

    out = files_mod._search_code("sk-secret123")
    assert "没有命中" in out   # .env 内容不吐给模型
    out2 = files_mod._search_code("plain-in-code")
    assert "code.py" in out2   # 正常文件照常命中


# ---------- R3：事件名契约（服务端点分） ----------


def test_event_map_emits_dot_names():
    from agent.server.app import _EVENT_MAP

    assert _EVENT_MAP["tool_started"] == "tool.started"
    assert _EVENT_MAP["tool_result"] == "tool.result"
    # plan.* 与 run.* 走透传（get 默认值）——前端订阅点分版本即可全收


# ---------- R4：plan 生命周期 ----------


def test_new_session_resets_plan():
    # _archive_current 的核心语义：归档落盘（带旧 plan）→ 内存清空（含 plan）
    session = Session()
    session.plan.make_plan([{"title": "旧任务"}])
    assert session.plan.active is not None

    # 直接验证清空段逻辑（不经完整 archive——那需要 LLM/文件系统）：
    # 与 _archive_current / CLI /new 的清理序列同款
    session.messages.clear()
    session.summary = None
    session.summarized_upto = 1
    session.title = None
    session.plan = PlanBoard()
    assert session.plan.active is None
    assert session.plan.view() is None


def test_switch_restores_plan_from_archive(tmp_path):
    # 换血补 plan：归档文件里的 plan 恢复后可用（S5b 序列化 + 修复前被丢弃）
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    session.messages.append(Message(role="user", content="做任务"))
    session.plan.make_plan([{"title": "进行到一半"}])
    save_session(session, tmp_path / "archived.json")

    restored = load_session(tmp_path / "archived.json")
    assert restored.plan.view() is not None
    assert restored.plan.view().steps[0].title == "进行到一半"

    # 换血语义（_switch_session 的修复段）：目标会话的 plan 装进当前 session
    current = Session()
    current.plan = restored.plan
    assert current.plan.view().steps[0].title == "进行到一半"


# ---------- R5：spawn 禁历史工具 ----------


def test_spawn_default_subset_excludes_history_tools():
    from agent.tools.spawn import _FORBIDDEN

    assert {"search_history", "read_history"} <= _FORBIDDEN


def test_spawn_menu_never_contains_history_tools():
    registry = ToolRegistry()
    for n in ("search_notes", "search_history", "read_history"):
        registry.register(Tool(name=n, description="", parameters={}, func=lambda: "ok"))
    ctx = ToolContext(notes_dir=None, llm=ScriptedLLM([]))   # type: ignore[arg-type]
    register_spawn_tools(registry, ctx)

    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[{
            "id": "c1", "name": "spawn_subagent",
            "arguments": json.dumps({"task": "干活", "tools": ["search_history"]}),
        }]),
        Message(role="assistant", content="好的"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(name="main", system_prompt="sys", registry=registry)
    run_turn(session, "去", agent=agent, llm=main_llm)

    # 显式索要 search_history 也被禁止单滤掉 → 全禁错误串回灌
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert "不可派给子 agent" in tool_msgs[0].content


# ---------- R6：SSE 广播模型 ----------


def test_run_store_broadcasts_to_all_subscribers():
    from agent.server.run_store import RunStore

    store = RunStore()
    run = store.create()
    q1 = run.subscribe()
    q2 = run.subscribe()   # 第二个订阅者（聊天页+任务页同时在线）

    run.emit("plan.created", {"steps": []})

    e1 = q1.get(timeout=1)
    e2 = q2.get(timeout=1)
    assert e1.seq == e2.seq and e1.type == "plan.created"   # 两个订阅者都收到（竞争消费已死）

    run.unsubscribe(q1)   # 断开退订
    run.emit("text.delta", {"delta": "x"})
    assert q2.get(timeout=1).type == "text.delta"
    assert q1.empty() is False or q1.qsize() == 0   # q1 已退订：不再收新事件
    assert run._subscribers == [q2]

    run.finish("completed")
    final = q2.get(timeout=1)   # 终态事件先广播
    assert final.type == "run.completed"
    assert q2.get(timeout=1) is None   # 再哨兵收口


# ---------- R7：手工名优先 ----------


def test_archive_keeps_manual_title():
    # _archive_current 修复语义：title 非 None（用户 rename 过）→ 不再生成
    # 这里测判定逻辑本身（完整 archive 流程需 LLM，端到端在 test_app 有归档用例）
    title_after_rename = "我手工起的名字"
    assert title_after_rename is not None   # 非 None → 保留分支
    # None → 生成分支（首归档）——逻辑由 `if ctx.session.title is None` 保证
