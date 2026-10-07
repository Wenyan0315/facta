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

事件透传（059，receives_event 通道）：子 agent 的过程事件以 `sub.*`
命名空间转进父事件流——**隔离的是主 agent 的上下文（底片），不是人的
眼睛**。子事件不进 checkpoint 账本（writer 只认精确类型，见 059），
所以恢复语义、评测轨迹口径都不受影响；并行 spawn 时兄弟事件会交织，
每条带 task 摘要用于区分。

取消透传（082 ①，receives_cancel 通道）：主循环的 should_cancel 回调
透传进子 run_turn——子任务与主循环在同样的协作式取消检查点上响应取消，
不再只等主循环边界（取消是 Run 级终态，子任务掐半截轮后按 CANCELLED
回灌，主 agent 自纠）。

失败语义走反馈环（083 结构化）：spawn_subagent 返回 (RunResult, 结论串)，
工具层只暴露结论串给主 agent 自纠（换方案或如实汇报），不炸主轮（M5
「错误也返回字符串」惯例）；spawn_step 按 RunResult 回写 done/failed，
不再靠字符串前缀（枚举 → 字符串 → 前缀匹配的中间态已删，详见 083）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from facta.core.llm import LLM
from facta.core.types import Message
from facta.memory.store import Session
from facta.orchestrator.agent import Agent
from facta.orchestrator.loop import RunResult, run_turn
from facta.tools.context import ToolContext
from facta.tools.files import register_file_tools
from facta.tools.plan import format_view
from facta.tools.registry import Tool, ToolRegistry
from facta.tools.terminal import register_terminal_tools
from facta.tools.worktree import (
    commit_and_merge_back,
    create_worktree,
    discard_worktree,
    worktree_changes,
)

# 禁止清单（程序侧硬编码，显式指定也不放行）：
# - spawn_subagent / spawn_step（S6c）：递归防护（子 agent 不再派子 agent，
#   也不派发计划步骤——它是执行者不是编排者，规划深度=2 层封顶）
# - 计划三件：分派语义是「主 agent 已规划好，子 agent 执行」；子 agent 再
#   规划=规划套规划，主 plan 事件流被稀释（挂触发信号：真实使用出现
#   「子任务本身够复杂需要二级计划」再开，届时 S6 编排也该上了）
# - sync_graph（S7a）：图谱是主 agent 维护的共享知识资产——子 agent 是
#   执行者，不重抽/改写共享图谱（它写的笔记要进图谱，让主 agent 收口）
# - 历史两件（评审修复轮）：search_history/read_history 的闭包绑着【父会话】
#   messages——默认子集含它们=「子上下文看不到本对话」的 prompt 承诺被
#   工具层击穿（子 agent 能检索父会话全部内容，噪声隔离反向泄漏）
_FORBIDDEN = frozenset({
    "spawn_subagent", "spawn_step", "make_plan", "update_plan_step", "finish_plan",
    "sync_graph", "search_history", "read_history",
})

DEFAULT_ROUNDS = 3
MAX_ROUNDS = 10

TASK_TEMPLATE = (
    "你是被派来执行一项具体任务的专项执行员。任务：{task}\n"
    "只做这一件事，用可用的工具把它做完；"
    "做完后用 2-5 句话汇报结论（做了什么、结果如何、关键发现），"
    "不要寒暄、不要复述任务、不要展开中间过程。"
)

# 059 子事件命名映射：run_turn 的五种事件 → 点分 sub.* 命名空间。
# 点分风格与 plan.* 一致 ⇒ server 侧 `_EVENT_MAP.get(type_, type_)` 原样
# 透传到 SSE / Run Store，零改动（未知类型不丢）。子 agent 没有计划工具
# （_FORBIDDEN），所以不会出现 sub.plan.*——命名空间只需覆盖这五种。
_SUB_EVENT_MAP = {
    "tool_started": "sub.tool.started",
    "tool_result": "sub.tool.result",
    "stuck": "sub.stuck",
    "max_rounds": "sub.max_rounds",
    "error": "sub.error",
}

_SUB_TASK_LEN = 60   # 事件里带的 task 摘要长度：够区分并行兄弟，不撑爆事件流


def _sub_emitter(
    on_event: Callable[[str, dict], None] | None,
    task: str,
) -> Callable[[str, dict], None] | None:
    """把父事件缝包成子事件缝：改名进 sub.* 命名空间 + 挂 task 摘要。

    None 进 None 出（CLI/评测不关心事件时子 agent 也零开销）。
    `{**data, ...}` 而非原地改：data 由 loop 造出后会同时喂给多个消费者
    （RunStore、checkpoint writer、SSE），在这条缝上加字段就用副本，
    不动上游对象。
    """
    if on_event is None:
        return None
    brief = task.strip()[:_SUB_TASK_LEN]

    def emit(type_: str, data: dict) -> None:
        on_event(_SUB_EVENT_MAP.get(type_, f"sub.{type_}"), {**data, "task": brief})

    return emit


