"""S1 多会话管理验收：归档原子操作 / 标题派生 / 清单 + run_chat 退出原因契约。

不变量：
- 归档是「复制」不是「挪走」：active（session.json）位置固定，副本才进仓库
- 文件名 = 身份（零填充时间戳，字典序 = 时间序）；标题 = 标签（用户首句派生，不另存）
- run_chat 只发信号不碰文件：/new → 返回 EXIT_NEW，清理/归档是 __main__ 的活
"""

from datetime import datetime
from pathlib import Path

import pytest

from agent.cli import EXIT_NEW, EXIT_QUIT, run_chat
from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.store import (
    Session,
    archive_session,
    derive_title,
    list_archived_sessions,
    load_session,
    restore_session,
    save_session,
)


def _saved_session(path: Path, first_user: str = "第一句话") -> Session:
    session = Session(
        messages=[
            Message(role="system", content="sys"),
            Message(role="user", content=first_user),
            Message(role="assistant", content="回复"),
        ]
    )
    save_session(session, path)
    return session


# ---------- 归档原子操作 ----------

def test_archive_copies_and_keeps_active(tmp_path):
    active = tmp_path / "session.json"
    expected = _saved_session(active, "帮我看看 MCP 协议")

    target = archive_session(active, tmp_path / "sessions", now=datetime(2026, 9, 13, 10, 19, 56))

    assert target.name == "20260913-101956.json"   # 身份 = 零填充时间戳
    assert active.exists()                          # copy2：active 原样留在原地
    restored = load_session(target)
    assert [m.content for m in restored.messages] == [m.content for m in expected.messages]


def test_archive_creates_dir_on_first_run(tmp_path):
    active = tmp_path / "session.json"
    _saved_session(active)
    target = archive_session(
        active,
        tmp_path / "sessions" / "nested" / "deep",   # 目录还不存在
        now=datetime(2026, 9, 13, 0, 0, 1),
    )
    assert target.exists()   # mkdir(parents=True) 建出的深层目录里躺着归档


# ---------- 标题派生（身份 vs 标签） ----------

def test_derive_title_takes_first_user_first_line():
    session = Session(
        messages=[
            Message(role="system", content="sys"),
            Message(role="user", content="帮我看看 MCP 协议的 initialize 握手细节"),
            Message(role="assistant", content="好"),
            Message(role="user", content="第二个问题"),   # 标题绝不取这条
        ]
    )
    assert derive_title(session) == "帮我看看 MCP 协议的 initial…"   # 截前 20 字 + …


def test_derive_title_short_sentence_as_is():
    session = Session(messages=[Message(role="system", content="s"), Message(role="user", content="你好")])
    assert derive_title(session) == "你好"


def test_derive_title_multiline_flattened():
    session = Session(messages=[Message(role="user", content="第一行\n第二行")])
    title = derive_title(session)
    assert "\n" not in title   # 多行压成一行，防清单被换行撕裂


def test_derive_title_empty_session_placeholder():
    assert derive_title(Session()) == "（空会话）"


# ---------- 清单 ----------

def test_list_sorts_by_time_ascending(tmp_path):
    store = tmp_path / "sessions"
    active = tmp_path / "session.json"
    for stamp, text in [
        (datetime(2026, 9, 12, 9, 0, 0), "早上的会话"),
        (datetime(2026, 9, 13, 10, 19, 56), "下午的会话"),
    ]:
        _saved_session(active, text)
        archive_session(active, store, now=stamp)

    items = list_archived_sessions(store)

    assert [p.name for p, _ in items] == ["20260912-090000.json", "20260913-101956.json"]
    assert [t for _, t in items] == ["早上的会话", "下午的会话"]


def test_list_missing_dir_returns_empty(tmp_path):
    assert list_archived_sessions(tmp_path / "不存在") == []


# ---------- run_chat 退出原因契约 ----------

def test_new_word_signals_exit_new(monkeypatch):
    """/new 只发信号：底片保留、不清空（清理归 __main__）。"""
    inputs = iter(["你好", "/new"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))
    llm = ScriptedLLM([Message(role="assistant", content="恢复啦")])

    session, reason = run_chat(llm, None)

    assert reason == EXIT_NEW
    assert [m.role for m in session.messages] == ["system", "user", "assistant"]   # 底片原样带走


def test_quit_word_signals_exit_quit(monkeypatch):
    inputs = iter(["退出"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))

    _, reason = run_chat(ScriptedLLM([]), None)

    assert reason == EXIT_QUIT


# ---------- restore 切回（move 语义） ----------

def test_restore_moves_archive_to_active(tmp_path):
    active = tmp_path / "session.json"
    archive = tmp_path / "sessions" / "20260913-101956.json"
    _saved_session(archive, "待切回的会话")   # 先有一个归档

    restored = restore_session(archive, active)

    assert restored.messages[1].content == "待切回的会话"   # 切回的内容对
    assert active.exists() and not archive.exists()          # move：归档消失，active 接管
    # active 落盘的内容与返回值一致（写回 active 的就是切回的那段对话）
    assert [m.content for m in load_session(active).messages] == [m.content for m in restored.messages]


def test_restore_missing_archive_raises_without_touching_active(tmp_path):
    active = tmp_path / "session.json"
    _saved_session(active, "当前会话")   # active 已有一句真人对话

    with pytest.raises(FileNotFoundError):
        restore_session(tmp_path / "sessions" / "不存在.json", active)

    # active 原样未动——静默把空会话写进去覆盖真人对话，才是真事故
    assert load_session(active).messages[1].content == "当前会话"
