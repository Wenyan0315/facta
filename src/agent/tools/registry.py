"""工具注册表：agent 的「手」。

核心认知（M5 最重要的一句话）：
模型从不执行任何东西——它只是返回"我想调用哪个工具、参数是什么"（JSON），
真正执行函数的是我们的程序。工具清单是我们给的，函数是我们实现的，
模型只能在"菜单"里点菜。

Tool：一个工具的四要素（名字/说明书/参数schema/函数本体）
ToolRegistry：登记所有工具，对外提供两件事——
  1. schemas()  → 生成给模型看的"菜单"
  2. execute()  → 按模型点菜执行，返回字符串结果（错误也返回字符串）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    """一个工具 = 元信息 + 函数本体。

    name/description/parameters 都是给模型看的"使用说明书"：
    模型能不能正确选用这个工具，全靠 description 写得好不好。
    """

    name: str                       # 工具名，模型用它"点菜"
    description: str                # 说明书：什么时候该用这个工具
    parameters: dict                # JSON Schema：参数结构
    func: Callable[..., str]        # 真正执行的 Python 函数


class ToolRegistry:
    """登记工具 + 生成菜单 + 执行点单。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """登记一个工具（名字重复时后者覆盖前者）。"""
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        """当前已登记的工具名清单（给外部展示用）。"""
        return list(self._tools.keys())

    def schemas(self) -> list[dict]:
        """生成 OpenAI 格式的工具清单——这就是发给模型的「菜单」。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, arguments_json: str) -> str:
        """执行模型点的菜。注意：错误也返回字符串，而不是抛异常。

        为什么？——错误信息回填给模型后，模型能自我纠正重试。
        崩溃没有意义；让模型看到"哪里错了"才有意义。这是 agent 的容错反馈环。
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：不存在名为 {name} 的工具"

        # 模型给的 arguments 是 JSON 字符串，先解析成 dict
        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
        except json.JSONDecodeError as e:
            return f"错误：参数不是合法的 JSON（{e}）"

        try:
            result = tool.func(**args)
        except TypeError as e:
            return f"错误：参数不匹配（{e}）"
        except Exception as e:  # 兜底：工具内部任何异常都不让程序崩溃
            return f"错误：工具执行失败（{type(e).__name__}: {e}）"

        return str(result)
