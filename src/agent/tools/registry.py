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


def _validate_args(args: dict, parameters: dict) -> str | None:
    """按 JSON Schema 最小子集校验参数结构；返回错误描述，合法返回 None。

    校验三维分工里的「结构层」——语法层（json.loads）与语义层（工具函数
    自身抛异常）之外的补全。只实现 required + 基础类型：
    花哨的 anyOf/$defs 是给模型看菜单用的，执行防线只需要
    「缺了必填」和「类型错了」两道；未声明字段放行（宽松，不矫枉过正）。
    """

    def type_name(spec: dict) -> str | None:
        # anyOf 等复合写法（MCP 三方 schema 常见）没有单一 type → 跳过
        return spec.get("type") if isinstance(spec.get("type"), str) else None

    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"
    properties = parameters.get("properties", {})
    for key in parameters.get("required", []):
        if key not in args:
            return f"缺少必填参数 {key}"
    for key, value in args.items():
        spec = properties.get(key) or {}
        expected = type_name(spec)
        if not expected:
            continue
        actual = type(value).__name__
        if expected == "string" and not isinstance(value, str):
            return f"参数 {key} 应为字符串，实际 {actual}"
        if expected == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
            return f"参数 {key} 应为整数，实际 {actual}"  # bool 是 int 子类，显式排除
        if expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            # 括号必须齐全：and 优先级高于 or，漏括号会让所有 bool 值
            # 一律误报「应为数字」（list_todos 的 only_pending:true 曾中招）
            return f"参数 {key} 应为数字，实际 {actual}"
        if expected == "boolean" and not isinstance(value, bool):
            return f"参数 {key} 应为布尔，实际 {actual}"
        if expected == "array" and not isinstance(value, list):
            return f"参数 {key} 应为数组，实际 {actual}"
        if expected == "object" and not isinstance(value, dict):
            return f"参数 {key} 应为对象，实际 {actual}"
    return None


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

    def unregister(self, name: str) -> None:
        """把工具从菜单摘除（MCP-c：服务器已死时摘掉死菜，模型不再撞墙）。"""
        self._tools.pop(name, None)

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

        # 结构层校验（JSON Schema 最小子集）：语法合法但缺必填/类型错，
        # 同样以错误字符串回给模型——自我纠正反馈环在两层校验间无差别
        error = _validate_args(args, tool.parameters)
        if error:
            return f"错误：参数校验失败（{error}）"

        try:
            result = tool.func(**args)
        except TypeError as e:
            return f"错误：参数不匹配（{e}）"
        except Exception as e:  # 兜底：工具内部任何异常都不让程序崩溃
            return f"错误：工具执行失败（{type(e).__name__}: {e}）"

        return str(result)
