"""实机冒烟回归套件：四条核心路径的 HTTP SSE 端到端验证。

评审修复轮（sonus.md）8 项硬伤有 5 项是集成路径 bug——单元全对、接线全错。
376 个测试绿的但端到端路径没人自动跑。本套件覆盖：
1. 事件契约（event names 点分 + SSE envelope 结构）——R3/S5c 回归
2. plan 生命周期通过 SSE——R3/R4 回归
3. spawn 分派通过 SSE——R3/R5 回归（噪声隔离：子 agent 事件不泄漏到主流）
4. 路由降级（无 router → fail-open）——M10 三态生命周期状态 A 回归
"""

import json
import tempfile
import time
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
from agent.tools.context import ToolContext
from agent.tools.plan import register_plan_tools
from agent.tools.registry import Tool, ToolRegistry
from agent.tools.spawn import register_spawn_tools


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    """隔离真实磁盘状态（与 test_app.py 同款 fixture）：worker 收官会真落盘。"""
    monkeypatch.setattr("agent.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("agent.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")


def _call(name: str, args: dict) -> dict:
    """构造 tool_call 字典（与 test_spawn/test_plan 同款）。"""
    return {"id": f"call_{name}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def _read_events(client, run_id: str) -> list[dict]:
    """读 SSE 事件流（与 test_app.py 同款）。"""
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def _resolve_confirm(client, run_id: str, approve: bool = True):
    """轮询确认端点直到 pending（与 test_app.py 同款）。"""
    resp = None
    for _ in range(100):   # 最多 5s
        resp = client.post(f"/api/runs/{run_id}/confirm", json={"approve": approve})
        if resp.status_code == 200:
            return resp
        time.sleep(0.05)
    return resp


def _make_ctx(
    main_llm: ScriptedLLM,
    internal_llm: ScriptedLLM | None = None,
    *,
    with_plan: bool = False,
    with_spawn: bool = False,
) -> AppContext:
    """冒烟测试专用 AppContext：按需注册 plan/spawn 工具。

    S8a 起工具集在 build_agent 工厂里现造（与 assemble 的 session_registry 同一
    形状）：plan 工具需要 ToolContext.session（PlanBoard 住 Session 上）、spawn
    工具需要 ToolContext.llm（子 agent 的 LLM = internal_llm），两者都必须抓
    worker 从仓库 load 出来的那个会话对象——所以只能等工厂拿到 session 再建。
    """
    sub_llm = internal_llm or ScriptedLLM([])

    def build_agent(session: Session) -> Agent:
        registry = ToolRegistry()
        tool_ctx = ToolContext(
            notes_dir=None,
            llm=sub_llm,
            history=session.messages,
            session=session,
        )
        if with_plan:
            register_plan_tools(registry, tool_ctx)
        if with_spawn:
            # 子 agent 需要一个工具可用（echo 搜索工具）
            registry.register(Tool(
                name="search_notes",
                description="冒烟 echo 工具",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                func=lambda query, **kw: f"搜索结果：{query}",
            ))
            register_spawn_tools(registry, tool_ctx)

        agent = Agent(name="smoke", system_prompt="冒烟测试人设", registry=registry)
        ensure_persona(session, agent)   # 工厂契约：还的 agent 与会话都保证带人设
        return agent

    return AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=main_llm,
        internal_llm=sub_llm,
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=build_agent,
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
    )


# ---------- 1. 事件契约：点分命名 + SSE envelope 结构 ----------


def test_smoke_event_contract_through_sse():
    """所有事件 type 遵循 domain.action 格式 + envelope 四键齐全 + seq 单调。

    回归目标：R3（事件名契约——tool_started→tool.started）+ S5c（信封解包——
    data 是载荷本体，非外层信封）。评审修复轮的 5 项集成 bug 里，事件名
    契约断裂和信封未解包是前端从 SSE 读不到/读错事件的根因。
    """
    ctx = _make_ctx(ScriptedLLM([Message(role="assistant", content="你好")]))
    client = TestClient(create_app(ctx))

    run_id = client.post("/api/runs", json={"text": "你好"}).json()["run_id"]
    events = _read_events(client, run_id)
    types = [e["type"] for e in events]

    # 事件名契约：全部 domain.action 格式（含点号，无下划线）
    assert all("." in t for t in types), f"事件名缺点号：{types}"
    assert not any("_" in t for t in types), f"事件名含下划线：{types}"
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert "text.delta" in types

    # SSE envelope 结构：四键齐全，data 是载荷本体（dict，非信封嵌套）
    for ev in events:
        assert set(ev.keys()) == {"schema_version", "run_id", "seq", "type", "data"}
        assert ev["run_id"] == run_id
        assert isinstance(ev["data"], dict)
        assert ev["schema_version"] == "1"

    # seq 单调递增
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)


# ---------- 2. plan 生命周期：plan.* 事件通过 SSE 端到端 ----------


