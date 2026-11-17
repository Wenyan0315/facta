"""ADR 117 §3/§4 授权档位 + 任务级首写授权：结构与链路钉子。

不变量：
- readonly 结构性缺席：写/终端工具不在 registry（调不到，不是拦得住）
- write 档：写两件标 WRITE_AUTH_GUARD 确认，run_command 维持 019 动态规则
- 任务级一次性写授权：write 档技能任务首次 write_file/write_note 停 L2，
  approve → 置位 Task.authorized_write（落盘）→ 后续写免停；reject 不置位
- run_command 的 approve 不置位（只有写两件算写授权）
- 覆盖规则：缺省=技能声明档；请求只能收严（write→readonly 允许，放宽 422）
- 主 agent 任务零回归（档位不作用，本文件不重复测——test_app 既有链路即钉子）
"""

import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facta.core.audit import AuditLog
from facta.core.llm import ScriptedLLM
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent, ensure_persona
from facta.orchestrator.assemble import AppContext
from facta.server.app import create_app
from facta.skills import (
    AUTH_READONLY,
    AUTH_WRITE,
    SKILL_TOOLS,
    SKILL_WRITE_TOOLS,
    WRITE_AUTH_GUARD,
    SkillSpec,
    build_skill_agent,
    build_skill_registry,
    load_skill,
)
from facta.tools.context import ToolContext
from facta.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))


# ---------- 技能文件 ## authorization 节（装配期校验） ----------


def _write_skill(tmp_path: Path, stem: str, body: str) -> Path:
    path = tmp_path / f"{stem}.md"
    path.write_text(f"# {stem}\n\n{body}\n", encoding="utf-8")
    return path


def test_load_skill_authorization_parsing(tmp_path):
    assert load_skill(_write_skill(tmp_path, "a", "人设。")).authorization == AUTH_READONLY   # 缺省
    spec = load_skill(_write_skill(tmp_path, "b", "人设。\n\n## authorization\nwrite"))
    assert spec.authorization == AUTH_WRITE


def test_load_skill_unknown_authorization_raises(tmp_path):
    path = _write_skill(tmp_path, "c", "人设。\n\n## authorization\nadmin")
    with pytest.raises(ValueError, match="authorization"):
        load_skill(path)


def test_load_skill_tools_validation_by_tier(tmp_path):
    # readonly 档声明写工具 = 配置错误，装配期拦（不静默少给）
    path = _write_skill(tmp_path, "d", "人设。\n\n## tools\nread_file\nwrite_file")
    with pytest.raises(ValueError, match="write_file"):
        load_skill(path)
    # write 档声明写工具合法
    spec = load_skill(_write_skill(
        tmp_path, "e", "人设。\n\n## authorization\nwrite\n\n## tools\nread_file\nwrite_file\nrun_command",
    ))
    assert spec.tools == frozenset({"read_file", "write_file", "run_command"})


# ---------- registry 两档结构 ----------


def _registry(tmp_path: Path, tier: str) -> ToolRegistry:
    ctx = ToolContext(notes_dir=tmp_path / "notes", workspace_root=tmp_path)
    return build_skill_registry(ctx, AuditLog(tmp_path / "audit"), tmp_path, tier)


def test_registry_readonly_structural_absence(tmp_path):
    reg = _registry(tmp_path, AUTH_READONLY)
    names = set(reg.names())
    assert not (names & SKILL_WRITE_TOOLS)          # 写/终端结构上缺席（调不到）
    assert "read_file" in names and "read_notes" in names


def test_registry_write_tier_guards(tmp_path):
    reg = _registry(tmp_path, AUTH_WRITE)
    assert set(reg.names()) >= SKILL_WRITE_TOOLS
    # 写两件标 WRITE_AUTH_GUARD（任务级首写授权的工具侧半边）
    for name in ("write_file", "write_note"):
        assert reg.get(name).needs_confirmation == WRITE_AUTH_GUARD
    # run_command 维持 019 动态规则（callable），不改标
    assert callable(reg.get("run_command").needs_confirmation)


def test_registry_unknown_tier_raises(tmp_path):
    with pytest.raises(ValueError, match="授权档位"):
        _registry(tmp_path, "admin")


# ---------- 全链路：写首停 → approve → 置位 → 免停 ----------


def _skill_ctx(tmp_path: Path, spec: SkillSpec, llm: ScriptedLLM) -> AppContext:
    """带技能工厂的最小 ctx：工厂复用生产两件（build_skill_registry +
    build_skill_agent），档位收严逻辑与 assemble._skill_agent_factory 同式。"""
    mother = ToolContext(notes_dir=tmp_path / "notes", workspace_root=tmp_path)
    audit = AuditLog(tmp_path / "audit")

    def build_agent(session: Session, repo_root=None) -> Agent:
        agent = Agent(name="test", system_prompt="测试人设", registry=ToolRegistry())
        ensure_persona(session, agent)
        return agent

    def build_skill(session: Session, name: str, repo_root: Path, authorization: str | None = None) -> Agent:
        effective = authorization or spec.authorization
        registry = build_skill_registry(mother, audit, repo_root, effective)
        return build_skill_agent(
            spec if effective == spec.authorization else replace(spec, authorization=effective),
            registry, session,
        )

    return AppContext(
        provider="mock",
        ledger=UsageLedger(),
        embedder=None,
        llm=llm,
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=build_agent,
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
        skills={spec.name: spec},
        build_skill_agent=build_skill,
    )