def _worktree_registry(registry: ToolRegistry, ctx: ToolContext, wt: Path) -> ToolRegistry:
    """S6a worktree 模式的子 registry：file/terminal 五件重锚 worktree，其余原样共享。

    重锚=重新注册（闭包锚 wt 目录）；共享=搬运同一 Tool 对象（闭包锚主资源
    ——知识库/待办/时钟共享是正确语义）。审计同源（registry.audit 透传），
    S3「单一必经点」与 S5a「确认缝收口不分叉」都保持。
    """
    # scope_check 一并继承（057）：范围声明锚的是主会话的 plan 棋盘，子 agent 受
    # 主计划约束是正确语义——不继承就成洞（主 agent 声明窄范围，再把渗出步骤
    # spawn_subagent(worktree=True) 派出去，子 agent 在裸 registry 里想用什么用什么）
    sub = ToolRegistry(audit=registry.audit, scope_check=registry.scope_check)
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
    on_event: Callable[[str, dict], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    worktree: bool = False,
    ctx: ToolContext | None = None,
) -> tuple[RunResult, str]:
    """构造子 agent + 临时会话跑一轮，回传结构化 (status, conclusion)。

    单独导出为模块级函数（不是闭包）：测试可直接调，不经 registry 菜单。
    confirm / on_event / should_cancel 由 registry.execute 的 receives_confirm /
    receives_event / receives_cancel 通道注入（见 registry.py）。

    status 直接复用 run_turn 的 RunResult（COMPLETED / CANCELLED / FAILED），
    供 spawn_step 按枚举回写 done/failed（083，不再靠字符串前缀）；工具层
    （_spawn 闭包）解包只取 conclusion，对主 agent 仍是「只回传结论」。

    worktree（S6a）：True = 子 agent 在独立 git worktree 里干活——文件
    改动不碰主工作区；跑完后 diff 经确认缝裁决（人审掌舵，与 make_plan
    同一原则）：批准→commit+merge 回主分支；拒绝/无通道→整棵丢弃
    （保守默认：没有眼睛就不动手）。需要 ctx（重锚信息：notes_dir 等）。
    """
    if not task.strip():
        return RunResult.FAILED, "错误：task 不能为空——说清楚要子任务做什么"
    rounds = max(1, min(int(max_rounds), MAX_ROUNDS))

    if worktree and ctx is None:
        return RunResult.FAILED, "错误：worktree 模式需要装配上下文（spawn 未接 ctx，检查注册路径）"

    # S6a worktree 分支：先建沙箱，子 registry 重锚，跑完裁决合回/丢弃
    wt_dir: Path | None = None
    effective_registry = registry
    if worktree and ctx is not None:
        wt_dir, err = create_worktree()
        if err:
            return RunResult.FAILED, f"错误：{err}"
        effective_registry = _worktree_registry(registry, ctx, wt_dir)

    # 工具子集：默认全量；显式指定 ∩ 全量；一律过禁止单
    available = set(effective_registry.names()) - _FORBIDDEN
    if tools:
        wanted = set(tools) & available
        if not wanted:
            if wt_dir is not None:
                discard_worktree(wt_dir)
            return (
                RunResult.FAILED,
                f"错误：指定的工具都不在可用清单里（可用：{sorted(available)}；"
                "spawn_subagent 与计划工具不可派给子 agent）",
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
        # 事件缝透传（059）：过程事件改名进 sub.* 后写进父流；on_text 不透传
        # （子 agent 的流式正文不是给人看的成品，只回传结论这条边界不动）
        on_event=_sub_emitter(on_event, task),
        should_cancel=should_cancel,   # 082 ①：取消缝透传——子任务同主循环一样
        # 在协作式取消检查点上响应取消，不再只等主循环边界
    )
    if result is RunResult.COMPLETED and reply is not None:
        conclusion = reply.content or "（子任务完成，但未产出文本结论）"
    elif result is RunResult.CANCELLED:
        conclusion = "子任务被取消，未产出结论"
    else:
        conclusion = "子任务失败：模型不可用（可稍后重试，或由你直接执行）"

    # S6a worktree 收尾（裁决细节在 _worktree_finalization）
    if wt_dir is not None:
        return result, _worktree_finalization(wt_dir, task, conclusion, confirm)
    return result, conclusion


