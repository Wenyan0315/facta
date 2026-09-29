"""SSE 编码器（纯函数）：把 Run 事件编码成 text/event-stream 帧（S2b）。

SSE 是独立协议边界，必须可测——encode_sse / encode_heartbeat 当纯函数单测，
否则下游所有 bug 都会出现在这段字节里（课件测试矩阵 TC01-06 的落地版）。

帧格式：
    id: <seq>            事件位置（前端 Last-Event-ID 断线重连用）
    event: <type>        事件类型（前端按它路由渲染）
    data: <json>         JSON payload
    （空行）              一条 SSE 事件的分隔符

关键：json.dumps(ensure_ascii=False) 让中文原样（不变 \\u 天书），
同时自动转义 payload 内的换行/引号/反斜杠——否则它们会撕破单行 data 帧。
"""

from __future__ import annotations

import json

from facta.server.run_store import SCHEMA_VERSION, RunEvent


def encode_sse(run_id: str, event: RunEvent) -> str:
    """一条 Run 事件 → SSE 帧。payload 带 schema_version + run_id + seq + type + data。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "seq": event.seq,
        "type": event.type,
        "data": event.data,
    }
    return (
        f"id: {event.seq}\n"
        f"event: {event.type}\n"
        "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    )


def encode_heartbeat() -> str:
    """心跳帧：SSE 注释行（: 开头），不进事件语义、不占业务 seq，只保活长连接。"""
    return ": heartbeat\n\n"
