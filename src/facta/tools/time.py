"""时间工具（builtin 拆分，S4a 清账）：get_current_time。"""

from __future__ import annotations

from datetime import datetime

from facta.tools.registry import Tool, ToolRegistry

_EMPTY_PARAMS = {"type": "object", "properties": {}, "required": []}


def get_current_time() -> str:
    """返回当前日期、时间和星期。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")


def register_time_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="get_current_time",
        description="获取当前的日期、时间和星期。当用户询问现在几点、今天几号、今天星期几时使用。",
        parameters=_EMPTY_PARAMS,
        func=get_current_time,
        is_readonly=True,
    ))