def register_spawn_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """spawn_subagent 上菜单（S5c）。ctx.llm 缺席 = 不上菜单（条件注册惯例）。"""
    if ctx.llm is None:
        return
    sub_llm = ctx.llm   # 局部窄化：闭包捕获局部变量（mypy 不认跨闭包的属性窄化）

    def _spawn(task: str, tools: list[str] | None = None, max_rounds: int = DEFAULT_ROUNDS,
               worktree: bool = False, confirm=None, event=None, should_cancel=None) -> str:
        _, conclusion = spawn_subagent(
            task, llm=sub_llm, registry=registry,
            tools=tools, max_rounds=max_rounds, confirm=confirm, on_event=event,
            should_cancel=should_cancel, worktree=worktree, ctx=ctx,
        )
        return conclusion

    registry.register(Tool(
        name="spawn_subagent",
        description=(
            "把一项边界清晰的子任务派给一个干净的执行上下文去跑，只回传结论"
            "（中间过程不打扰本对话）。适合检索、整理、验证类杂活，"
            "或任何「过程啰嗦但结论一句话」的工作。tools 可限定子上下文能用的工具，"
            "max_rounds 是其工具循环预算（默认 3）。注意：子上下文没有本对话的历史。"
            "worktree=True 时子上下文在独立的 git worktree 沙箱里改文件——改动不碰"
            "当前工作区，跑完经用户确认后合回（拒绝则整棵丢弃）。"
            "有多件互不依赖的杂活要同时做时，可一次调用多个 spawn_subagent"
            "（同一轮回复里并列点菜），它们会并行执行。"
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
        receives_event=True,     # 059：事件缝透传（registry 注入 event 参数 → sub.* 进父流）
        receives_cancel=True,    # 082 ①：取消缝透传（registry 注入 should_cancel → 子 run_turn）
    ))

    # ---- S6c spawn_step：计划步骤派发（真编排的焊缝）----
    # 把「计划的一步」绑定「一个子任务」：标 in_progress → 派子任务 → 按
    # 成败回写 done/failed（自动回写，模型不用手动 update_plan_step）。
    # 需要 ctx.session（计划载体）——缺席则不上菜单（条件注册惯例）。
    if ctx.session is not None:
        board = ctx.session.plan

        def _spawn_step(step_id: int, task: str, tools: list[str] | None = None,
                        max_rounds: int = DEFAULT_ROUNDS, worktree: bool = False,
                        confirm=None, event=None, should_cancel=None) -> str:
            # 校验在 board.update_step 里统一做（薄包装原则，与 plan.py 工具同款）：
            # 无活跃计划 / step_id 不在计划 / 已终态，都 ValueError → 错误串回灌
            try:
                board.update_step(step_id, "in_progress", note="子任务执行中")
            except ValueError as e:
                return f"步骤派发被拒：{e}"

            # 复用 spawn_subagent 全套（噪声隔离/工具子集/worktree 隔离/确认透传/事件透传/取消透传）
            # 099：tools/max_rounds 透传——两条派发入口参数面对称；子任务工具＝
            # 计划白名单（自动继承）∩ 本参数，不构成第二真值源
            result_status, conclusion = spawn_subagent(
                task, llm=sub_llm, registry=registry,
                tools=tools, max_rounds=max_rounds,
                worktree=worktree, ctx=ctx, confirm=confirm, on_event=event,
                should_cancel=should_cancel,
            )

            # 083：按 RunResult 枚举回写 done/failed，不再靠字符串前缀（撞前缀即误判）
            step_status, prefix = (
                ("failed", "执行失败")
                if result_status is not RunResult.COMPLETED
                else ("done", "执行完成")
            )
            try:
                board.update_step(step_id, step_status, note=conclusion)
            except ValueError as e:   # 理论上不会（前面已校验 + 同步执行）
                return f"步骤已派发但回写失败：{e}\n子任务结果：{conclusion}"
            return (
                f"步骤 #{step_id} {prefix}（子任务结论）：\n{conclusion}\n\n"
                f"当前计划：\n{format_view(board.view())}"
            )

        registry.register(Tool(
            name="spawn_step",
            description=(
                "把当前计划里的一个步骤派给子 agent 执行，完成后自动回写该步骤状态"
                "（成功标 done、失败标 failed），无需你手动再调 update_plan_step。"
                "用它与 make_plan 配合：先 make_plan 拆步骤，再逐个 spawn_step 执行，"
                "最后 finish_plan 收官。step_id 是计划里的步骤编号（以最新计划为准）。"
                "tools 可限定子任务能用的工具（缺省＝计划白名单内全量；本计划声明的"
                "工具范围会自动继承并对子任务生效），max_rounds 是子任务的工具循环"
                "预算（默认 3）。worktree=True 时子任务在独立 git worktree 沙箱里"
                "改文件，改动经确认后合回。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "step_id": {"type": "integer", "description": "计划步骤编号（以最新计划为准）"},
                    "task": {"type": "string", "description": "这一步的执行任务书：做什么、产出什么结论（自包含）"},
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "子任务可用的工具名清单（缺省＝计划白名单内全量；白名单自动继承）",
                    },
                    "max_rounds": {"type": "integer", "description": "子任务工具循环预算（默认 3，上限 10）"},
                    "worktree": {"type": "boolean", "description": "是否在独立 git worktree 沙箱里改文件（改代码类步骤用 true）"},
                },
                "required": ["step_id", "task"],
            },
            func=_spawn_step,
            receives_confirm=True,   # worktree 合回确认透传（S6a 同款）
            receives_event=True,     # 059：子步骤过程同样以 sub.* 进父流
            receives_cancel=True,    # 082 ①：子步骤取消透传（与 spawn_subagent 同款）
        ))
