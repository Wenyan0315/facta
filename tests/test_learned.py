"""记忆面板验收（2026-09-19，025）：learned 读/改/删原语 + 三端点。

不变量：
- 行号定位协议：GET 返回的 line 原样传回 PUT/DELETE，操作后行号仍对
- 好行编辑保留日期前缀（时间戳归程序管）；坏行（手写行）原样替换可删
- 空行/文件尾换行在重写后原样保留（append-only 固化的兼容前提）
- category 白名单挡路径穿越，line 越界 404，空内容 400
"""

import pytest

from agent.memory.learned import delete_line, read_learned, update_line


def _write(tmp_path, text):
    path = tmp_path / "constraints.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------- read_learned：解析 ----------

def test_read_parses_formatted_lines(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 第一条\n- [2026-09-14] 第二条\n")
    entries = read_learned(path)
    assert [(e.line, e.date, e.content) for e in entries] == [
        (0, "2026-09-13", "第一条"),
        (1, "2026-09-14", "第二条"),
    ]


def test_read_tolerates_bad_lines_and_skips_blank(tmp_path):
    # 手写坏行不炸：date=None、content=原行全文；空行不进列表（行号仍按文件计）
    path = _write(tmp_path, "- [2026-09-13] 好行\n\n手写的裸行\n")
    entries = read_learned(path)
    assert [(e.line, e.date) for e in entries] == [(0, "2026-09-13"), (2, None)]
    assert entries[1].content == "手写的裸行"


def test_read_missing_file_is_empty(tmp_path):
    assert read_learned(tmp_path / "nope.md") == []


# ---------- update / delete：行号定位 ----------

def test_update_keeps_date_prefix(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 原文\n")
    update_line(path, 0, "新正文")
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] 新正文\n"


def test_update_bad_line_replaces_raw(tmp_path):
    path = _write(tmp_path, "手写裸行\n- [2026-09-13] 好行\n")
    update_line(path, 0, "改过的裸行")
    assert path.read_text(encoding="utf-8") == "改过的裸行\n- [2026-09-13] 好行\n"


def test_update_preserves_blank_lines_and_tail_newline(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 甲\n\n- [2026-09-14] 乙\n")
    update_line(path, 2, "乙改")
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] 甲\n\n- [2026-09-14] 乙改\n"


def test_line_out_of_range_raises(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 唯一\n")
    with pytest.raises(IndexError):
        update_line(path, 5, "x")
    with pytest.raises(IndexError):
        delete_line(path, 5)


def test_delete_line_keeps_rest(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 甲\n- [2026-09-14] 乙\n- [2026-09-15] 丙\n")
    delete_line(path, 1)
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] 甲\n- [2026-09-15] 丙\n"


# ---------- API 端点 ----------

def _client(monkeypatch, tmp_path):
    """最小 AppContext + LEARNED_DIR 指向临时目录（与 test_app 同款隔离模式）。"""
    from fastapi.testclient import TestClient

    from agent.core.llm import ScriptedLLM
    from agent.core.types import Message
    from agent.memory.store import SessionStore
    from agent.orchestrator.agent import Agent
    from agent.orchestrator.assemble import AppContext
    from agent.server.app import create_app
    from agent.tools.registry import ToolRegistry

    monkeypatch.setattr("agent.server.app.LEARNED_DIR", tmp_path)

    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="ok")]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(tmp_path / "sessions"),
        # 记忆面板端点不跑 worker，工厂只需满足类型（不会真被调用）
        build_agent=lambda session: Agent(
            name="test", system_prompt="测试人设", registry=ToolRegistry()
        ),
        todos=None,
    )
    return TestClient(create_app(ctx))


