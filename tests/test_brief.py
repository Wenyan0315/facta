"""ADR 116 §1 验收（Brief 技能）：brief.md 属性钉 + /brief 页 + mode 分派泛化 +
钉子「brief + 外部已登记仓 → 文件锚点与 learned 都指向该项目」。

Brief 是技能数据化后新增的第一个非评审技能（113 §2 六节契约）：证明零代码
加技能（新增 .md 即新增 mode）与项目归属注入在真实技能上成立。
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
from facta.orchestrator.assemble import AppContext, ensure_persona
from facta.paths import SKILLS_DIR
from facta.projects import ProjectsManifest, resolve_learned_dir
from facta.server.app import create_app
from facta.skills import SKILL_TOOLS, build_skill_agent, build_skill_registry, load_skill
from facta.tools.context import ToolContext
from facta.tools.registry import ToolRegistry

# 技能文件即真值源：人设/工具/预算/settle 都在 data/skills/brief.md 里
SPEC = load_skill(SKILLS_DIR / "brief.md")


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    """隔离真实磁盘状态（照 tests/test_app.py 的同款纪律）。"""
    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.projects.PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))


def _make_ctx() -> tuple[AppContext, dict]:
    calls: dict = {"skill": []}

    def build_agent(session: Session, repo_root: Path | None = None) -> Agent:
        agent = Agent(name="test", system_prompt="测试人设", registry=ToolRegistry())
        ensure_persona(session, agent)
        return agent

    def build_skill(session: Session, name: str, repo_root: Path, authorization: str | None = None) -> Agent:
        calls["skill"].append((session, name, repo_root))
        agent = Agent(name=name, system_prompt=SPEC.system_prompt, registry=ToolRegistry())
        ensure_persona(session, agent)
        return agent

    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="Brief 草稿")]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=build_agent,
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
        skills={"brief": SPEC},
        build_skill_agent=build_skill,
    )
    return ctx, calls


# ---------- brief.md 属性钉（113 §2 六节契约 + 补强三条）----------


def test_brief_skill_file_pins_contract():
    assert SPEC.name == "brief"
    # 六节契约逐字在人设里（①…⑥），补强三条：过时判定/外部仓来源/预算尽收尾
    for marker in (
        "①任务目标复述", "②相关历史决定", "③仓库现状",
        "④约束与纪律", "⑤验收建议", "⑥待用户补充",
        "可能已被", "未找到相关决定",   # 过时判定标注 + 不编造
        "docs/decisions/",             # 外部仓历史决定来源
        "待用户补充",                  # 预算尽后进待补充
        "事实与推测",                  # 事实与推测分开
        "注入免疫",
    ):
        assert marker in SPEC.system_prompt
    assert SPEC.tools == SKILL_TOOLS   # 显式声明恰 8 件只读（与 registry 全集同集）
    assert SPEC.max_tool_rounds == 12
    assert SPEC.skip_settle is True   # Brief 是草稿，不自动升级为项目决定


# ---------- 接线：/brief 页 + mode 分派泛化 ----------


def test_brief_page_serves_html():
    client = TestClient(create_app(_make_ctx()[0]))
    resp = client.get("/brief")
    assert resp.status_code == 200
    assert "开工 Brief" in resp.text


def test_brief_mode_dispatches_skill_agent_with_resolved_repo_root(tmp_path):
    # mode 分派泛化：新技能零 py 改动走同一条技能工厂通道
    repo = tmp_path / "external"
    repo.mkdir()
    ctx, calls = _make_ctx()
    client = TestClient(create_app(ctx))

    resp = client.post(
        "/api/runs/sync",
        json={"text": "任务意图", "mode": "brief", "repo_root": str(repo)},
    )
    assert resp.status_code == 200
    assert len(calls["skill"]) == 1
    _session, name, root = calls["skill"][0]
    assert name == "brief"
    assert root == repo.resolve()


# ---------- 钉子：brief + 外部已登记仓 → 文件锚点与 learned 都指向该项目 ----------


def test_brief_registered_project_pins_file_and_learned_anchors(tmp_path):
    repo = tmp_path / "proj"
    (repo / "docs").mkdir(parents=True)
    (repo / "marker.txt").write_text("PROJECT_MARKER\n", encoding="utf-8")

    entry = ProjectsManifest().register("proj", str(repo))
    learned = Path(entry["learned_path"])
    (learned / "decisions.md").write_text("2026-11-01 [决定] 项目专属决定\n", encoding="utf-8")

    # 归属：登记命中 → 项目 learned 目录
    resolved = resolve_learned_dir(repo, own_dir=tmp_path / "own")
    assert resolved == learned

    # 文件锚点：read_file 读到的是【项目仓】的文件
    reg = build_skill_registry(
        ToolContext(notes_dir=tmp_path / "notes"), AuditLog(tmp_path / "audit"), repo
    )
    assert "PROJECT_MARKER" in reg.execute("read_file", json.dumps({"path": "marker.txt"}))

    # learned 注入：prompt 带项目决定（②相关历史决定的来源）
    agent = build_skill_agent(SPEC, reg, Session(), learned_dir=resolved)
    assert "项目专属决定" in agent.system_prompt
