"""时间戳注入验收（真实使用经验逼出的功能）：投影带「今天」、底片不带。

不变量：时间戳属于「本轮视野」，绝不混进「对话内容」——
入底片会堆日期垃圾、被摘要压缩吸收；投影每轮现切、随轮作废。
"""

from datetime import datetime

from agent.core.agent_loop import _time_stamp, run_chat
from agent.core.llm import ScriptedLLM
from agent.core.types import Message


def test_time_stamp_format():
    fixed = datetime(2026, 9, 12, 15, 30)
    stamp = _time_stamp(fixed)
    assert stamp.role == "system"
    # 格式：日期 + 星期 + 时分（相对时间说法"这周五""下午三点前"才有得算）
    assert stamp.content == "今天：2026-09-12（周六）15:30"


def test_stamp_in_payload_not_in_history(monkeypatch):
    inputs = iter(["你好", "退出"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))
    llm = ScriptedLLM([Message(role="assistant", content="恢复啦")])
    script = llm  # ScriptedLLM 记录每次收到的 messages 快照

    session = run_chat(script, None)

    # 底片干净：历史里没有任何时间戳痕迹
    assert all("今天：" not in m.content for m in session.messages)
    # 投影有戳：模型这一轮确实看到了「今天」
    assert any("今天：" in m.content for m in script.calls[0])