def test_api_roundtrip_edit_and_delete(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    (tmp_path / "constraints.md").write_text(
        "- [2026-09-13] 甲\n- [2026-09-14] 乙\n", encoding="utf-8"
    )

    listed = client.get("/api/learned").json()
    assert [(e["category"], e["line"], e["date"]) for e in listed] == [
        ("constraints", 0, "2026-09-13"),
        ("constraints", 1, "2026-09-14"),
    ]

    # 编辑保留日期
    assert client.put(
        "/api/learned/constraints/0", json={"content": "甲改"}
    ).status_code == 200
    # 删除按行号
    assert client.delete("/api/learned/constraints/1").status_code == 200

    after = client.get("/api/learned").json()
    assert len(after) == 1
    assert after[0]["date"] == "2026-09-13" and after[0]["content"] == "甲改"
    # 被删行后，剩余行号已变（0）——前端 refresh 后用新行号，协议自洽
    assert after[0]["line"] == 0


def test_api_guards(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    (tmp_path / "other.md").write_text("- [2026-09-13] 唯一\n", encoding="utf-8")

    # 路径穿越被挡（httpx 客户端侧即归一化 → 405；即便构造原始请求，
    # category 白名单也会拦）——断言只认「没成功写入」
    assert client.put("/api/learned/../etc/0", json={"content": "x"}).status_code >= 400
    assert client.put("/api/learned/other/9", json={"content": "x"}).status_code == 404
    assert client.put("/api/learned/other/0", json={"content": "  "}).status_code == 400
    assert client.delete("/api/learned/decisions/0").status_code == 404   # 类别文件不存在


# ---------- 041：用户级分栏 ----------

def test_learned_path_user_follows_env(monkeypatch, tmp_path):
    """user 的路径走 paths.user_memory_path()——与 agent 注入侧同源（CORTEX_USER_MEMORY）。"""
    from agent.server.app import PANEL_CATEGORIES, _learned_path

    monkeypatch.setattr("agent.server.app.LEARNED_DIR", tmp_path)
    monkeypatch.setenv("CORTEX_USER_MEMORY", str(tmp_path / "user.md"))
    assert _learned_path("user") == tmp_path / "user.md"
    assert _learned_path("constraints") == tmp_path / "constraints.md"
    assert PANEL_CATEGORIES == ("decisions", "constraints", "other", "user")


def test_api_user_scope_roundtrip(monkeypatch, tmp_path):
    user_md = tmp_path / "user.md"
    user_md.write_text(
        "- [2026-09-20] 行程提早一周提醒\n- [2026-09-21] 回答用中文\n", encoding="utf-8"
    )
    project = tmp_path / "constraints.md"
    project.write_text("- [2026-09-13] 项目甲\n", encoding="utf-8")
    before = project.read_bytes()
    monkeypatch.setenv("CORTEX_USER_MEMORY", str(user_md))
    client = _client(monkeypatch, tmp_path)

    listed = client.get("/api/learned").json()
    assert [(e["category"], e["line"], e["date"]) for e in listed if e["category"] == "user"] == [
        ("user", 0, "2026-09-20"),
        ("user", 1, "2026-09-21"),
    ]

    # 编辑保留日期前缀、删除按行号——与项目桶同一套协议（同一个 learned.py）
    assert client.put("/api/learned/user/0", json={"content": "行程提早两周提醒"}).status_code == 200
    assert client.delete("/api/learned/user/1").status_code == 200
    assert user_md.read_text(encoding="utf-8") == "- [2026-09-20] 行程提早两周提醒\n"
    # 用户级操作不动项目桶（字节级）
    assert project.read_bytes() == before


def test_api_user_scope_missing_file_and_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("CORTEX_USER_MEMORY", str(tmp_path / "never-created.md"))
    client = _client(monkeypatch, tmp_path)

    # 新用户：固化管线还没写出 user.md → 空栏，不是错误
    assert client.get("/api/learned").json() == []
    assert client.put("/api/learned/user/0", json={"content": "x"}).status_code == 404
    assert client.delete("/api/learned/user/0").status_code == 404
    # 白名单外的类别仍 400（含看起来像目录名的），穿越防线没被 user 撑开
    assert client.put("/api/learned/preferences/0", json={"content": "x"}).status_code == 400
    assert client.delete("/api/learned/notes/0").status_code == 400

