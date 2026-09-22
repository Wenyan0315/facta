"""spawn_subagent 工具（S5c）：子 agent 分派——只回传结论，噪声隔离。

021 定的隔离边界三件全落地：
- 只回传结论：子 agent 的全部中间过程（点菜/观察/工具输出）不进主会话
  底片——主上下文只多一条 tool 消息。这就是「噪声隔离」的精确语义。
- 工具子集由主 agent 指定：默认=全量减禁止清单；显式指定也强制过禁止清单
  （递归防护与「主规划子执行」分工都归程序管，不信任模型自觉——S5b 同款哲学）。
- 轮数预算注入：max_rounds 默认 3（分派的杂活，小预算防漫游），上限 10。

子 agent 是 S5a Agent 对象的实例化（六字段直接复用）：
    system_prompt  任务书模板（专项执行员人设+结论格式）
    allowed_tools  主 agent 指定（禁单强制过滤）
    max_tool_rounds 预算注入
    registry       与主 agent 共享同一实例——审计/L2 确认收口不分叉（S5a 决策）
    router         None（M10 路由是主 agent 前置判断；子任务场景已定，不重复路由）

临时 Session：纯内存对象，不落盘、不进主会话、用完即弃——不进 Run Store
不受单锁约束（spawn 是主 Run worker 内的工具调用，不是新 Run；单进程内
隔离，真并行多 Run 仍留 S6）。

确认缝透传（receives_confirm 通道）：子 agent 的高危工具照常走 registry
确认缝 → 主循环 on_confirm → Web 上挂起当前 Run 弹窗——人审不分主子。

失败语义走反馈环：子 run_turn FAILED/CANCELLED → 返回错误串，主 agent
自纠（换方案或如实汇报），不炸主轮（M5「错误也返回字符串」惯例）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent.core.llm import LLM
from agent.core.types import Message
from agent.memory.store import Session
from agent.orchestrator.agent import Agent
from agent.orchestrator.loop import RunResult, run_turn
from agent.tools.context import ToolContext
from agent.tools.files import register_file_tools
from agent.tools.registry import Tool, ToolRegistry
from agent.tools.terminal import register_terminal_tools
from agent.tools.worktree import (
    commit_and_merge_back,
    create_worktree,
    discard_worktree,
    worktree_changes,
)

# 禁止清单（程序侧硬编码，显式指定也不放行）：
# - spawn_subagent：递归防护（子 agent 不再派子 agent，规划深度=2 层封顶）
# - 计划三件：分派语义是「主 agent 已规划好，子 agent 执行」；子 agent 再
#   规划=规划套规划，主 plan 事件流被稀释（挂触发信号：真实使用出现
#   「子任务本身够复杂需要二级计划」再开，届时 S6 编排也该上了）
# - 历史两件（评审修复轮）：search_history/read_history 的闭包绑着【父会话】
#   messages——默认子集含它们=「子上下文看不到本对话」的 prompt 承诺被
#   工具层击穿（子 agent 能检索父会话全部内容，噪声隔离反向泄漏）
_FORBIDDEN = frozenset({
    "spawn_subagent", "make_plan", "update_plan_step", "finish_plan",
    "search_history", "read_history",
})

DEFAULT_ROUNDS = 3
MAX_ROUNDS = 10

TASK_TEMPLATE = (
    "你是被派来执行一项具体任务的专项执行员。任务：{task}\n"
    "只做这一件事，用可用的工具把它做完；"
    "做完后用 2-5 句话汇报结论（做了什么、结果如何、关键发现），"
    "不要寒暄、不要复述任务、不要展开中间过程。"
)


def _worktree_registry(registry: ToolRegistry, ctx: ToolContext, wt: Path) -> ToolRegistry:
    """S6a worktree 模式的子 registry：file/terminal 五件重锚 worktree，其余原样共享。

    重锚=重新注册（闭包锚 wt 目录）；共享=搬运同一 Tool 对象（闭包锚主资源
    ——知识库/待办/时钟共享是正确语义）。审计同源（registry.audit 透传），
    S3「单一必经点」与 S5a「确认缝收口不分叉」都保持。
    """
    sub = ToolRegistry(audit=registry.audit)
    wt_ctx = ToolContext(notes_dir=ctx.notes_dir, workspace_root=wt)
    register_file_tools(sub, wt_ctx)
    register_terminal_tools(sub, wt_ctx)
    for name in registry.names():
        if name not in {"read_file", "search_code", "list_dir", "write_file", "run_command"}:
            tool = registry.get(name)
            if tool is not None:
                sub.register(tool)
    return sub


def _worktree_finalization(
    wt: Path,
    task: str,
    conclusion: str,
    confirm: Callable[[str, dict], bool] | None,
) -> str:
    """S6a worktree 收尾：改动经确认缝裁决——批准合回，拒绝/无通道丢弃。

    保守默认与 L2 同哲学：没有眼睛就不动手（confirm 缺失 = 丢弃）。
    """
    changes = worktree_changes(wt)
    if not changes:
        discard_worktree(wt)
        return f"{conclusion}\n〔worktree 无文件改动，沙箱已清理〕"
    approved = confirm is not None and confirm(
        "merge_worktree", {"task": task[:100], "changes": changes[:2000]}
    )
    if approved:
        outcome = commit_and_merge_back(wt, message=f"spawn: {task.strip()[:60]}")
    else:
        discard_worktree(wt)
        outcome = "用户未批准合回（或无确认通道），改动已整棵丢弃，主工作区未受影响"
    return f"{conclusion}\n〔文件改动〕\n{changes}\n〔处理〕{outcome}"


def spawn_subagent(
    task: str,
    *,
    llm: LLM,
    registry: ToolRegistry,
    tools: list[str] | None = None,
    max_rounds: int = DEFAULT_ROUNDS,
    confirm: Callable[[str, dict], bool] | None = None,
    worktree: bool = False,
    ctx: ToolContext | None = None,
) -> str:
    """构造子 agent + 临时会话跑一轮，只回传结论（spawn 工具的本体）。

    单独导出为模块级函数（不是闭包）：测试可直接调，不经 registry 菜单。
    confirm 由 registry.execute 的 receives_confirm 通道注入（见 registry.py）。

    worktree（S6a）：True = 子 agent 在独立 git worktree 里干活——文件
    改动不碰主工作区；跑完后 diff 经确认缝裁决（人审掌舵，与 make_plan
    同一原则）：批准→commit+merge 回主分支；拒绝/无通道→整棵丢弃
    （保守默认：没有眼睛就不动手）。需要 ctx（重锚信息：notes_dir 等）。
    """
    if not task.strip():
        return "错误：task 不能为空——说清楚要子任务做什么"
    rounds = max(1, min(int(max_rounds), MAX_ROUNDS))

    if worktree and ctx is None:
        return "错误：worktree 模式需要装配上下文（spawn 未接 ctx，检查注册路径）"

    # S6a worktree 分支：先建沙箱，子 registry 重锚，跑完裁决合回/丢弃
    wt_dir: Path | None = None
    effective_registry = registry
    if worktree and ctx is not None:
        wt_dir, err = create_worktree()
        if err:
            return f"错误：{err}"
        effective_registry = _worktree_registry(registry, ctx, wt_dir)

    # 工具子集：默认全量；显式指定 ∩ 全量；一律过禁止单
    available = set(effective_registry.names()) - _FORBIDDEN
    if tools:
        wanted = set(tools) & available
        if not wanted:
            if wt_dir is not None:
                discard_worktree(wt_dir)
            return (
                f"错误：指定的工具都不在可用清单里（可用：{sorted(available)}；"
                "spawn_subagent 与计划工具不可派给子 agent）"
            )
    else:
        wanted = available

    sub_agent = Agent(
        name="sub",
        system_prompt=TASK_TEMPLATE.format(task=task.strip()),
        registry=effective_registry,
        allowed_tools=frozenset(wanted),
        max_tool_rounds=rounds,
    )
    # 临时会话：人设即任务书，直接种进底片第一条——子会话一次性，
    # 无压缩无恢复，不走 ensure_persona（那是主会话的装配不变量）
    sub_session = Session()
    sub_session.messages.append(Message(role="system", content=sub_agent.system_prompt))

    result, reply = run_turn(
        sub_session,
        task.strip(),
        agent=sub_agent,
        llm=llm,
        on_confirm=confirm,   # 确认缝透传：子 agent 高危工具照常请求裁决
        # should_cancel 不透传：取消等主循环下一检查点（子任务通常几轮内完成）
    )
    if result is RunResult.COMPLETED and reply is not None:
        conclusion = reply.content or "（子任务完成，但未产出文本结论）"
    elif result is RunResult.CANCELLED:
        conclusion = "子任务被取消，未产出结论"
    else:
        conclusion = "子任务失败：模型不可用（可稍后重试，或由你直接执行）"

    # S6a worktree 收尾（裁决细节在 _worktree_finalization）
    if wt_dir is not None:
        return _worktree_finalization(wt_dir, task, conclusion, confirm)
    return conclusion


def register_spawn_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """spawn_subagent 上菜单（S5c）。ctx.llm 缺席 = 不上菜单（条件注册惯例）。"""
    if ctx.llm is None:
        return
    sub_llm = ctx.llm   # 局部窄化：闭包捕获局部变量（mypy 不认跨闭包的属性窄化）

    def _spawn(task: str, tools: list[str] | None = None, max_rounds: int = DEFAULT_ROUNDS,
               worktree: bool = False, confirm=None) -> str:
        return spawn_subagent(
            task, llm=sub_llm, registry=registry,
            tools=tools, max_rounds=max_rounds, confirm=confirm,
            worktree=worktree, ctx=ctx,
        )

    registry.register(Tool(
        name="spawn_subagent",
        description=(
            "把一项边界清晰的子任务派给一个干净的执行上下文去跑，只回传结论"
            "（中间过程不打扰本对话）。适合检索、整理、验证类杂活，"
            "或任何「过程啰嗦但结论一句话」的工作。tools 可限定子上下文能用的工具，"
            "max_rounds 是其工具循环预算（默认 3）。注意：子上下文没有本对话的历史。"
            "worktree=True 时子上下文在独立的 git worktree 沙箱里改文件——改动不碰"
            "当前工作区，跑完经用户确认后合回（拒绝则整棵丢弃）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "子任务书：做什么、产出什么结论（自包含，子上下文看不到本对话）"},
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "子上下文可用的工具名清单（缺省=全部可用工具）",
                },
                "max_rounds": {"type": "integer", "description": "工具循环预算（默认 3，上限 10）"},
                "worktree": {"type": "boolean", "description": "是否在独立 git worktree 沙箱里执行文件改动（改代码类任务用 true；改动经确认后合回主分支）"},
            },
            "required": ["task"],
        },
        func=_spawn,
        receives_confirm=True,   # S5c：确认缝透传给子执行流（registry 注入 confirm 参数）
    ))
