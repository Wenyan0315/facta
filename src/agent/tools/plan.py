"""计划工具三件（S5b）：make_plan / update_plan_step / finish_plan。

设计落位（027 三拍板）：
- 模型自判：make_plan 是普通工具，点不点模型定（复杂度是语义概念）
- 人审掌舵：make_plan 标 needs_confirmation=True，复用 S4b 确认缝全套设施
  （CLI input / Web 挂起重弹 / 批准拒绝都落审）——机制复用、语义不同
  （L2 问「危险吗」，计划审批问「对吗」）。v1 弹窗显示 JSON 可审，
  漂亮面板归 S5c
- 双视图：轮首投影块管「开局定位」，update_plan_step 的工具结果回灌带
  最新全量视图管「行进导航」——同轮内多步连续执行时模型不靠过时快照

薄包装原则：状态机全部在 memory/plan.py（board），本层只做三件事——
调 board、ValueError 转错误串（M5 反馈环：错误也返回字符串让模型自纠，
错误文案即给模型的提示词，每条拒绝都指路）、回灌格式化。
"""

from __future__ import annotations

from agent.memory.plan import Plan, StepStatus
from agent.tools.context import ToolContext
from agent.tools.registry import Tool, ToolRegistry

_MARKS = {   # 视图渲染记号：pending 空、in_progress 半、done 满、skipped/failed 各式否决
    "pending": "○",
    "in_progress": "◐",
    "done": "●",
    "skipped": "×",
    "failed": "✗",
}


def format_view(view: Plan | None) -> str:
    """计划视图的人/模型两用文本渲染（工具回灌与投影注入共用）。"""
    if view is None:
        return "（无活跃计划）"
    lines = []
    for s in view.steps:
        note = f" —— {s.note}" if s.note else ""
        lines.append(f"{_MARKS[s.status.value]} {s.id}. {s.title}{note}")
    return "\n".join(lines)


def register_plan_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """计划三件上菜单（S5b）。session 缺席 = 不上菜单（条件注册惯例）。"""
    if ctx.session is None:
        return
    board = ctx.session.plan

    def _make_plan(steps: list[dict], reason: str = "") -> str:
        try:
            kind = board.make_plan(steps, reason=reason)
        except ValueError as e:
            return f"计划操作被拒：{e}"
        verb = "已创建" if kind == "created" else "已修订"
        # S6c 实机验收发现 A：模型不知道「步骤可派出去」这条焊缝——用户明说
        # 派子任务它仍自己做。回灌补一句中性引导（掌舵权归模型：简单步骤
        # 自己做更便宜，重步骤派 spawn_step 换隔离与噪声抑制，它自己选）
        return (
            f"计划{verb}（用户已确认）。当前计划：\n{format_view(board.view())}"
            "\n（执行提示：步骤可自己做，也可用 spawn_step 派子任务执行——"
            "过程啰嗦或值得上下文隔离的步骤建议派出去，它会自动回写状态）"
        )

    def _update_plan_step(step_id: int, status: str, note: str = "") -> str:
        try:
            board.update_step(step_id, status, note)
        except ValueError as e:
            return f"计划操作被拒：{e}"
        return (
            f"已更新步骤 #{step_id} → {status}。当前计划：\n{format_view(board.view())}"
            "\n（继续执行；全部步骤终态化后用 finish_plan 收官）"
        )

    def _finish_plan(summary: str) -> str:
        try:
            board.finish_plan(summary)
        except ValueError as e:
            return f"计划操作被拒：{e}"
        return f"任务收官（全部步骤已终态化，计划转入归档）：{summary}"

    registry.register(Tool(
        name="make_plan",
        description=(
            "为多步骤复杂任务创建执行计划（先规划→执行→逐步回写状态）。"
            "仅在任务足够复杂、值得先出蓝图时使用；简单请求直接做。"
            "修改当前计划也用本工具：steps 是完整新表（修订时每步必须显式声明 status，"
            "对照旧计划继承），reason 必填说明修订原因。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "步骤表（新建只给 title；修订给完整新表）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "一步做什么（一句话）"},
                            "status": {
                                "type": "string",
                                "enum": [s.value for s in StepStatus],
                                "description": "步骤状态（修订时必填，新建时忽略）",
                            },
                            "note": {"type": "string", "description": "状态说明（继承时保留原注）"},
                        },
                        "required": ["title"],
                    },
                },
                "reason": {"type": "string", "description": "修订原因（修改已有计划时必填）"},
            },
            "required": ["steps"],
        },
        func=_make_plan,
        needs_confirmation=True,   # 人审掌舵点：计划创建/修订都要过确认缝（021）
    ))
    registry.register(Tool(
        name="update_plan_step",
        description="回写计划步骤状态：开始做标 in_progress，做完标 done（note 一句话结果）；不需要了标 skipped、做不成标 failed（都必须带 note 说明）。终态不可再改，计划有变走 make_plan 修订。",
        parameters={
            "type": "object",
            "properties": {
                "step_id": {"type": "integer", "description": "步骤编号（以最新计划为准）"},
                "status": {"type": "string", "enum": [s.value for s in StepStatus]},
                "note": {"type": "string", "description": "结果/跳过理由/失败原因（终态必填）"},
            },
            "required": ["step_id", "status"],
        },
        func=_update_plan_step,
    ))
    registry.register(Tool(
        name="finish_plan",
        description="计划收官：全部步骤终态化（done/skipped/failed，无悬空）后调用，summary 一句话总结交付。未终态化会被拒绝。",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "收官总结（做了什么、结果如何）"},
            },
            "required": ["summary"],
        },
        func=_finish_plan,
    ))
