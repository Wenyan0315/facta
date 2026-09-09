"""M6 会话状态：既存底片（原文），也存压缩缓存。

M6.2 之前只把 messages（底片）落盘，summary / summarized_upto（压缩缓存）
是 run_chat 的局部变量，重启即清零——滚动摘要退化成「每次启动一次性全量大压缩」。
本模块把「底片 + 缓存」打包成一个 Session 对象整体持久化，才是完整的会话状态。

存储格式（version 预留演进）：
    {
      "version": 1,
      "messages": [ {"role","content","tool_calls"?,"tool_call_id"?}, ... ],
      "memory": { "summary": "...", "summarized_upto": 37 }
    }
旧格式（M6.2 及以前）是纯 [Message, ...] 列表，load 时识别并自动迁移。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent.core.types import Message

SESSION_VERSION = 1


@dataclass
class Session:
    """一次会话的完整状态：底片（原文）+ 缓存（滚动摘要与覆盖进度）。

    这是「状态」的完整定义——不仅消息是状态，消息之上的摘要游标也是状态。
    只存消息不存游标，重启后游标归零，滚动摘要等于白做。
    """

    messages: list[Message] = field(default_factory=list)
    summary: str | None = None
    summarized_upto: int = 1


def save_session(session: Session, path: Path) -> None:
    """把会话状态（底片 + 缓存）存成 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)   # 父目录不存在就建（第一次跑 data/memory/ 还不存在）
    data = {
        "version": SESSION_VERSION,
        "messages": [asdict(m) for m in session.messages],
        "memory": {
            "summary": session.summary,
            "summarized_upto": session.summarized_upto,
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)   # ensure_ascii=False：中文原样存，不变 \u 天书


def load_session(path: Path) -> Session:
    """读回会话状态。文件不存在 → 空会话（第一次跑很正常，别报错）；旧列表格式 → 自动迁移。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return Session()

    # 旧格式迁移：M6.2 及以前存的是纯 [Message, ...] 列表，没有缓存——summary 归空、游标归 1
    if isinstance(raw, list):
        return Session(messages=[Message(**d) for d in raw])

    messages = [Message(**d) for d in raw.get("messages", [])]
    memory = raw.get("memory", {})
    summarized_upto = memory.get("summarized_upto", 1)
    # 游标不可越界：防御性钳到 [1, len(messages)] 内
    return Session(
        messages=messages,
        summary=memory.get("summary"),
        summarized_upto=max(1, min(summarized_upto, len(messages))),
    )