"""内置工具集聚合入口（S4a 拆分后）：按家族分文件，本文件只做装配。

拆分背景（S4 开工清账，工具数>10 触发）：单文件 334 行 9 工具已到可读性
边界。家族划分：time（1 件）/ history（2 件）/ notes（5 件）——files（S4a
新增 4 件）独立在 files.py，不经本入口（恒注册、无 ctx 依赖）。

register_builtin 签名不变（P1-2 的承诺继续成立）：assemble 调用方零改动。
"""

from __future__ import annotations

from facta.tools.context import ToolContext
from facta.tools.history import register_history_tools
from facta.tools.notes import register_note_tools
from facta.tools.registry import ToolRegistry
from facta.tools.time import register_time_tools


def register_builtin(registry: ToolRegistry, ctx: ToolContext) -> None:
    """把内置工具族登记进注册表（聚合各家族文件的注册函数）。

    ctx.history 注意：是列表对象本身（不是副本）——run_chat 在这个
    列表上原地 append，工具闭包抓同一对象才能实时看到全部历史
    （List identity trap）。
    """
    register_time_tools(registry)
    register_history_tools(registry, ctx)
    register_note_tools(registry, ctx)
