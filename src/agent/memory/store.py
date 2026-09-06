import json
from dataclasses import asdict
from pathlib import Path

from agent.core.llm import Message

def save_messages(messages: list[Message], path: Path) -> None:
    """把对话历史存成 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)   # 父目录不存在就建（第一次跑 data/memory/ 还不存在）
    data = [asdict(msg) for msg in messages]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)   # ensure_ascii=False：中文原样存，不变 \u 天书

def load_messages(path: Path) -> list[Message]:
    """读回对话历史。文件不存在时返回空列表（第一次跑很正常，别报错）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []                                    # 第一次跑没有文件 → 空历史，不是错误
    return [Message(**d) for d in data]              # **d：把 dict 的键值对展开成关键字参数
