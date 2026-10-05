"""时间戳注入验收（真实使用经验逼出的功能）：投影带「今天」、底片不带。

不变量：时间戳属于「本轮视野」，绝不混进「对话内容」——
入底片会堆日期垃圾、被摘要压缩吸收；投影每轮现切、随轮作废。
"""

from datetime import datetime

from facta.cli import EXIT_QUIT, run_chat
from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.plan import PlanBoard
from facta.orchestrator.projection import _append_stamps, _time_stamp


def test_time_stamp_format():
    fixed = datetime(2026, 9, 12, 15, 30)
    stamp = _time_stamp(fixed)
    assert stamp.role == "system"
    # 格式：日期 + 星期 + 时段（082 ② 从分钟降为上午/下午/晚间，prefix 缓存友好）
    assert stamp.content == "今天：2026-09-12（周六）下午"


def test_stamp_in_payload_not_in_history(monkeypatch):
    inputs = iter(["你好", "退出"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))
    llm = ScriptedLLM([Message(role="assistant", content="恢复啦")])
    script = llm  # ScriptedLLM 记录每次收到的 messages 快照

    session, reason = run_chat(script, None)

    # 底片干净：历史里没有任何时间戳痕迹
    assert all("今天：" not in m.content for m in session.messages)
    # 投影有戳：模型这一轮确实看到了「今天」
    assert any("今天：" in m.content for m in script.calls[0])
    # 正常输入「退出」→ 退出原因 quit（S1 返回值契约）
    assert reason == EXIT_QUIT


def test_stamps_appended_to_tail_not_head():
    """080：三件 stamp 动态后置，为 prefix 缓存让路——头不动、尾追加。"""
    payload = [
        Message(role="system", content="人设"),
        Message(role="user", content="hi"),
    ]
    # 空板 + 无路由决定：只追加时间戳（缺席不注入零开销）
    _append_stamps(payload, None, PlanBoard())

    # 头部不动：system/user 仍原位（前缀字节稳定，同会话连续轮次 prefix 命中）
    assert payload[0].content == "人设"
    assert payload[1].content == "hi"
    # 时间戳被追加到尾部，而不是插在头部位置 1
    assert "今天：" in payload[-1].content
    assert "今天：" not in payload[0].content