def test_smoke_plan_lifecycle_through_sse():
    """make_plan → update_plan_step → finish_plan 的 plan.* 事件经 SSE 流出。

    回归目标：R3（plan.created/step_updated/finished 点分命名通过 SSE）+
    R4（plan 生命周期可见——/new 清 plan 的修复保证活跃计划在 SSE 可观测）。
    现有 test_plan 验了 on_event 链路，这里验 HTTP SSE 链路（_EVENT_MAP 透传）。
    """
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("make_plan", {"steps": [{"title": "查资料"}, {"title": "写总结"}]}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 1, "status": "done", "note": "查完了"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("update_plan_step", {"step_id": 2, "status": "done", "note": "写完了"}),
        ]),
        Message(role="assistant", content="", tool_calls=[
            _call("finish_plan", {"summary": "资料已查，总结完成"}),
        ]),
        Message(role="assistant", content="任务完成，资料在这里"),
    ])
    ctx = _make_ctx(main_llm, with_plan=True)
    client = TestClient(create_app(ctx))

    run_id = client.post("/api/runs", json={"text": "查点资料然后总结"}).json()["run_id"]
    # make_plan 标 needs_confirmation=True → 轮询确认端点批准
    _resolve_confirm(client, run_id, approve=True)
    events = _read_events(client, run_id)
    types = [e["type"] for e in events]

    # 事件序列：plan.created/step_updated/finished 都出现在 SSE 流中
    # （经 _EVENT_MAP 透传——plan.* 已是点分名，不映射）
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert "plan.created" in types
    assert "plan.step_updated" in types
    assert "plan.finished" in types

    # 因果序：每个 plan 事件在其对应的 tool.result 之前
    # （_forward_plan_events 在工具执行后、tool_result 前转发——
    #  confirm.request/resolved 可能在 tool.started 与 plan.created 之间，
    #  不影响因果序：plan 事件先于 tool.result）
    for plan_type in ("plan.created", "plan.step_updated", "plan.finished"):
        plan_idx = types.index(plan_type)
        # 找最近的 tool.result（在 plan 事件之后）
        rest = types[plan_idx + 1:]
        assert "tool.result" in rest, f"{plan_type} 后无 tool.result（因果序断裂）"

    # plan.created 的 data 带步骤
    created = next(e for e in events if e["type"] == "plan.created")
    assert "steps" in created["data"]


# ---------- 3. spawn 分派：tool.started/result 通过 SSE + 噪声隔离 ----------


def test_smoke_spawn_through_sse():
    """spawn_subagent 的 tool.started/result 通过 SSE + 子 agent 事件不泄漏。

    回归目标：R3（tool.started/result 点分命名）+ R5（spawn 噪声隔离——
    子 agent 的中间工具事件不出现在主 SSE 流）。spawn 的 run_turn 不传
    on_event，所以子 agent 的事件天然不进主流——这是设计保证，本测试验它没退化。
    """
    # 子 agent 的 LLM：先点 search_notes，再出结论
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("search_notes", {"query": "向量库"}),
        ]),
        Message(role="assistant", content="结论：向量库是 ChromaVectorStore"),
    ])
    # 主 agent 的 LLM：先点 spawn_subagent，再出最终回复
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "查向量库是什么"}),
        ]),
        Message(role="assistant", content="已让子任务查完：向量库是 ChromaVectorStore"),
    ])
    ctx = _make_ctx(main_llm, internal_llm=sub_llm, with_spawn=True)
    client = TestClient(create_app(ctx))

    run_id = client.post("/api/runs", json={"text": "查向量库"}).json()["run_id"]
    events = _read_events(client, run_id)
    types = [e["type"] for e in events]

    assert types[0] == "run.started"
    assert types[-1] == "run.completed"

    # spawn_subagent 的 tool.started 和 tool.result 在主流可见
    tool_starts = [e for e in events if e["type"] == "tool.started"]
    assert any(e["data"]["name"] == "spawn_subagent" for e in tool_starts)

    tool_results = [e for e in events if e["type"] == "tool.result"]
    assert any(e["data"]["name"] == "spawn_subagent" for e in tool_results)

    # 噪声隔离：子 agent 的 search_notes 事件不出现在主 SSE 流
    # （spawn 的 run_turn 不传 on_event，子 agent 的 tool.started/result 天然不进主流）
    assert not any(
        e["data"].get("name") == "search_notes"
        for e in events
        if e["type"] in ("tool.started", "tool.result")
    ), "子 agent 的 search_notes 事件泄漏到主 SSE 流——噪声隔离退化"


# ---------- 4. 路由降级：无 router → fail-open → 正常完成 ----------


def test_smoke_route_degradation_no_router():
    """Agent.router=None（无 JEV_API_KEY）→ run 正常完成（fail-open）。

    回归目标：M10 三态生命周期状态 A（无 key 条件装配——assemble 里
    JEV_API_KEY 缺席 → 不挂 router → 行为与 v0.57 逐字节一致）。
    现有 test_jev 验了 ScenarioRouter 单元层，这里验 HTTP 端到端：无 router 不炸。
    """
    ctx = _make_ctx(ScriptedLLM([Message(role="assistant", content="你好")]))
    # 显式断言：工厂造的 agent 无 router（条件装配的结果）
    assert ctx.build_agent(Session()).router is None

    client = TestClient(create_app(ctx))
    run_id = client.post("/api/runs", json={"text": "你好"}).json()["run_id"]
    events = _read_events(client, run_id)
    types = [e["type"] for e in events]

    # fail-open：无 router → 原生路径 → 正常完成
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert "text.delta" in types
