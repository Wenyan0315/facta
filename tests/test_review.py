"""ADR 116 §1 验收（111 评审等价点重锚到技能机制）：技能分派 + 只读 registry + 归属。

分两层：
- 机制层：直接调 skills.py 的 build_skill_registry / build_skill_agent（assemble
  也走这两个函数），钉「锚点指向目标仓」「写工具结构性缺席」「人设=技能文件」。
- 接线层：最小 AppContext + 记录用工厂，钉「mode 分派」「settle 节=skip 跳固化」
  「/review 页路由」「skip_settle 异常路径」。

review 的人设/工具集/预算已迁入 data/skills/review.md——本文件重锚**技能文件**：
REVIEW_SYSTEM_PROMPT/REVIEW_TOOLS 的等价断言改钉 spec（load_skill 的解析产物）。
对应 ADR 116「既有路径不回归」+ 评审补列钉子测试。
"""

import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facta.core.audit import AuditLog
from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext, ensure_persona
from facta.paths import SKILLS_DIR, WORKSPACE_ROOT
from facta.server.app import create_app
from facta.server.run_store import RunStore
from facta.skills import SKILL_TOOLS, build_skill_agent, build_skill_registry, load_skill
from facta.tools.context import ToolContext
from facta.tools.registry import ToolRegistry

# 技能文件即真值源（ADR 116「评审重锚技能文件」）：人设/工具/预算都在文件里
SPEC = load_skill(SKILLS_DIR / "review.md")


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    """隔离真实磁盘状态（照 tests/test_app.py 的同款纪律）。

    跑 Run 的测试真落盘；settle 尾部的 sleep-time 整理会读 user.md——
    不隔离会摸真 home 的隐私文件。评审测试还会用 tmp 的 learned 目录。
    """
    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.projects.PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))


def _make_ctx(reply: str = "收到") -> tuple[AppContext, dict]:
    """最小 AppContext + 记录两工厂调用（chat / skill 各记一条）。"""
    calls: dict = {"chat": [], "skill": []}

    def build_agent(session: Session, repo_root: Path | None = None) -> Agent:
        calls["chat"].append((session, repo_root))
        agent = Agent(name="test", system_prompt="测试人设", registry=ToolRegistry())
        ensure_persona(session, agent)
        return agent

    def build_skill(session: Session, name: str, repo_root: Path) -> Agent:
        calls["skill"].append((session, name, repo_root))
        agent = Agent(name=name, system_prompt=SPEC.system_prompt, registry=ToolRegistry())
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
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
        skills={"review": SPEC},
        build_skill_agent=build_skill,
    )
    return ctx, calls


def _skill_registry(repo_root: Path, tmp_path: Path):
    ctx = ToolContext(notes_dir=tmp_path / "notes")
    return build_skill_registry(ctx, AuditLog(tmp_path / "audit"), repo_root)


def _read_events(client, run_id: str) -> list[dict]:
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


# ---------- 技能文件（review.md）等价钉：迁出 review.py 不丢语义 ----------


def test_review_skill_file_pins_migrated_constants():
    # REVIEW_SYSTEM_PROMPT/REVIEW_TOOLS/预算 12 的等价点：改文件里的值这里会红
    assert SPEC.name == "review"
    assert "只评审、不执行" in SPEC.system_prompt
    assert SPEC.tools == SKILL_TOOLS == frozenset({
        "get_current_time", "list_notes", "read_notes", "search_notes",
        "search_and_summarize", "read_file", "search_code", "list_dir",
    })
    assert SPEC.max_tool_rounds == 12   # ADR 112 证据预算
    assert SPEC.skip_settle is True     # settle 节=skip：评审产出不固化


# ---------- 机制层：锚定与只读证据集 ----------


