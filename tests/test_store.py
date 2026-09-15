"""M6 会话状态持久化的往返测试：底片 + 压缩缓存整体落盘、无损读回。"""

import json

from agent.core.types import Message
from agent.memory.store import Session, load_session, save_session


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
