"""跨工具编号一致性：search_history 与 read_history 必须共享同一套 #编号坐标系。"""

from agent.core.llm import Message
from agent.tools.builtin import register_builtin
from agent.tools.registry import ToolRegistry


def test_history_numbering_consistent():
    """两工具对同一条消息给出的 #编号必须一致（同一数据的多个视图共享坐标系）。"""
    history = [
        Message(role="system", content="人设"),
        Message(role="user", content="第一句话是海星"),
        Message(role="assistant", content="收到"),
    ]
    registry = ToolRegistry()
    register_builtin(registry, kb=None, llm=None, history=history)

    # content→position：按关键词命中用户第一句
    search_out = registry.execute("search_history", '{"query": "海星"}')
    assert "#1 [user] 第一句话是海星" in search_out

    # position→content：按同一编号直接读回同一条原话
    read_out = registry.execute("read_history", '{"start": 1, "count": 1}')
    assert "#1 [user] 第一句话是海星" in read_out