def test_review_registry_reads_the_selected_repo_not_facta(tmp_path):
    # ADR 110/V01 硬要求：确实读取【所选仓库】而非 Facta 本仓。文件锚点
    # 复用 S6a 注入缝（ctx.workspace_root=repo_root）——读同名相对路径
    # 拿到的是外部仓内容；Facta 才有的文件在评审仓里读不到。
    repo = tmp_path / "external"
    repo.mkdir()
    (repo / "marker.txt").write_text("EXTERNAL_REPO_MARKER\n", encoding="utf-8")
    reg = _skill_registry(repo, tmp_path)

    out = reg.execute("read_file", json.dumps({"path": "marker.txt"}))
    assert "EXTERNAL_REPO_MARKER" in out

    # pyproject.toml 只在 Facta 本仓有 → 证明锚点不在 Facta，读取范围被限制
    assert "文件不存在" in reg.execute("read_file", json.dumps({"path": "pyproject.toml"}))


def test_review_registry_fence_still_blocks_escape(tmp_path):
    # 不放宽全局权限：围栏相对 repo_root 判定，越界照旧被挡。
    repo = tmp_path / "external"
    repo.mkdir()
    (tmp_path / "outside.txt").write_text("OUTSIDE\n", encoding="utf-8")
    reg = _skill_registry(repo, tmp_path)

    assert "越界" in reg.execute("read_file", json.dumps({"path": "../outside.txt"}))


def test_skill_registry_write_tools_are_structurally_absent(tmp_path):
    # ADR 116 §1：技能 registry 只注册只读 8 件——write_note/write_file 不是
    # 「被过滤掉」而是根本不在注册表（review「不改代码不跑命令」同一事实）。
    repo = tmp_path / "external"
    repo.mkdir()
    reg = _skill_registry(repo, tmp_path)

    assert set(reg.names()) <= set(SKILL_TOOLS)
    for banned in ("write_file", "write_note", "run_command", "spawn_subagent", "terminal"):
        assert banned not in reg.names()


def test_review_agent_menu_is_readonly_evidence_set(tmp_path):
    repo = tmp_path / "external"
    repo.mkdir()
    reg = _skill_registry(repo, tmp_path)

    agent = build_skill_agent(SPEC, reg, Session(), learned_dir=None)
    names = {s["function"]["name"] for s in agent.schemas()}

    assert agent.allowed_tools == SPEC.tools == SKILL_TOOLS
    assert names == set(SKILL_TOOLS) & set(reg.names())
    for banned in ("write_file", "write_note", "run_command", "spawn_subagent"):
        assert banned not in names   # 菜单里结构性不可见

    # 菜单外拦截 + 不落盘：点写文件被拒，仓库里没有新文件
    out = agent.execute("write_file", json.dumps({"path": "x.txt", "content": "hi"}))
    assert "不在当前 agent 的工具清单" in out
    assert not (repo / "x.txt").exists()
    assert agent.router is None   # router=None 钉死（116 评审问题 8：保确定性）

    # ADR 112：评审证据预算住技能文件 budget 节（12），解析进 agent
    assert agent.max_tool_rounds == SPEC.max_tool_rounds == 12


def test_review_agent_injects_learned_only_when_given(tmp_path):
    # 归属注入的机制面：build_skill_agent 只吃 resolve_learned_dir 的结果——
    # None=不注入（外部未登记仓）；有目录=注入（本仓/已登记项目）。
    learned = tmp_path / "learned"
    learned.mkdir()
    (learned / "decisions.md").write_text("2026-10-01 [决定] 只在自仓注入\n", encoding="utf-8")
    reg = _skill_registry(WORKSPACE_ROOT, tmp_path)

    own = build_skill_agent(SPEC, reg, Session(), learned_dir=learned)
    assert "[决定]" in own.system_prompt

    external = build_skill_agent(SPEC, reg, Session(), learned_dir=None)
    assert "[决定]" not in external.system_prompt
    # 不注入：prompt 恰为技能文件人设本身（无 learned、无用户记忆注入）
    assert external.system_prompt == SPEC.system_prompt


def test_review_agent_seeds_persona_into_session(tmp_path):
    repo = tmp_path / "external"
    repo.mkdir()
    s = Session()
    agent = build_skill_agent(SPEC, _skill_registry(repo, tmp_path), s, learned_dir=None)

    assert s.messages and s.messages[0].role == "system"
    assert s.messages[0].content == agent.system_prompt == SPEC.system_prompt


# ---------- 接线层：mode 分派 + 跳固化 + 页面 ----------


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
    assert calls["skill"] == []   # 校验先于起线程，没跑到 worker


