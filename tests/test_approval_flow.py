"""P1 审批恢复与任务推送：裁决端点/恢复语义的集成层（spec 测试矩阵 A1-A8）。

不变量（真值源 docs/p1-spec-approval-recovery.md §二/§五）：
- 审批项经真实确认流落盘（data/approvals.json 级快照），重建 app 后可查可裁决；
- 裁决按审批项 id 幂等：重复 POST 返回原裁决不重复执行；已决项带异参 → 409
  明确拒绝（指明与原项差异）；编辑参数/目标 → 原项 superseded、新项 pending
  链回原项重新走裁决，批准后按**新参数**执行；
- 旧布尔端点 POST /api/runs/{id}/confirm 映射到该 Run 当前 pending 项（test_app/
  test_authorization 的旧语义回归由那两份文件钉，这里钉映射面）；
- 审批挂起不占全局执行槽（waiting_approval 不计入上限），同会话互斥条不变；
- 重启恢复：未决项重新呈现等待裁决（同参再现复用原项）、已决已执行项再现自动
  套用原裁决原结果（不重新弹窗、不重跑）、已批准但结果未知 → unknown_result
  「结果未知，需核验」不自动执行（走核验重新审）。

单测层（ApprovalStore 原语）在 tests/test_approvals.py；本文件全走 TestClient。
"""

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from facta.core.llm import ScriptedLLM
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext, ensure_persona
from facta.orchestrator.task import TASK_DONE, TaskStore
from facta.server.app import create_app
from facta.server.run_store import ApprovalStore, RunStore
from facta.tools.registry import Tool, ToolRegistry

# ---------- 装配与剧本助手 ----------


def _probe_registry(calls: list[dict]) -> ToolRegistry:
    """带确认闸门的探针工具：每次执行收到的参数记进 calls。

    A 系列的「不重跑 / 编辑生效参数」判据全靠它——比文件副作用更直接，
    也不必关心任务沙箱/worktree 的落点。
    """
    registry = ToolRegistry()

    def probe_op(cmd: str = "") -> str:
        calls.append({"cmd": cmd})
        return f"probe:{cmd}"

    registry.register(Tool(
        name="probe_op",
        description="测试探针工具：确认闸门必停，执行参数可断言",
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
        func=probe_op,
        needs_confirmation=True,
    ))
    return registry


def _probe_llm(*cmds: str) -> ScriptedLLM:
    """剧本：逐个点菜 probe_op(cmd=...)，收尾一句纯文本。"""
    script = [
        Message(role="assistant", content="", tool_calls=[
            {"id": f"c{i}", "name": "probe_op", "arguments": json.dumps({"cmd": cmd})},
        ])
        for i, cmd in enumerate(cmds)
    ]
    script.append(Message(role="assistant", content="完成"))
    return ScriptedLLM(script)


def _make_ctx(llm: ScriptedLLM, sessions: Path, registry: ToolRegistry) -> AppContext:
    """最小 AppContext（test_task_feed 同款形状）；sessions 目录显式传入——
    「重启」场景两份 ctx 共享同一会话仓库（真重启同盘）。"""

    def build_agent(session: Session, repo_root=None) -> Agent:
        agent = Agent(name="test", system_prompt="测试人设", registry=registry)
        ensure_persona(session, agent)
        return agent

    return AppContext(
        provider="mock",
        ledger=UsageLedger(),
        embedder=None,
        llm=llm,
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(sessions),
        build_agent=build_agent,
        todos=TodoStore(sessions.parent / "todos.json"),
    )


def _seed_leftover(
    ctx: AppContext,
    cmd: str = "a",
    *,
    approved: bool | None = None,
    executed: bool = False,
    result: str = "",
) -> tuple[str, object]:
    """崩溃残留现场：任务快照 + 审批项直接落盘（不经在飞 worker），返回 (task_id, item)。

    与 create_app 的落盘根同源（TaskStore().root 与其 parent 推导，conftest 的
    TASKS_DIR 隔离闸同一把钥匙）——重启后 app 从同一快照加载。
    """
    tasks = TaskStore()
    sid = ctx.store.create(Session())
    task = tasks.create("继续干", session_id=sid, next_text=f"请调用 probe_op cmd={cmd}")
    store = ApprovalStore(tasks.root.parent / "approvals.json")
    item = store.create(task.task_id, "crashed-run", "probe_op", {"cmd": cmd}, "")
    if approved is not None:
        got, _ = store.decide(item.id, approved)
        item = got or item
    if executed:
        store.record_result(item.id, result)
    return task.task_id, item


