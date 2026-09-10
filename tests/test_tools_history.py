"""跨工具编号一致性：search_history 与 read_history 必须共享同一套 #编号坐标系。"""

from agent.core.types import Message
from agent.paths import NOTES_DIR
from agent.tools.builtin import register_builtin
from agent.tools.context import ToolContext
from agent.tools.registry import ToolRegistry


def _make_registry(history: list[Message]) -> ToolRegistry:
    # 本测试只跑 history 工具，notes_dir 不会被真正访问；用共享常量保持全项目一份
    registry = ToolRegistry()
    register_builtin(registry, ToolContext(notes_dir=NOTES_DIR, history=history))
    return registry


def test_history_numbering_consistent():
    """两工具对同一条消息给出的 #编号必须一致（同一数据的多个视图共享坐标系）。"""
    history = [
        Message(role="system", content="人设"),
        Message(role="user", content="第一句话是海星"),
        Message(role="assistant", content="收到"),
    ]
    registry = _make_registry(history)

    # content→position：按关键词命中用户第一句
    search_out = registry.execute("search_history", '{"query": "海星"}')
    assert "#1 [user] 第一句话是海星" in search_out

    # position→content：按同一编号直接读回同一条原话
    read_out = registry.execute("read_history", '{"start": 1, "count": 1}')
    assert "#1 [user] 第一句话是海星" in read_out


def test_history_is_live_reference():
    """List identity trap 回归测试：ctx.history 必须是列表对象本身。

    若哪天有人手滑写成 history=messages.copy()，工具闭包抓的就是旧照片，
    run_chat 原地 append 的新消息它永远看不见——安静变瞎，不报错。
    本测试让这种错误当场爆炸。
    """
    history = [Message(role="system", content="人设")]
    registry = _make_registry(history)

    # 注册【之后】再 append——闭包抓的是同一个对象，必须看得见
    history.append(Message(role="user", content="后来加的暗号：蒲公英"))
    out = registry.execute("search_history", '{"query": "蒲公英"}')
    assert "#1 [user] 后来加的暗号：蒲公英" in out