def test_chat_mode_ignores_repo_root():
    # 缺省 mode=chat ⇒ 全部既有调用方逐字节不受影响：repo_root 不参与校验
    # 也不参与归属（恒 None → 本仓全局）。
    ctx, calls = _make_ctx()
    client = TestClient(create_app(ctx))
    resp = client.post("/api/runs/sync", json={"text": "你好", "repo_root": str(Path("/no/such/dir"))})

    assert resp.status_code == 200
    assert calls["chat"] != [] and calls["skill"] == []
    assert calls["chat"][0][1] is None   # 116：chat 的 repo_root 恒被忽略成 None


def test_review_mode_dispatches_skill_agent_with_resolved_repo_root(tmp_path):
    repo = tmp_path / "external"
    repo.mkdir()
    ctx, calls = _make_ctx()
    client = TestClient(create_app(ctx))

    resp = client.post(
        "/api/runs/sync",
        json={"text": "材料", "mode": "review", "repo_root": str(repo)},
    )
    assert resp.status_code == 200
    assert calls["chat"] == []   # 评审走技能工厂，不走 chat 工厂
    assert len(calls["skill"]) == 1
    _session, name, root = calls["skill"][0]
    assert name == "review"
    assert root == repo.resolve()


def test_review_skips_settle_but_still_persists(monkeypatch):
    # 评审（settle 节=skip）不固化记忆（模型意见不自动升级为项目决定），
    # 只保底落盘——判定走 skip_settle 属性（读 review.md），不是按名字硬编码。
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


def test_skill_factory_failure_still_finishes_run_and_sync(monkeypatch):
    # ADR 116 评审问题 3（异常路径）：技能构造在 try 内抛异常时，skip_settle
    # 是预计算布尔（不是 agent.skip_settle）——finally 不 NameError，run 正常
    # finish、sync 不挂死、错误如实可见。
    settled: list[str] = []
    monkeypatch.setattr("facta.server.app.settle_session", lambda *a, **k: settled.append("settle"))
    ctx, _calls = _make_ctx()

    def broken_factory(session, name, repo_root):
        raise RuntimeError("技能构造炸了")

    ctx = replace(ctx, build_skill_agent=broken_factory)
    rs = RunStore()
    client = TestClient(create_app(ctx, store=rs))
    body = client.post("/api/runs", json={"text": "材料", "mode": "review"}).json()
    events = _read_events(client, body["run_id"])

    # run 仍落终态（构造异常 → failed 终态，不挂死不悬空）
    assert any(e["type"] in ("run.completed", "run.failed", "run.cancelled") for e in events)
    # 错误如实可见（json.dumps 缺省 ASCII 转义中文，须 ensure_ascii=False 比对）
    assert any("技能构造炸了" in json.dumps(e, ensure_ascii=False) for e in events)
    assert settled == []   # settle 节=skip 照旧跳过（预计算，不依赖 agent）


def test_review_without_factory_reports_error_event():
    # 装配缺失（最小 ctx 无 build_skill_agent）是编程错误：报 error 事件，
    # 不静默降级成 chat（否则技能会悄悄跑成普通对话）。
    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="x")]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=lambda s, repo_root=None: Agent(name="t", system_prompt="s", registry=ToolRegistry()),
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
        skills={"review": SPEC},
    )
    rs = RunStore()
    client = TestClient(create_app(ctx, store=rs))
    body = client.post("/api/runs", json={"text": "材料", "mode": "review"}).json()
    events = _read_events(client, body["run_id"])

    errors = [e for e in events if e["type"] == "error"]
    assert errors and "build_skill_agent" in json.dumps(errors[0])


def test_unknown_mode_is_422():
    # ADR 116 §1：mode 扩 str 后未知值 422 在入口拦（零代码加技能的对照面：
    # 技能名来自 ctx.skills，不是任意字符串都收）
    ctx, calls = _make_ctx()
    client = TestClient(create_app(ctx))
    resp = client.post("/api/runs/sync", json={"text": "材料", "mode": "nope"})
    assert resp.status_code == 422
    assert calls["chat"] == [] and calls["skill"] == []