def _wait_task_status(client, task_id: str, statuses, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        status = client.get(f"/api/tasks/{task_id}").json()["status"]
        if status in statuses:
            return status
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内到达 {statuses}（当前 {status}）")


def _approvals_of(client, task_id: str) -> list[dict]:
    return client.get("/api/approvals", params={"task_id": task_id}).json()["approvals"]


def _wait_pending(client, task_id: str, timeout: float = 10.0) -> dict:
    """轮询查询端点到该任务出现待审项（worker 到确认挂起点有毫秒级竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = [it for it in _approvals_of(client, task_id) if it["status"] == "pending"]
        if pending:
            return pending[-1]
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未出现待审审批项")


def _decide(client, approval_id: str, approved: bool, **extra):
    return client.post(
        f"/api/approvals/{approval_id}/decision", json={"approved": approved, **extra}
    )


# ---------- A1：持久化与重启存活 ----------


def test_a1_pending_item_persisted_and_survives_restart(tmp_path):
    # 真实确认流产生审批项 → 落快照（含 confirm.request 载荷加法键）→
    # 重建 app 后可查、可裁决（裁决经新 app 生效，挂起 worker 解挂执行）
    calls: list[dict] = []
    run_store = RunStore()
    app = create_app(
        _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls)), store=run_store
    )
    client = TestClient(app)
    task_id = client.post("/api/tasks", json={"goal": "干活"}).json()["task_id"]
    item = _wait_pending(client, task_id)

    assert item["tool"] == "probe_op" and item["arguments"] == {"cmd": "a"}
    assert item["task_id"] == task_id and item["target"] == ""   # probe 无语义目标 → 空串
    assert Path(app.state.approvals.path).is_file()   # spec 2.1：快照即真值源
    # confirm.request 载荷带加法键 approval_id/target（旧消费方忽略，兼容规则）
    run = run_store.get(item["run_id"])
    req = [e for e in run.events if e.type == "confirm.request"][0]
    assert req.data["approval_id"] == item["id"] and req.data["target"] == ""

    restarted = TestClient(create_app(   # 重启：同快照重建
        _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls)), store=run_store
    ))
    shown = [it for it in _approvals_of(restarted, task_id) if it["status"] == "pending"]
    assert [it["id"] for it in shown] == [item["id"]]   # 重启后可查

    resp = _decide(restarted, item["id"], True)          # 重启后可裁决
    assert resp.status_code == 200 and resp.json()["approval"]["status"] == "approved"
    assert _wait_task_status(restarted, task_id, {TASK_DONE}) == TASK_DONE   # 裁决生效
    cur = _approvals_of(restarted, task_id)[0]
    assert cur["executed"] is True and cur["result"].startswith("probe:a")   # 结果回填


# ---------- A2：重复裁决幂等 ----------


def test_a2_repeat_decision_returns_original_verdict(tmp_path):
    calls: list[dict] = []
    app = create_app(
        _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls)), store=RunStore()
    )
    client = TestClient(app)
    task_id = client.post("/api/tasks", json={"goal": "干活"}).json()["task_id"]
    item = _wait_pending(client, task_id)

    first = _decide(client, item["id"], True)
    assert first.status_code == 200 and first.json()["approval"]["status"] == "approved"
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE
    assert calls == [{"cmd": "a"}]   # 执行一次

    again = _decide(client, item["id"], True)   # 重复同参 → 原裁决原样返回
    assert again.status_code == 200
    assert again.json()["message"].startswith("重复请求") and "批准" in again.json()["message"]

    flip = _decide(client, item["id"], False)   # 不一致的重复 → 首次落子为准
    assert flip.status_code == 200 and flip.json()["approval"]["approved"] is True
    assert calls == [{"cmd": "a"}]              # 重复裁决不触发二次执行
    assert _approvals_of(client, task_id)[0]["executed"] is True


# ---------- A3：异参/异目标拒绝 ----------


def test_a3_mismatched_decision_refused(tmp_path):
    calls: list[dict] = []
    app = create_app(
        _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls)), store=RunStore()
    )
    client = TestClient(app)
    task_id = client.post("/api/tasks", json={"goal": "干活"}).json()["task_id"]
    item = _wait_pending(client, task_id)
    _decide(client, item["id"], True)
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE

    resp = _decide(client, item["id"], True, arguments={"cmd": "DIFFERENT"})
    assert resp.status_code == 409   # 明确拒绝，不执行不改写
    detail = resp.json()["detail"]
    assert "不一致" in detail and "参数" in detail   # 错误串指明与原项的差异

    cur = _approvals_of(client, task_id)[0]
    assert cur["status"] == "approved" and cur["arguments"] == {"cmd": "a"}   # 原项不动
    assert calls == [{"cmd": "a"}]


# ---------- A4：编辑生成新版本，重新走裁决 ----------


def test_a4_edit_creates_new_pending_version(tmp_path):
    calls: list[dict] = []
    app = create_app(
        _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls)), store=RunStore()
    )
    client = TestClient(app)
    task_id = client.post("/api/tasks", json={"goal": "干活"}).json()["task_id"]
    item = _wait_pending(client, task_id)

    edited = _decide(client, item["id"], True, arguments={"cmd": "b"})   # 编辑并入 decision
    assert edited.status_code == 200 and "生成新的待审版本" in edited.json()["message"]
    new = edited.json()["approval"]
    assert new["id"] != item["id"] and new["status"] == "pending" and new["approved"] is None
    assert new["supersedes"] == item["id"] and new["arguments"] == {"cmd": "b"}

    items = {it["id"]: it for it in _approvals_of(client, task_id)}
    assert items[item["id"]]["status"] == "superseded"        # 原项留档不覆盖
    assert items[item["id"]]["superseded_by"] == new["id"]    # 版本链可溯

    final = _decide(client, new["id"], True)                  # 新版本重新走裁决
    assert final.status_code == 200 and final.json()["approval"]["status"] == "approved"
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE
    assert calls == [{"cmd": "b"}]   # 批准的是编辑后的参数：按新参数执行，不跑旧参数
    cur = {it["id"]: it for it in _approvals_of(client, task_id)}[new["id"]]
    assert cur["executed"] is True and cur["result"].startswith("probe:b")   # 结果留在裁决版本上


# ---------- A5：审批挂起不占执行槽 ----------


def test_a5_approval_hang_releases_global_slot(tmp_path):
    # max_in_flight=1：挂起等裁决的 Run 不占全局执行槽，另一会话 Run 照常准入；
    # 同会话互斥条不变（仍 409）
    calls: list[dict] = []
    client = TestClient(create_app(
        _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls)),
        store=RunStore(max_in_flight=1),
    ))
    task_id = client.post("/api/tasks", json={"goal": "干活"}).json()["task_id"]
    _wait_pending(client, task_id)   # 此刻该 Run 处于 waiting_approval
    sid = client.get(f"/api/tasks/{task_id}").json()["artifacts"]["session_id"]

    assert client.post("/api/runs", json={"text": "hi"}).status_code == 202   # 槽位释放
    same = client.post("/api/runs", json={"text": "hi", "session_id": sid})
    assert same.status_code == 409   # 同会话互斥条不变

    item = _wait_pending(client, task_id)
    _decide(client, item["id"], True)   # 收尾
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE


# ---------- A6：重启后未决项重新呈现，裁决生效 ----------


def test_a6_pending_reprised_on_resume_and_decides(tmp_path):
    # 崩溃残留的待审项：重启后同参操作再现 → 复用原项重新呈现（不造重复弹窗），
    # 裁决生效——挂起侧解挂真执行
    calls: list[dict] = []
    ctx = _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls))
    task_id, item = _seed_leftover(ctx)
    run_store = RunStore()
    client = TestClient(create_app(ctx, store=run_store))   # 重启

    shown = _wait_pending(client, task_id)
    assert shown["id"] == item.id                     # 重新呈现的是原项
    # 等 worker 真挂起（confirm.request 进事件流）再裁决——复用判定只认 pending，
    # 裁决先落会让再现的操作走「已决未执行」旁路（match_decided 不命中 → 新项）
    deadline = time.time() + 10
    adopted = False
    while time.time() < deadline and not adopted:
        run_ids = client.get(f"/api/tasks/{task_id}").json()["run_ids"]
        if run_ids:
            reqs = [e for e in run_store.get(run_ids[0]).events if e.type == "confirm.request"]
            if reqs:
                assert reqs[0].data["approval_id"] == item.id   # 挂起的是原项（重新呈现）
                adopted = True
                break
        time.sleep(0.05)
    assert adopted
    assert [it["id"] for it in _approvals_of(client, task_id)] == [item.id]   # 不造重复项

    resp = _decide(client, item.id, True)
    assert resp.status_code == 200
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE
    assert calls == [{"cmd": "a"}]   # 裁决生效：真执行一次
    done_items = _approvals_of(client, task_id)
    assert len(done_items) == 1      # 全程只有一项（复用原项，不造重复弹窗）
    assert done_items[0]["status"] == "approved" and done_items[0]["executed"] is True
    assert done_items[0]["result"].startswith("probe:a")   # 执行结果回填审批项


# ---------- A7：已决动作再现自动套用（不弹窗不重跑）----------


def test_a7_replay_auto_applies_and_backfills_result(tmp_path):
    # 同（tool, arguments, target）第二次出现：套用原裁决原结果——只弹一次窗、
    # 只执行一次，执行结果回填审批项
    calls: list[dict] = []
    run_store = RunStore()
    app = create_app(
        _make_ctx(_probe_llm("a", "a"), tmp_path / "sessions", _probe_registry(calls)),
        store=run_store,
    )
    client = TestClient(app)
    task_id = client.post("/api/tasks", json={"goal": "干活"}).json()["task_id"]
    item = _wait_pending(client, task_id)
    _decide(client, item["id"], True)
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE

    assert calls == [{"cmd": "a"}]                 # 第二次调用不重跑
    items = _approvals_of(client, task_id)
    assert len(items) == 1                         # 第二次不新建审批项（不重新弹窗）
    assert items[0]["executed"] is True and items[0]["result"]   # 结果回填
    run = run_store.get(items[0]["run_id"])
    results = [e.data["result"] for e in run.events if e.type == "tool.result"]
    assert results == [items[0]["result"], items[0]["result"]]   # 原结果回注，两帧一致
    assert sum(1 for e in run.events if e.type == "confirm.request") == 1


def test_a7_restart_replays_decided_action(tmp_path):
    # 重启后已决已执行项再现：自动套用原裁决原结果（不重新弹窗、不重跑）
    calls: list[dict] = []
    ctx = _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls))
    task_id, _ = _seed_leftover(ctx, approved=True, executed=True, result="orig-result")
    run_store = RunStore()
    client = TestClient(create_app(ctx, store=run_store))

    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE
    assert calls == []                             # 不重跑已完成工作
    items = _approvals_of(client, task_id)
    assert len(items) == 1 and items[0]["status"] == "approved"   # 不新建、不弹窗
    run_id = client.get(f"/api/tasks/{task_id}").json()["run_ids"][0]
    run = run_store.get(run_id)
    results = [e.data["result"] for e in run.events if e.type == "tool.result"]
    assert results == ["orig-result"]              # 原结果回注
    assert not [e for e in run.events if e.type == "confirm.request"]   # 不重新弹窗


# ---------- A8：unknown_result 核验路径 ----------


def test_a8_unknown_result_requires_verification(tmp_path):
    # 已批准但执行结果没回来的项：启动扫描标 unknown_result（「结果未知，需
    # 核验」）——不自动执行；同参再现走真挂起重新审，核验放行后才执行
    calls: list[dict] = []
    ctx = _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls))
    task_id, item = _seed_leftover(ctx, approved=True)   # approved + 未执行 + 结果空
    client = TestClient(create_app(ctx))

    items = {it["id"]: it for it in _approvals_of(client, task_id)}
    assert items[item.id]["status"] == "unknown_result"   # 启动扫描已标
    assert items[item.id]["approved"] is True
    assert items[item.id]["executed"] is False and items[item.id]["result"] == ""   # 不自动执行

    fresh = _wait_pending(client, task_id)   # 同参再现 → 真挂起（重新走审）
    assert fresh["id"] != item.id
    assert calls == []
    _decide(client, fresh["id"], True)       # 核验放行后才执行
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE
    assert calls == [{"cmd": "a"}]


# ---------- 旧布尔端点映射（spec 2.2 兼容规则）----------


def test_a_target_derived_from_tool_semantics(tmp_path):
    # spec 2.1：target 由工具语义给出——run_command→command；聊天端确认弹窗语义
    # 不变（task_id 空串、旧布尔端点照常），结果回填审批项
    from facta.tools.context import ToolContext
    from facta.tools.terminal import register_terminal_tools

    registry = ToolRegistry()
    register_terminal_tools(registry, ToolContext(notes_dir=tmp_path, workspace_root=tmp_path))
    app = create_app(_make_ctx(
        ScriptedLLM([
            Message(role="assistant", content="", tool_calls=[
                {"id": "c1", "name": "run_command",
                 "arguments": json.dumps({"command": "touch t.txt"})},
            ]),
            Message(role="assistant", content="已执行"),
        ]),
        tmp_path / "sessions", registry,
    ))
    client = TestClient(app)
    run_id = client.post("/api/runs", json={"text": "建个文件"}).json()["run_id"]

    deadline = time.time() + 10
    item = None
    while time.time() < deadline:
        items = client.get("/api/approvals", params={"run_id": run_id}).json()["approvals"]
        pending = [it for it in items if it["status"] == "pending"]
        if pending:
            item = pending[0]
            break
        time.sleep(0.05)
    assert item is not None
    assert item["target"] == "touch t.txt" and item["task_id"] == ""   # 语义目标 / 聊天无任务
    assert client.post(f"/api/runs/{run_id}/confirm", json={"approve": True}).status_code == 200
    deadline = time.time() + 10          # 批准后命令真执行（worker 毫秒级异步）
    while time.time() < deadline and not (tmp_path / "t.txt").exists():
        time.sleep(0.05)
    assert (tmp_path / "t.txt").exists()


def test_a_legacy_bool_confirm_maps_to_run_pending_item(tmp_path):
    # POST /api/runs/{run_id}/confirm（旧布尔载荷）映射到该 Run 当前 pending 项：
    # 200 落裁决；无 pending 维持现状 409
    calls: list[dict] = []
    app = create_app(
        _make_ctx(_probe_llm("a"), tmp_path / "sessions", _probe_registry(calls)), store=RunStore()
    )
    client = TestClient(app)
    task_id = client.post("/api/tasks", json={"goal": "干活"}).json()["task_id"]
    item = _wait_pending(client, task_id)

    resp = client.post(f"/api/runs/{item['run_id']}/confirm", json={"approve": True})
    assert resp.status_code == 200 and resp.json() == {"status": "approved"}
    assert _wait_task_status(client, task_id, {TASK_DONE}) == TASK_DONE
    assert calls == [{"cmd": "a"}]
    cur = _approvals_of(client, task_id)[0]
    assert cur["id"] == item["id"] and cur["status"] == "approved"   # 落在同一审批项上

    again = client.post(f"/api/runs/{item['run_id']}/confirm", json={"approve": False})
    assert again.status_code == 409   # 无 pending（已决非 pending）→ 维持现状
