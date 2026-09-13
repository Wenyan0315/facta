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
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
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


def archive_session(path: Path, archive_dir: Path, now: datetime | None = None) -> Path:
    """S1 归档原子操作：把会话文件复制进归档仓库，命名 = 零填充时间戳。

    - 文件名即身份（机器）：唯一、零填充 `%Y%m%d-%H%M%S` 保证「字典序 = 时间序」，
      列历史清单 sorted() 文件名即可，不用读内容
    - 标题是标签（人看）：这里不生成——由调用方派生（用户消息首句），
      身份和标签分离，不往文件名里塞易漂移的文本
    - 用 shutil.copy2 复制而非 rename：文件消失前先复制成功，
      「先存后清」的语义就落在这里——调用方确认本函数成功后才重置内存
    - now 参数留给测试注入固定时刻
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    target = archive_dir / f"{now:%Y%m%d-%H%M%S}.json"
    shutil.copy2(path, target)
    return target


TITLE_MAX_LEN = 20   # 标题截断上限：它是清单里的一行标签，不是摘要


def derive_title(session: Session) -> str:
    """身份 vs 标签（S1 裁定二）：身份在文件名，标签 = 用户首句【派生】。

    派生而非另存的用意：标签永远是「算出来的」，和内容天然一致——
    不存在「改完内容忘了同步标题」的漂移；想换截断长度随时重派生，
    身份（时间戳文件名）纹丝不动。
    """
    for m in session.messages:
        if m.role == "user":
            first = m.content.strip().replace("\n", " ")
            if len(first) <= TITLE_MAX_LEN:
                return first
            return first[:TITLE_MAX_LEN] + "…"
    return "（空会话）"


def list_archived_sessions(archive_dir: Path) -> list[tuple[Path, str]]:
    """历史会话清单：按文件名升序（零填充 → 字典序 = 时间序），每项 (归档路径, 标题)。

    这是 S2 会话列表的直接原料。v1 每个文件全量 load 再派生标题——教学
    规模足够；升级触发信号（文件数 >20 或单文件 >10MB）届时换只读头的索引。
    """
    if not archive_dir.is_dir():
        return []
    items = []
    for path in sorted(archive_dir.glob("*.json")):
        items.append((path, derive_title(load_session(path))))
    return items