def _write_call(cid: str, path: str) -> Message:
    return Message(role="assistant", content="", tool_calls=[
        {"id": cid, "name": "write_file", "arguments": json.dumps({"path": path, "content": "x"})},
    ])


def _run_write_task(tmp_path: Path, llm: ScriptedLLM, *, approves: list[bool]) -> dict:
    """跑一个 write 档技能任务：按 approves 序列裁决挂起的确认，返回任务视图。"""
    spec = SkillSpec(
        name="demo", system_prompt="测试技能", authorization=AUTH_WRITE,
        tools=frozenset(SKILL_TOOLS | SKILL_WRITE_TOOLS), max_tool_rounds=5,
    )
    client = TestClient(create_app(_skill_ctx(tmp_path, spec, llm)))
    tid = client.post("/api/tasks", json={"goal": "改文件", "skill": "demo", "repo_root": str(tmp_path)}).json()["task_id"]

    run_id = None
    deadline = time.time() + 5
    for approve in approves:
        # 轮询到挂起确认出现（worker 是真线程，确认缝有毫秒级竞态）
        resp = None
        while time.time() < deadline:
            d = client.get(f"/api/tasks/{tid}").json()
            if d["run_ids"]:
                run_id = d["run_ids"][0]
            if run_id:
                resp = client.post(f"/api/runs/{run_id}/confirm", json={"approve": approve})
                if resp.status_code == 200:
                    break
            time.sleep(0.02)
        assert resp is not None and resp.status_code == 200, f"确认未挂起（approve={approve}）"

    while time.time() < deadline:
        d = client.get(f"/api/tasks/{tid}").json()
        if d["status"] in ("done", "failed", "cancelled", "blocked", "waiting-steer"):
            return d
        time.sleep(0.02)
    raise AssertionError(f"任务未落终态：{d['status']}")


def test_write_first_stop_approve_then_free(tmp_path):
    """首写停 L2 → approve 置位（落盘）→ 第二写免停直过（钉子②核心链）。"""
    llm = ScriptedLLM([
        _write_call("c1", "a.txt"),
        _write_call("c2", "b.txt"),
        Message(role="assistant", content="写完了"),
    ])
    done = _run_write_task(tmp_path, llm, approves=[True])

    assert done["status"] == "done"
    assert done["authorization"] == AUTH_WRITE          # 缺省=技能声明档
    assert done["authorized_write"] is True             # approve 置位（TaskStore 落盘）
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_write_reject_not_authorized(tmp_path):
    """reject 不置位：本次写被拒（文件不落地），下次写再停。"""
    llm = ScriptedLLM([
        _write_call("c1", "pwned.txt"),
        Message(role="assistant", content="好的，不写了"),
    ])
    done = _run_write_task(tmp_path, llm, approves=[False])

    assert done["status"] == "done"
    assert done["authorized_write"] is False
    assert not (tmp_path / "pwned.txt").exists()


def test_run_command_approve_does_not_set_write_auth(tmp_path):
    """run_command 确认（019 动态规则）零改动：approve 不置位写授权。"""
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "run_command", "arguments": json.dumps({"command": "touch t.txt"})},
        ]),
        Message(role="assistant", content="跑完了"),
    ])
    done = _run_write_task(tmp_path, llm, approves=[True])

    assert done["status"] == "done"
    assert done["authorized_write"] is False             # 只有写两件算写授权
    assert (tmp_path / "t.txt").exists()                 # 命令照常执行


# ---------- 覆盖规则：缺省=声明档，请求只收严 ----------


def test_task_authorization_override_rules(tmp_path, monkeypatch):
    write_spec = SkillSpec(name="demo", system_prompt="写技能", authorization=AUTH_WRITE)
    read_spec = SkillSpec(name="lite", system_prompt="读技能", authorization=AUTH_READONLY)
    llm = ScriptedLLM([Message(role="assistant", content="好")])
    ctx = replace(_skill_ctx(tmp_path, write_spec, llm), skills={"demo": write_spec, "lite": read_spec})
    monkeypatch.setattr("facta.server.app._task_worker", lambda *a: None)   # 只验建档口径，不起真 worker
    client = TestClient(create_app(ctx))

    # 缺省 = 技能声明档（文件是声明，请求是约束）
    tid = client.post("/api/tasks", json={"goal": "g", "skill": "demo"}).json()["task_id"]
    assert client.get(f"/api/tasks/{tid}").json()["authorization"] == AUTH_WRITE
    # 收严 write→readonly 允许
    tid = client.post("/api/tasks", json={"goal": "g", "skill": "demo", "authorization": "readonly"}).json()["task_id"]
    assert client.get(f"/api/tasks/{tid}").json()["authorization"] == AUTH_READONLY
    # 放宽 readonly→write 拒 422（真值源唯一：放宽须改技能文件）
    assert client.post("/api/tasks", json={"goal": "g", "skill": "lite", "authorization": "write"}).status_code == 422
    # 无 skill 带 authorization = 400；未知档/未知 skill = 422
    assert client.post("/api/tasks", json={"goal": "g", "authorization": "write"}).status_code == 400
    assert client.post("/api/tasks", json={"goal": "g", "skill": "demo", "authorization": "admin"}).status_code == 422
    assert client.post("/api/tasks", json={"goal": "g", "skill": "nope"}).status_code == 422
