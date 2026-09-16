"""待办工具三件：add_todo / list_todos / complete_todo（2026-09-17）。

语义裁定（014 产品定位）：「任务」= 个人待办（UI 创建/勾销，agent 可读写）；
工程委派（长任务 Run）是 S4/S6 的能力，不占「任务」一词。

不学 Claude Code 的单工具 TodoWrite（整体覆写语义）：三个简单工具
参数更浅、模型点菜更准；个人待办量级下没有批量改写的真实需求
（触发信号：出现「一次改十条」的真实用法再并）。
"""

from __future__ import annotations

from agent.memory.todos import TodoStore
from agent.tools.registry import Tool, ToolRegistry


def _fmt(todos: list) -> str:
    if not todos:
        return "（无待办）"
    lines = []
    for t in todos:
        mark = "✓" if t.done else "○"
        extra = f"（{t.done_at[:10]} 完成）" if t.done_at else ""
        lines.append(f"{mark} #{t.id} {t.text}{extra}")
    return "\n".join(lines)


def register_todo_tools(registry: ToolRegistry, store: TodoStore) -> None:
    """待办工具注册。store 由装配层构造注入（同一实例给 Web API 共用）。"""
    registry.register(Tool(
        name="add_todo",
        description="添加一条个人待办。用户说「记一下」「提醒我」「别忘了」指未来要做的事时使用。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待办内容，一句话说清做什么（如「9/20 后查阳澄湖天气」）"},
            },
            "required": ["text"],
        },
        func=lambda text: f"已添加待办 #{store.add(text).id}：{text}",
    ))
    registry.register(Tool(
        name="list_todos",
        description="列出个人待办。用户问「我有什么待办」「还有什么没做」时使用；确认待办内容时也用它先看现状。",
        parameters={
            "type": "object",
            "properties": {
                "only_pending": {"type": "boolean", "description": "只看未完成的（默认 true）；false 时含已完成"},
            },
            "required": [],
        },
        func=lambda only_pending=True: _fmt(store.list(only_pending=bool(only_pending))),
    ))
    registry.register(Tool(
        name="complete_todo",
        description="勾销一条待办。用户说某件事「做完了」「搞定了」时使用。",
        parameters={
            "type": "object",
            "properties": {
                "todo_id": {"type": "integer", "description": "待办编号（#号后的数字），先用 list_todos 查"},
            },
            "required": ["todo_id"],
        },
        func=lambda todo_id: (
            f"已勾销 #{todo_id}：{t.text}"
            if (t := store.complete(int(todo_id))) is not None
            else f"#{todo_id} 不存在，请先用 list_todos 查看现有编号"
        ),
    ))
