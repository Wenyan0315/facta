"""ADR 111（098/D01 + 110/V01）验收：评审模式接线 + 指定仓库锚定 + 只读证据集 + 归属。

分两层：
- 机制层：直接调模块级 build_review_registry / build_review_agent（assemble
  也走这两个函数），钉「锚点指向被评审仓库」「只读菜单」「项目决定只在自仓注入」。
- 接线层：最小 AppContext + 记录用工厂，钉「mode/repo_root 分派」「评审跳固化」
  「/review 页路由」。

对应 ADR 111「待验收」第 1–3 条：确实读取所选仓库（而非 Facta 本仓）、
工具清单只含只读证据集且不写盘/不固化、项目决定按归属注入。
"""

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facta.core.audit import AuditLog
from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import (
    AppContext,
    build_review_agent,
    build_review_registry,
    ensure_persona,
)
from facta.paths import WORKSPACE_ROOT
from facta.review import REVIEW_SYSTEM_PROMPT, REVIEW_TOOLS
from facta.server.app import create_app
from facta.server.run_store import RunStore
from facta.tools.context import ToolContext
from facta.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    """隔离真实磁盘状态（照 tests/test_app.py 的同款纪律）。

    跑 Run 的测试真落盘；settle 尾部的 sleep-time 整理会读 user.md——
    不隔离会摸真 home 的隐私文件。评审测试还会用 tmp 的 learned 目录。
    """
    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))


def _make_ctx(reply: str = "收到") -> tuple[AppContext, dict]:
    """最小 AppContext + 记录两工厂调用（chat / review 各记一条）。"""
    calls: dict = {"chat": [], "review": []}

    def build_agent(session: Session) -> Agent:
        calls["chat"].append(session)
        agent = Agent(name="test", system_prompt="测试人设", registry=ToolRegistry())
        ensure_persona(session, agent)
        return agent

    def build_review(session: Session, repo_root: Path) -> Agent:
        calls["review"].append((session, repo_root))
        agent = Agent(name="review", system_prompt=REVIEW_SYSTEM_PROMPT, registry=ToolRegistry())
        ensure_persona(session, agent)
        return agent

    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content=reply)]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=build_agent,
        build_review_agent=build_review,
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
    )
    return ctx, calls


def _review_registry(repo_root: Path, tmp_path: Path):
    ctx = ToolContext(notes_dir=tmp_path / "notes")
    return build_review_registry(ctx, AuditLog(tmp_path / "audit"), repo_root)


def _read_events(client, run_id: str) -> list[dict]:
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


# ---------- 机制层：锚定与只读证据集 ----------


def test_review_registry_reads_the_selected_repo_not_facta(tmp_path):
    # ADR 110/V01 硬要求：确实读取【所选仓库】而非 Facta 本仓。文件锚点
    # 复用 S6a 注入缝（ctx.workspace_root=repo_root）——读同名相对路径
    # 拿到的是外部仓内容；Facta 才有的文件在评审仓里读不到。
    repo = tmp_path / "external"
    repo.mkdir()
    (repo / "marker.txt").write_text("EXTERNAL_REPO_MARKER\n", encoding="utf-8")
    reg = _review_registry(repo, tmp_path)

    out = reg.execute("read_file", json.dumps({"path": "marker.txt"}))
    assert "EXTERNAL_REPO_MARKER" in out

    # pyproject.toml 只在 Facta 本仓有 → 证明锚点不在 Facta，读取范围被限制
    assert "文件不存在" in reg.execute("read_file", json.dumps({"path": "pyproject.toml"}))


def test_review_registry_fence_still_blocks_escape(tmp_path):
    # 不放宽全局权限：围栏相对 repo_root 判定，越界照旧被挡。
    repo = tmp_path / "external"
    repo.mkdir()
    (tmp_path / "outside.txt").write_text("OUTSIDE\n", encoding="utf-8")
    reg = _review_registry(repo, tmp_path)

    assert "越界" in reg.execute("read_file", json.dumps({"path": "../outside.txt"}))


def test_review_agent_menu_is_readonly_evidence_set(tmp_path):
    # 只读证据集是【结构性】事实：allowed_tools 过滤掉 write/命令/派生。
    repo = tmp_path / "external"
    repo.mkdir()
    reg = _review_registry(repo, tmp_path)
    assert "write_file" in reg.names()   # registry 里本有写工具……

    agent = build_review_agent(reg, Session(), repo, learned_dir=None)
    names = {s["function"]["name"] for s in agent.schemas()}

    assert agent.allowed_tools == REVIEW_TOOLS
    assert names == set(REVIEW_TOOLS) & set(reg.names())
    for banned in ("write_file", "write_note", "run_command", "spawn_subagent"):
        assert banned not in names   # ……但菜单里结构性不可见

    # 菜单外拦截 + 不落盘：点写文件被拒，仓库里没有新文件
    out = agent.execute("write_file", json.dumps({"path": "x.txt", "content": "hi"}))
    assert "不在当前 agent 的工具清单" in out
    assert not (repo / "x.txt").exists()
    assert agent.router is None   # 评审不挂场景路由（保确定性）


