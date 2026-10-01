"""S8a 会话仓库验收：身份=文件名、不搬文件 + 标题派生 + run_chat 退出原因契约。

不变量：
- 一段对话只有一个物理副本，由【位置唯一】保证（不再有 active/archive 两处）
- 文件名 = 身份（零填充时间戳，字典序 = 时间序）；标题 = 标签（提炼优先，缺失时首句派生）
- run_chat 只发信号不碰文件：/new → 返回 EXIT_NEW，收官与另起一段是 __main__ 的活

（S1 时代的 archive/restore/list_archived 三件套已随 move 语义整体下线。）
"""

import os
from datetime import datetime

import pytest

from facta.cli import EXIT_NEW, EXIT_QUIT, run_chat
from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.store import Session, SessionStore, derive_title, save_session


def _talked(first_user: str = "第一句话") -> Session:
    return Session(
        messages=[
            Message(role="system", content="sys"),
            Message(role="user", content=first_user),
            Message(role="assistant", content="回复"),
        ]
    )


# ---------- create / load / save / delete ----------

def test_create_persists_and_id_is_filename(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    sid = store.create(_talked(), now=datetime(2026, 9, 13, 10, 19, 56))

    assert sid == "20260913-101956"                    # 身份 = 零填充时间戳
    assert store.path(sid).name == "20260913-101956.json"
    assert store.load(sid).messages[1].content == "第一句话"   # create 即落盘


def test_create_same_second_appends_counter_not_overwrite(tmp_path):
    # 同秒不覆写（S2 验收修复轮#3）：Web 快速点击曾让同秒两次写入共用一个
    # 文件名，后写覆盖先写——会话数凭空变少。S8a 归档没了，但「快速连点新建」
    # 是同一个触发信号，序号逻辑原样保留。
    store = SessionStore(tmp_path / "sessions")
    stamp = datetime(2026, 9, 13, 10, 19, 56)
    first = store.create(_talked("第一个会话"), now=stamp)
    second = store.create(_talked("第二个会话"), now=stamp)

    assert (first, second) == ("20260913-101956", "20260913-101956-1")
    assert store.load(first).messages[1].content == "第一个会话"
    assert store.load(second).messages[1].content == "第二个会话"


def test_creates_dir_on_first_run(tmp_path):
    store = SessionStore(tmp_path / "sessions" / "nested" / "deep")   # 目录还不存在
    assert store.load(store.create(Session())).messages == []


def test_path_rejects_illegal_id(tmp_path):
    # id 来自 HTTP 路径 = 外部输入：不合白名单大声崩，不做静默清洗
    store = SessionStore(tmp_path)
    for bad in ["../../etc/passwd", "session", "20260913-1019", ""]:
        with pytest.raises(ValueError):
            store.path(bad)


def test_load_missing_raises(tmp_path):
    # 「必须已经有了」是调用方的前提；静默返回空会话会把新壳子写回去覆盖真人对话
    with pytest.raises(FileNotFoundError):
        SessionStore(tmp_path).load("20260913-101956")


def test_delete_is_idempotent(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create(_talked())
    assert store.delete(sid) is True
    assert store.delete(sid) is False          # 第二次：没东西可删，不抛


# ---------- 清单 ----------

def test_list_metas_sorted_by_mtime_desc(tmp_path):
    store = SessionStore(tmp_path)
    old = store.create(_talked("早上的会话"), now=datetime(2026, 9, 12, 9, 0, 0))
    new = store.create(_talked("下午的会话"), now=datetime(2026, 9, 13, 10, 19, 56))
    # 身份是创建时刻，排序看的是【最后聊过】的时刻 —— 两者不必同序
    past = datetime(2026, 9, 11, 0, 0, 0).timestamp()
    os.utime(store.path(new), (past, past))

    metas = store.list_metas()
    assert [m.id for m in metas] == [old, new]
    assert [m.title for m in metas] == ["早上的会话", "下午的会话"]


def test_list_metas_missing_dir_returns_empty(tmp_path):
    assert SessionStore(tmp_path / "不存在").list_metas() == []
    assert SessionStore(tmp_path / "不存在").latest() is None


def test_list_metas_skips_corrupt_and_foreign_files(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create(_talked("完好会话"))
    (tmp_path / "20260912-000000.json").write_text("{ 半截", encoding="utf-8")  # partial write
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")                # 人手塞进来的

    metas = store.list_metas()
    assert [m.id for m in metas] == [sid]      # 坏文件与外来文件都不进清单，也不炸


def test_list_metas_prefers_llm_title_over_first_message(tmp_path):
    store = SessionStore(tmp_path)
    session = _talked("PHP 结合 AI Agent 可以做什么")
    session.title = "PHP 工具封装"
    store.create(session)

    assert store.list_metas()[0].title == "PHP 工具封装"


def test_latest_is_most_recently_touched(tmp_path):
    store = SessionStore(tmp_path)
    first = store.create(_talked())
    second = store.create(_talked())
    assert store.latest() == second            # 刚建的排最前
    store.save(first, store.load(first))       # 聊回旧会话 → 它变成 latest
    # 显式推进 mtime：本测试断言的是「最近触碰者居首」的排序语义，不是文件系统的
    # 时间戳精度——粗粒度 fs（部分 overlay/网络盘）上 save 与 create 同刻会拿到
    # 相同 mtime，排序语义对，测试却红了（issue #15 收尾时实测复现，干净 main 同款）
    future = store.path(second).stat().st_mtime + 2
    os.utime(store.path(first), (future, future))
    assert store.latest() == first


# ---------- 老布局一次性迁移 ----------

def test_migrate_legacy_active_moves_and_is_idempotent(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    legacy = tmp_path / "session.json"
    save_session(_talked("老 active 里的对话"), legacy)

    sid = store.migrate_legacy_active(legacy)

    assert not legacy.exists()                                  # move，不是 copy
    assert store.load(sid).messages[1].content == "老 active 里的对话"
    assert store.migrate_legacy_active(legacy) is None           # 幂等：源已不在
    assert store.migrate_legacy_active(tmp_path / "从来没有.json") is None


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
    title = derive_title(Session(messages=[Message(role="user", content="第一行\n第二行")]))
    assert "\n" not in title   # 多行压成一行，防清单被换行撕裂


def test_derive_title_empty_session_placeholder():
    assert derive_title(Session()) == "（空会话）"


# ---------- run_chat 退出原因契约 ----------

def test_new_word_signals_exit_new(monkeypatch):
    """/new 只发信号：底片保留、不清空（收官与另起一段归 __main__）。"""
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
