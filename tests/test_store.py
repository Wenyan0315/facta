"""M6 会话状态持久化的往返测试：底片 + 压缩缓存整体落盘、无损读回。"""

import json

import pytest

from facta.core.types import Message
from facta.memory.store import Session, load_session, save_session


def test_session_round_trip(tmp_path):
    """save → load 往返无损：消息、摘要、覆盖游标一个都不能丢（P0-1 的回归）。"""
    session = Session(
        messages=[
            Message(role="system", content="人设"),
            Message(role="user", content="暗号=海星"),
            Message(role="assistant", content="记住了"),
        ],
        summary="摘要：用户住海边",
        summarized_upto=3,
    )
    path = tmp_path / "session.json"
    save_session(session, path)
    loaded = load_session(path)
    assert loaded.messages == session.messages
    assert loaded.summary == session.summary
    assert loaded.summarized_upto == 3


def test_title_round_trip(tmp_path):
    """S2 验收修复轮：title 是会话展示标签，跟着状态一起落盘、无损读回。"""
    session = Session(messages=[Message(role="user", content="你好")], title="PHP 做 Agent 工具层")
    path = tmp_path / "session.json"
    save_session(session, path)
    assert load_session(path).title == "PHP 做 Agent 工具层"


def test_load_legacy_without_title(tmp_path):
    """旧归档无 title 字段 → None（list 时 fallback 首句派生）。"""
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps({
            "version": 1,
            "messages": [{"role": "user", "content": "你好"}],
            "memory": {"summary": None, "summarized_upto": 1},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    assert load_session(path).title is None


def test_load_legacy_list_format(tmp_path):
    """旧格式（M6.2 及以前的纯 [Message,...] 列表）自动迁移：缓存归空、游标归 1。"""
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps([{"role": "user", "content": "旧格式消息"}], ensure_ascii=False),
        encoding="utf-8",
    )
    session = load_session(path)
    assert len(session.messages) == 1
    assert session.messages[0].content == "旧格式消息"
    assert session.summary is None
    assert session.summarized_upto == 1


def test_load_missing_file_returns_empty_session(tmp_path):
    """文件不存在 = 新会话（第一次跑很正常），静默返回空，不报错。"""
    session = load_session(tmp_path / "no_such_file.json")
    assert session.messages == []
    assert session.summary is None


def test_load_clamps_summarized_upto(tmp_path):
    """游标不可越界：损坏/异常值钳到 [1, len(messages)] 内，防切片越界。"""
    data = {
        "version": 1,
        "messages": [{"role": "user", "content": "a"}],
        "memory": {"summary": "s", "summarized_upto": 99},
    }
    path = tmp_path / "session.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    session = load_session(path)
    assert session.summarized_upto == 1


# ---------- P1-6 评审修复：并发 create 的 id 唯一性 ----------

def test_concurrent_create_same_second_gets_unique_ids(tmp_path):
    """同秒并发 create 曾互相拿到同一个 id（评审 Barrier 复现：只落一个文件
    + FileNotFoundError）。「分配 + 落盘」整体持锁后：id 全唯一、文件全落。
    """
    import threading
    from datetime import datetime

    from facta.memory.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    now = datetime(2026, 9, 29, 12, 0, 0)   # 固定同秒：锁不锁的分水岭就在这
    results: list[str] = []
    barrier = threading.Barrier(4)

    def _create():
        barrier.wait()   # 对齐线程，最大化同秒碰撞窗口
        results.append(store.create(Session(), now=now))

    threads = [threading.Thread(target=_create) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 4
    assert len(list((tmp_path / "sessions").glob("*.json"))) == 4


# ADR 071：跨进程并发 create。同一时刻 CLI + Web 同时建新会话——线程锁绕开
# （不同进程），只有 fcntl.flock 能挡。Subprocess 在独立 Python 解释器里跑
# SessionStore.create，Barrier 用 multiprocessing.Event 对齐启动窗口。
@pytest.mark.skipif(
    not __import__("facta.memory.store", fromlist=["_HAS_FCNTL"])._HAS_FCNTL,
    reason="fcntl 不可用（Windows）——跨进程 flock 降级为线程锁",
)
def test_cross_process_create_same_second_gets_unique_ids(tmp_path):
    import multiprocessing


    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    barrier = multiprocessing.Barrier(3)
    procs: list[multiprocessing.Process] = []

    for _ in range(3):
        p = multiprocessing.Process(target=_child_create, args=(sessions_dir, barrier))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()
    # 三个独立进程各拿一个独立 id，无 FileNotFoundError / 无重复
    assert len(list(sessions_dir.glob("*.json"))) == 3


# multiprocessing.spawn 需要模块级函数（本地函数不可 pickle）
def _child_create(sessions_dir, barrier):
    from facta.memory.store import SessionStore
    store = SessionStore(sessions_dir)
    barrier.wait()
    store.create(Session())


# ---------- ADR 085：fork = create(load(sid)) ----------

def test_fork_copies_session_to_new_id(tmp_path):
    """fork 出完整副本：新 id ≠ 源 id、新文件独立、内容与源一致。"""
    from datetime import datetime

    from facta.memory.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    src = store.create(
        Session(
            messages=[
                Message(role="system", content="人设"),
                Message(role="user", content="暗号=海星"),
            ],
            summary="摘要：用户住海边",
            summarized_upto=2,
            title="原会话",
        ),
        now=datetime(2026, 9, 30, 10, 0, 0),
    )

    new_sid = store.fork(src, now=datetime(2026, 9, 30, 11, 0, 0))
    assert new_sid != src

    forked = store.load(new_sid)
    assert forked.messages == store.load(src).messages
    assert forked.summary == "摘要：用户住海边"
    assert forked.summarized_upto == 2
    assert forked.title == "原会话"


def test_fork_is_independent_from_source(tmp_path):
    """fork 是深拷贝：改新会话不影响源会话（各自独立物理文件）。"""
    from datetime import datetime

    from facta.memory.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    src = store.create(
        Session(messages=[Message(role="user", content="起点")]),
        now=datetime(2026, 9, 30, 10, 0, 0),
    )
    new_sid = store.fork(src, now=datetime(2026, 9, 30, 11, 0, 0))

    forked = store.load(new_sid)
    forked.messages.append(Message(role="assistant", content="fork 里新走的路"))
    store.save(new_sid, forked)

    assert len(store.load(src).messages) == 1   # 源纹丝不动
    assert len(store.load(new_sid).messages) == 2


def test_fork_missing_session_raises(tmp_path):
    """fork 不存在的会话：load 缺席大声崩（与 load 同语义）。"""
    from facta.memory.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    with pytest.raises(FileNotFoundError):
        store.fork("20260930-100000")
