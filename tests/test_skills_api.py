"""ADR 118 §2：`GET /api/skills` 契约——委派表单技能下拉的数据源。

此前技能名只活在 mode 校验 422 的报错串里，用户要知道有哪些技能得去翻
`data/skills/`；任务面板要让人「选技能委派」，就必须有个只读端点。

不变量：
- 字段全是 SkillSpec 的只读投影，**不含人设正文**（那是 prompt，不是表单料）
- 名字升序（下拉顺序稳定，不随 dict 插入序漂移）
- settle 把布尔 skip_settle 派生成前端好读的 "skip"/"normal"
- 空技能表 = 空列表（表单只剩「主 agent（全工具池）」一项）
"""

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facta.core.llm import ScriptedLLM
from facta.memory.store import SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext
from facta.server.app import create_app
from facta.skills import AUTH_READONLY, AUTH_WRITE, SkillSpec
from facta.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    """与 test_app/test_authorization 同款隔离：装配与 worker 都会真落盘。"""
    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.assemble.LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))


def _client(skills: dict[str, SkillSpec]) -> TestClient:
    """最小 ctx：只读清单不需要真 LLM/真技能工厂（不起 run，故工厂给桩）。"""
    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=lambda s, repo_root=None: Agent(
            name="t", system_prompt="s", registry=ToolRegistry(),
        ),
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
        skills=skills,
        build_skill_agent=lambda *a, **k: None,
    )
    return TestClient(create_app(ctx))


def test_list_skills_projects_spec_fields():
    specs = {
        "demo": SkillSpec(
            name="demo", system_prompt="人设正文", authorization=AUTH_WRITE,
            max_tool_rounds=12, skip_settle=True,
        ),
        "brief": SkillSpec(
            name="brief", system_prompt="人设正文", authorization=AUTH_READONLY,
            max_tool_rounds=7,
        ),
    }
    body = _client(specs).get("/api/skills").json()
    # 升序 + 四字段闭集（多一个字段＝契约变了，前端要跟着改，故钉死形状）
    assert body == [
        {"name": "brief", "authorization": "readonly", "budget": 7, "settle": "normal"},
        {"name": "demo", "authorization": "write", "budget": 12, "settle": "skip"},
    ]


def test_list_skills_matches_registry_and_hides_prompt():
    specs = {
        n: SkillSpec(name=n, system_prompt="独一无二的人设句子")
        for n in ("aaa", "bbb", "ccc")
    }
    body = _client(specs).get("/api/skills").json()
    assert {s["name"] for s in body} == set(specs)          # 与 registry 一致，不多不少
    assert "独一无二的人设句子" not in json.dumps(body, ensure_ascii=False)


def test_list_skills_empty_registry():
    assert _client({}).get("/api/skills").json() == []