def test_review_agent_injects_project_decisions_only_for_own_repo(tmp_path):
    # 记忆归属（110 §4 / ADR 111 §3）：learned 三桶只在 repo_root == 自仓
    # 时注入；评审外部仓不注入 Facta 的决定。用户级记忆恒不注入。
    learned = tmp_path / "learned"
    learned.mkdir()
    (learned / "decisions.md").write_text("2026-10-01 [决定] 只在自仓注入\n", encoding="utf-8")
    reg = _review_registry(WORKSPACE_ROOT, tmp_path)

    own = build_review_agent(reg, Session(), WORKSPACE_ROOT, learned_dir=learned)
    assert "[决定]" in own.system_prompt

    external = build_review_agent(reg, Session(), tmp_path, learned_dir=learned)
    assert "[决定]" not in external.system_prompt
    # 外部仓：prompt 恰为人设本身（无 learned、无用户记忆注入）
    assert external.system_prompt == REVIEW_SYSTEM_PROMPT


def test_review_agent_seeds_persona_into_session(tmp_path):
    repo = tmp_path / "external"
    repo.mkdir()
    s = Session()
    agent = build_review_agent(_review_registry(repo, tmp_path), s, repo, learned_dir=None)

    assert s.messages and s.messages[0].role == "system"
    assert s.messages[0].content == agent.system_prompt == REVIEW_SYSTEM_PROMPT


# ---------- 接线层：mode/repo_root 分派 + 跳固化 + 页面 ----------


def test_review_page_serves_html():
    client = TestClient(create_app(_make_ctx()[0]))
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "方案评审" in resp.text


def test_review_repo_missing_is_400_before_worker_starts(tmp_path):
    # repo_root 是外部输入：存在且为目录才放行；非法在起线程前就 400。
    ctx, calls = _make_ctx()
    client = TestClient(create_app(ctx))

    resp = client.post(
        "/api/runs/sync",
        json={"text": "材料", "mode": "review", "repo_root": str(tmp_path / "nope")},
    )
    assert resp.status_code == 400
    assert calls["review"] == []   # 校验先于起线程，没跑到 worker


def test_chat_mode_ignores_repo_root():
    # 缺省 mode=chat ⇒ 全部既有调用方逐字节不受影响：repo_root 不参与校验。
    ctx, calls = _make_ctx()
    client = TestClient(create_app(ctx))
    resp = client.post("/api/runs/sync", json={"text": "你好", "repo_root": str(Path("/no/such/dir"))})

    assert resp.status_code == 200
    assert calls["chat"] != [] and calls["review"] == []


def test_review_mode_dispatches_review_agent_with_resolved_repo_root(tmp_path):
    repo = tmp_path / "external"
    repo.mkdir()
    ctx, calls = _make_ctx()
    client = TestClient(create_app(ctx))

    resp = client.post(
        "/api/runs/sync",
        json={"text": "材料", "mode": "review", "repo_root": str(repo)},
    )
    assert resp.status_code == 200
    assert calls["chat"] == []   # 评审走 review 工厂，不走 chat 工厂
    assert len(calls["review"]) == 1
    _session, root = calls["review"][0]
    assert root == repo.resolve()


def test_review_skips_settle_but_still_persists(monkeypatch):
    # ADR 111 §3：评审不固化记忆（模型意见不自动升级为项目决定），只保底落盘。
    settled: list[str] = []
    monkeypatch.setattr("facta.server.app.settle_session", lambda *a, **k: settled.append("settle"))
    ctx, _calls = _make_ctx()
    client = TestClient(create_app(ctx))

    # 评审一轮：不触发 settle，但对话本体已保底落盘、标题就地补。
    body = client.post("/api/runs/sync", json={"text": "材料", "mode": "review"}).json()
    assert settled == []
    saved = ctx.store.load(body["session_id"])
    assert saved.title is not None
    assert any(m.role == "user" for m in saved.messages)

    # 对照：chat 模式照常走 settle（证明上面的空是「跳过」而非「从没调用」）
    client.post("/api/runs/sync", json={"text": "你好"})
    assert settled == ["settle"]


def test_review_without_factory_reports_error_event():
    # 装配缺失（最小 ctx 无 build_review_agent）是编程错误：报 error 事件，
    # 不静默降级成 chat（否则评审会悄悄跑成普通对话）。
    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="x")]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=lambda s: Agent(name="t", system_prompt="s", registry=ToolRegistry()),
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
    )
    rs = RunStore()
    client = TestClient(create_app(ctx, store=rs))
    body = client.post("/api/runs", json={"text": "材料", "mode": "review"}).json()
    events = _read_events(client, body["run_id"])

    errors = [e for e in events if e["type"] == "error"]
    assert errors and "build_review_agent" in json.dumps(errors[0])
