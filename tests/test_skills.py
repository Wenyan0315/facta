"""ADR 116 §1 skill 数据化验收：load_skill 校验纪律 + 零代码加技能 + 装配属性传导。

校验不静默是本案纪律（build_review_agent 先例）：坏文件装配报错，不静默降级。
钉子①（零代码加技能）：新增 data/skills/*.md 即新增 mode，零 py 改动——
端到端一例在 test_zero_code_add_skill_end_to_end。
"""

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
from facta.skills import SkillSpec, build_skill_agent, load_skill, load_skills
from facta.tools.registry import ToolRegistry

VALID = "# demo\n\n你是演示助手。\n"


def _write(tmp_path: Path, stem: str, text: str) -> Path:
    p = tmp_path / f"{stem}.md"
    p.write_text(text, encoding="utf-8")
    return p


# ---------- load_skill：格式与校验纪律 ----------


def test_load_skill_defaults():
    spec = load_skill(_write(Path(tempfile.mkdtemp()), "demo", VALID))
    assert spec == SkillSpec(name="demo", system_prompt="你是演示助手。")
    assert spec.tools is None            # 缺省 = registry 全量
    assert spec.max_tool_rounds == 5     # 缺省 = Agent 默认
    assert spec.skip_settle is False     # 缺省 = 正常固化


def test_load_skill_sections():
    spec = load_skill(_write(
        Path(tempfile.mkdtemp()),
        "demo",
        "# demo\n\n人设正文。\n\n## tools\nget_current_time\nread_file\n\n## budget\n12\n\n## settle\nskip\n",
    ))
    assert spec.system_prompt == "人设正文。"
    assert spec.tools == frozenset({"get_current_time", "read_file"})
    assert spec.max_tool_rounds == 12
    assert spec.skip_settle is True


def test_load_skill_rejects_bad_files():
    """坏文件全部报 ValueError（装配报错不静默）——防拼写漂移与静默少工具。"""
    root = Path(tempfile.mkdtemp())
    cases = {
        "no_h1": "你是人设但没标题\n",
        "stem_mismatch": "# other\n\n人设\n",                       # H1 ≠ 文件名 stem
        "chat_reserved": "# chat\n\n人设\n",                        # mode=chat 是主对话通道
        "no_body": "# demo\n\n## tools\nread_file\n",
        "unknown_section": "# demo\n\n人设\n\n## tool\nread_file\n",
        "dup_section": "# demo\n\n人设\n\n## tools\nread_file\n\n## tools\nlist_dir\n",
        "tool_not_in_registry": "# demo\n\n人设\n\n## tools\nwrite_file\n",
        "tools_empty": "# demo\n\n人设\n\n## tools\n",
        "budget_nan": "# demo\n\n人设\n\n## budget\n十二\n",
        "budget_zero": "# demo\n\n人设\n\n## budget\n0\n",
        "settle_unknown": "# demo\n\n人设\n\n## settle\nalways\n",
    }
    for stem, text in cases.items():
        with pytest.raises(ValueError):
            load_skill(_write(root, stem, text))


def test_load_skills_scans_directory(tmp_path):
    _write(tmp_path, "aaa", "# aaa\n\nA\n")
    _write(tmp_path, "bbb", "# bbb\n\nB\n## budget\n9\n")
    specs = load_skills(tmp_path)
    assert sorted(specs) == ["aaa", "bbb"]
    assert specs["bbb"].max_tool_rounds == 9


def test_load_skills_rejects_broken_file_loudly(tmp_path):
    _write(tmp_path, "good", VALID)
    _write(tmp_path, "bad", "# broken\n\n人设\n\n## budget\nx\n")
    with pytest.raises(ValueError):
        load_skills(tmp_path)   # 坏一个 = 整装配报错（宁可服务起不来）


# ---------- build_skill_agent：属性传导 ----------


def test_build_skill_agent_transmits_attrs(tmp_path):
    from facta.core.audit import AuditLog
    from facta.skills import build_skill_registry
    from facta.tools.context import ToolContext

    spec = load_skill(_write(tmp_path, "demo", "# demo\n\n人设。\n## tools\nread_file\n## budget\n7\n## settle\nskip\n"))
    reg = build_skill_registry(ToolContext(notes_dir=tmp_path / "notes"), AuditLog(tmp_path / "audit"), tmp_path)
    s = Session()
    agent = build_skill_agent(spec, reg, s, learned_dir=None)

    assert agent.allowed_tools == frozenset({"read_file"})
    assert agent.max_tool_rounds == 7
    assert agent.skip_settle is True
    assert agent.router is None
    assert agent.system_prompt == "人设。"          # 无 learned/用户记忆注入
    assert s.messages[0].content == "人设。"        # 人设保证进会话


# ---------- 钉子①：零代码加技能（新增 .md 即新增 mode）----------


def test_zero_code_add_skill_end_to_end(tmp_path, monkeypatch):
    """零 py 改动：新技能文件 + mode=技能名 即跑（无页面也行，走 API）。"""
    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.projects.PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))

    skills = load_skills(tmp_path)   # 临时目录里放一个 demo.md
    _write(tmp_path, "demo", "# demo\n\n你是演示助手。\n")
    skills = load_skills(tmp_path)
    assert "demo" in skills

    calls = []

    def build_skill(session, name, repo_root, authorization=None):
        calls.append((name, repo_root))
        agent = Agent(name=name, system_prompt=skills[name].system_prompt, registry=ToolRegistry())
        ensure_persona(session, agent)
        return agent

    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="ok")]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=lambda s, repo_root=None: Agent(name="t", system_prompt="s", registry=ToolRegistry()),
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
        skills=skills,
        build_skill_agent=build_skill,
    )
    client = TestClient(create_app(ctx, store=RunStore()))
    resp = client.post("/api/runs/sync", json={"text": "嗨", "mode": "demo"})
    assert resp.status_code == 200
    assert calls and calls[0][0] == "demo"
