"""S2b SSE 编码器验收：帧结构 + 中文/换行/引号/反斜杠往返 + 心跳不占 seq。"""

import json

from agent.server.run_store import RunEvent, SCHEMA_VERSION
from agent.server.sse import encode_heartbeat, encode_sse


def _decode_data_line(sse_text: str) -> dict:
    """从一帧 SSE 文本里取回 data 行的 JSON（编码正确性的反向验证）。"""
    data_line = next(l for l in sse_text.split("\n") if l.startswith("data: "))
    return json.loads(data_line[len("data: "):])


def test_encode_sse_frame_structure():
    ev = RunEvent(seq=2, type="tool.started", data={"name": "search_notes"})
    s = encode_sse("r1", ev)

    assert s.startswith("id: 2\n")
    assert "event: tool.started\n" in s
    assert s.endswith("\n\n")   # 空行分隔符


def test_encode_sse_roundtrip_special_chars():
    # 中文 + 换行 + 双引号 + 反斜杠，一帧全部安全往返
    original = "换行\n引号\"反斜杠\\中文"
    ev = RunEvent(seq=3, type="text.delta", data={"delta": original})
    payload = _decode_data_line(encode_sse("r123", ev))

    assert payload["data"]["delta"] == original          # 内容不丢字、不被转义破坏
    assert payload["run_id"] == "r123"
    assert payload["seq"] == 3
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["type"] == "text.delta"


def test_encode_heartbeat_is_comment_line():
    s = encode_heartbeat()
    assert s == ": heartbeat\n\n"
    assert "id:" not in s and "seq" not in s   # 心跳不占业务 seq
