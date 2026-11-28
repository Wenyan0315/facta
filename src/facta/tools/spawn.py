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

P0 绑定继承 + fan-out 只读（Spec 1 §2.4/§2.5）：worktree 模式下子沙箱
继承父任务的工作区绑定（建在绑定仓库、分支显式生成并持有、合回父工作区
=任务候选）；fan-out 条目只读默认（_WRITE_TOOLS），显式请求写工具的条目
整条拦截回执、不静默裁剪。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from facta.core.llm import LLM
from facta.core.types import Message
from facta.memory.store import Session
from facta.orchestrator.agent import Agent
from facta.orchestrator.loop import RunResult, run_turn
from facta.tools.context import ToolContext, WorkspaceBinding
from facta.tools.plan import format_view
from facta.tools.registry import Tool, ToolRegistry
from facta.tools.worktree import (
    commit_and_merge_back,
    create_worktree,
    discard_worktree,
    registry_in_worktree,
    worktree_changes,
)

# 禁止清单（程序侧硬编码，显式指定也不放行）：
# - spawn_subagent / spawn_step（S6c）：递归防护（子 agent 不再派子 agent，
#   也不派发计划步骤——它是执行者不是编排者，规划深度=2 层封顶）
# - fan_out_subagents（线五②）：递归防护同款——fan-out 是编排位，子 agent
#   拿到它就成了三级编排，2 层封顶不破
# - 计划三件：分派语义是「主 agent 已规划好，子 agent 执行」；子 agent 再
#   规划=规划套规划，主 plan 事件流被稀释（挂触发信号：真实使用出现
#   「子任务本身够复杂需要二级计划」再开，届时 S6 编排也该上了）
# - sync_graph（S7a）：图谱是主 agent 维护的共享知识资产——子 agent 是
#   执行者，不重抽/改写共享图谱（它写的笔记要进图谱，让主 agent 收口）
# - 历史两件（评审修复轮）：search_history/read_history 的闭包绑着【父会话】
#   messages——默认子集含它们=「子上下文看不到本对话」的 prompt 承诺被
#   工具层击穿（子 agent 能检索父会话全部内容，噪声隔离反向泄漏）
_FORBIDDEN = frozenset({
    "spawn_subagent", "spawn_step", "fan_out_subagents",
    "make_plan", "update_plan_step", "finish_plan",
    "sync_graph", "search_history", "read_history",
})

# fan-out 只读默认（P0 Spec 1 §2.5，B7/B8）：写类工具硬编码禁入 fan-out 条目
# ——程序侧纪律与 _FORBIDDEN 同款，不依赖模型自觉。显式点名也不静默裁剪：
# 该条目整条拦截不执行、聚合行回执（_FANOUT_WRITE_BLOCK）——静默降级 =
# 模型以为写成功。条目执行中调用写工具则由 allowed_tools 自然拦住、错误串回灌。
_WRITE_TOOLS = frozenset({"write_file", "run_command"})

# B8 固定回执（Spec 1 §2.5 原文）：拦截条目聚合行的结论串，错误串回灌主 agent 自纠
_FANOUT_WRITE_BLOCK = (
    "已拦截：fan-out 不允许写工具，请改用 spawn_subagent(worktree=True) 或任务沙箱"
)

DEFAULT_ROUNDS = 3
MAX_ROUNDS = 10
MAX_FANOUT = 8   # 一次 fan-out 的任务数上限：防模型点菜五十件把 LLM 预算打爆

# 线五②「只回传摘要」的结构上限：回灌主上下文的结论/改动摘要超长即截断
# （纪律变结构——细节全文走 sub.* 事件流给人看、落盘文件给指针，不进主底片）
_CONCLUSION_CAP = 2000
_CHANGES_CAP = 800


def _brief(text: str, cap: int = _CONCLUSION_CAP) -> str:
    """超长截断 + 指针化：细节不进主上下文（编排者禁读细节的执行面）。"""
    if len(text) <= cap:
        return text
    return text[:cap] + "…〔已截断：细节见 sub.* 事件流或子任务落盘文件，不进主上下文〕"

TASK_TEMPLATE = (
    "你是被派来执行一项具体任务的专项执行员。任务：{task}\n"
    "只做这一件事，用可用的工具把它做完；"
    "做完后用 2-5 句话汇报结论（做了什么、结果如何、关键发现），"
    "不要寒暄、不要复述任务、不要展开中间过程。"
    "长产出（全量报告/日志/清单）写进文件，结论里给文件路径作指针，"
    "不要把细节全文贴进结论。"
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

# ADR 117 §5 证据锚点机械闸：spawn_step 自动标 done 前，结论串须含至少一个
# 可验证锚点——checkpoint/tool call id（call_xxx）/ 文件路径 / run_id（32 位
# hex）/ 会话 id（YYYYMMDD-HHMMSS）任一可正则匹配。只挡「空口断言」，
# 不判断证据真伪（真伪核验在 M3 外，负决策）。
_EVIDENCE_ANCHOR_RE = re.compile(
    r"call_[A-Za-z0-9]+"          # checkpoint 账本 id（tool call id）
    r"|[\w.\-/]+\.[A-Za-z][A-Za-z0-9]{0,5}\b"  # 文件路径（扩展名首字符须字母：挡 "3.5" 类数字）
    r"|\b[0-9a-f]{32}\b"          # run_id（uuid4 hex）
    r"|\b\d{8}-\d{6}\b"           # 会话 id
)


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


def _worktree_finalization(
    wt: Path,
    task: str,
    conclusion: str,
    confirm: Callable[[str, dict], bool] | None,
    *,
    branch: str | None = None,
    binding: WorkspaceBinding | None = None,
    merge_into: Path | None = None,
) -> str:
    """S6a worktree 收尾：改动经确认缝裁决——批准合回，拒绝/无通道丢弃。

    保守默认与 L2 同哲学：没有眼睛就不动手（confirm 缺失 = 丢弃）。

    P0 绑定化（Spec 1 §2.4/B6）：binding 给定时 git 一律锚绑定仓库——分支是
    spawn 显式生成并持有的子分支（真值源在 spawn_subagent，不走目录名反推），
    基线取 binding.baseline；合回目标 = 父工作区 merge_into（任务沙箱：改动
    并入任务候选，随任务收口一次合入 target_branch），不是 target_branch
    本身。binding 缺席走老行为（merge_into=None→WORKSPACE_ROOT，分支按
    wt 目录名反推，基线空=只看工作区改动）。
    """
    baseline = binding.baseline if binding is not None else ""
    repo_root = binding.repo_root if binding is not None else None
    changes = worktree_changes(wt, baseline)
    if not changes.any():
        discard_worktree(wt, repo_root=repo_root, branch=branch)
        return f"{conclusion}\n〔worktree 无文件改动，沙箱已清理〕"
    approved = confirm is not None and confirm(
        "merge_worktree", {"task": task[:100], "changes": changes.summary()[:2000]}
    )
    if approved:
        _, outcome = commit_and_merge_back(
            wt,
            message=f"spawn: {task.strip()[:60]}",
            merge_into=merge_into,
            branch=branch,
            target_branch="",
            baseline=baseline,
        )
    else:
        discard_worktree(wt, repo_root=repo_root, branch=branch)
        outcome = "用户未批准合回（或无确认通道），改动已整棵丢弃，主工作区未受影响"
    # 改动摘要截断（线五②）：完整清单只进确认弹窗给人看，回灌结论的是摘要
    return f"{conclusion}\n〔文件改动〕\n{_brief(changes.summary(), _CHANGES_CAP)}\n〔处理〕{outcome}"


def _conclusion_of(result: RunResult, reply: Message | None) -> str:
    """终态 → 结论串（083 结构化返回的展示面，_brief 截断在此收口）。"""
    if result is RunResult.COMPLETED and reply is not None:
        return _brief(reply.content or "（子任务完成，但未产出文本结论）")
    if result is RunResult.CANCELLED:
        return "子任务被取消，未产出结论"
    return "子任务失败：模型不可用（可稍后重试，或由你直接执行）"


def _bound_sandbox(
    ctx: ToolContext, registry: ToolRegistry,
) -> tuple[ToolRegistry, Path, str | None, Path | None, str]:
    """worktree 模式的沙箱开箱 + 子 registry 重锚（P0 Spec 1 §2.4/B6）。

    binding 给定 → 子沙箱建在绑定仓库、分支显式生成（agent/<uuid8>）并
    持有、从 binding.baseline 切；合回目标 = ctx.workspace_root（父工作区
    =任务沙箱）。binding 缺席走老行为（create_worktree 无参、合回主仓）。
    返回 (子 registry, 沙箱目录, 子分支名, 合回目标, 错误串)——错误串非空
    = 创建失败（调用方走反馈环）。
    """
    binding = ctx.binding
    branch: str | None = None
    merge_into: Path | None = None
    if binding is not None:
        branch = f"agent/{uuid.uuid4().hex[:8]}"   # 显式生成并持有：分支名真值源在调用方
        wt_dir, err = create_worktree(
            repo_root=binding.repo_root, branch=branch, baseline=binding.baseline,
        )
        merge_into = ctx.workspace_root
    else:
        wt_dir, err = create_worktree()
    if err:
        return registry, wt_dir, branch, merge_into, err
    return registry_in_worktree(registry, wt_dir, binding=binding), wt_dir, branch, merge_into, ""


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
    read_only: bool = False,
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

    绑定继承（P0 Spec 1 §2.4/B6）：ctx.binding 给定时子沙箱建在绑定仓库、
    分支显式生成（agent/<uuid8>）并持有、从 binding.baseline 切；收尾合回
    目标 = 父工作区（任务沙箱，改动并入任务候选），registry_in_worktree
    一并透传 binding——父子不串仓。binding 缺席走老行为。

    read_only（B7/B8，内部参数不进模型可见 schema）：True = 可用集再减
    _WRITE_TOOLS——fan-out 条目全走 True（只读默认，程序侧纪律）。
    """
    if not task.strip():
        return RunResult.FAILED, "错误：task 不能为空——说清楚要子任务做什么"
    rounds = max(1, min(int(max_rounds), MAX_ROUNDS))

    if worktree and ctx is None:
        return RunResult.FAILED, "错误：worktree 模式需要装配上下文（spawn 未接 ctx，检查注册路径）"

    # S6a worktree 分支：先建沙箱，子 registry 重锚，跑完裁决合回/丢弃
    wt_dir: Path | None = None
    wt_branch: str | None = None
    wt_merge_into: Path | None = None
    binding = ctx.binding if ctx is not None else None
    effective_registry = registry
    if worktree and ctx is not None:
        effective_registry, wt_dir, wt_branch, wt_merge_into, err = _bound_sandbox(ctx, registry)
        if err:
            return RunResult.FAILED, f"错误：{err}"

    # 工具子集：默认全量；显式指定 ∩ 全量；一律过禁止单；只读模式再减写类
    available = set(effective_registry.names()) - _FORBIDDEN
    if read_only:
        available -= _WRITE_TOOLS   # B7：fan-out 只读默认（程序侧硬编码）
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
    conclusion = _conclusion_of(result, reply)

    # S6a worktree 收尾（裁决细节在 _worktree_finalization）
    if wt_dir is not None:
        return result, _worktree_finalization(
            wt_dir, task, conclusion, confirm,
            branch=wt_branch, binding=binding, merge_into=wt_merge_into,
        )
    return result, conclusion


def _fanout_entries(
    tasks: list, tools: list[str] | None,
) -> list[tuple[str, list[str] | None]]:
    """归一化 fan-out 条目 → [(子任务书, 生效工具集)]。

    条目两种形态：纯字符串（工具=共用 tools）；对象 {"task": 子任务书,
    "tools": 本条专用清单}——per-entry 点菜是「条目显式请求写工具」（B8）
    的表达通道。空白/畸形条目丢弃（与旧版对非 str 条目的容错一致），
    tools 非字符串列表时回落共用 tools。
    """
    entries: list[tuple[str, list[str] | None]] = []
    for t in tasks:
        entry_tools: list[str] | None = None
        if isinstance(t, dict):
            text = t.get("task")
            raw = t.get("tools")
            if isinstance(raw, list):
                entry_tools = [x for x in raw if isinstance(x, str)]
            if not isinstance(text, str):
                continue
        elif isinstance(t, str):
            text = t
        else:
            continue
        if text.strip():
            entries.append((text.strip(), entry_tools if entry_tools is not None else tools))
    return entries


def fan_out_subagents(
    tasks: list,
    *,
    llm: LLM,
    registry: ToolRegistry,
    tools: list[str] | None = None,
    max_rounds: int = DEFAULT_ROUNDS,
    confirm: Callable[[str, dict], bool] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """线五② fan-out 聚合：一次派 N 件互不依赖的子任务并行跑，聚合结论回主线。

    与「同轮并列点菜多个 spawn_subagent」的区别是聚合位在程序侧：一次调用
    一条结果，按任务序号确定性聚合（不靠模型自己数数对位），每条结论都过
    「只回传摘要」闸（_brief，spawn_subagent 内已截断）。复用 spawn_subagent
    全套（上下文隔离/工具子集/确认透传/事件透传/取消透传/失败反馈环）。

    fan-out 只读默认（P0 Spec 1 §2.5，B7/B8）：全部条目 read_only=True——
    默认工具集=全量减禁止单再减 _WRITE_TOOLS；条目显式点名写工具（共用
    tools 或本条专用 tools）时整条不执行、聚合行固定回执 _FANOUT_WRITE_BLOCK，
    不静默裁剪（静默降级=模型以为写成功）。拦截粒度=单条子任务：同批其余
    条目照常并行聚合，整个调用不因一条写请求拒绝。条目执行中调用写工具
    由 allowed_tools 自然拦住、错误串回灌。

    刻意不做 worktree 模式：fan-out 定位是检索/整理/验证类杂活的并行位；
    改文件的活走 spawn_subagent(worktree=True) 或任务沙箱，改动合并要过
    确认缝——N 个并行弹窗抢单槽位确认是自找的竞态。
    """
    entries = _fanout_entries(tasks, tools)
    if not entries:
        return "错误：tasks 不能为空——给至少一件子任务"
    if len(entries) > MAX_FANOUT:
        return f"错误：一次最多 fan-out {MAX_FANOUT} 件（收到 {len(entries)} 件）——拆批再派"

    def _one(task: str, entry_tools: list[str] | None) -> tuple[RunResult, str]:
        try:
            return spawn_subagent(
                task, llm=llm, registry=registry, tools=entry_tools,
                max_rounds=max_rounds, confirm=confirm, on_event=on_event,
                should_cancel=should_cancel, read_only=True,
            )
        except Exception as exc:   # spawn 自身兜底之外的意外（与 _run_parallel 同款防线）
            return RunResult.FAILED, f"错误：子任务执行异常（{exc}）"

    # B8 派发前拦截：显式点名写工具的条目零执行（不静默裁剪），回执进聚合行
    blocked = [bool(ts and set(ts) & _WRITE_TOOLS) for _, ts in entries]
    runnable = [e for e, b in zip(entries, blocked, strict=True) if not b]
    with ThreadPoolExecutor(max_workers=max(1, len(runnable))) as ex:
        futs = [ex.submit(_one, task, entry_tools) for task, entry_tools in runnable]
        done = [f.result() for f in futs]

    ok = sum(1 for status, _ in done if status is RunResult.COMPLETED)
    lines = [f"〔fan-out 聚合：{len(entries)} 件，{ok} 完成、{len(entries) - ok} 未完成〕"]
    labels = {RunResult.COMPLETED: "完成", RunResult.CANCELLED: "取消", RunResult.FAILED: "失败"}
    results = iter(done)
    for i, ((task, _), is_blocked) in enumerate(zip(entries, blocked, strict=True), start=1):
        if is_blocked:
            lines.append(f"[{i}/{len(entries)}] 拦截｜{task[:60]}\n{_FANOUT_WRITE_BLOCK}")
            continue
        status, conclusion = next(results)
        lines.append(f"[{i}/{len(entries)}] {labels[status]}｜{task[:60]}\n{conclusion}")
    return "\n".join(lines)


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
            "有多件互不依赖的杂活要同时做时，用 fan_out_subagents 一次派发并聚合结论"
            "（或一次调用多个本工具并列点菜，它们会并行执行）。"
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

    def _fanout(tasks: list, tools: list[str] | None = None,
                max_rounds: int = DEFAULT_ROUNDS,
                confirm=None, event=None, should_cancel=None) -> str:
        return fan_out_subagents(
            tasks, llm=sub_llm, registry=registry, tools=tools,
            max_rounds=max_rounds, confirm=confirm, on_event=event,
            should_cancel=should_cancel,
        )

    registry.register(Tool(
        name="fan_out_subagents",
        description=(
            "一次派发多件互不依赖的子任务并行执行，只回传聚合结论"
            "（每件一段：成败 + 结论摘要，中间过程不打扰本对话）。"
            "适合「同一件事的多个独立切面」：并行检索、逐项验证、批量整理。"
            "tasks 是子任务书清单（每条自包含，子上下文看不到本对话）；"
            "tools/max_rounds 与 spawn_subagent 同义。"
            "条目一律只读：write_file/run_command 不可用，显式点名写工具的条目"
            "整条不执行（聚合行标「已拦截」）——要写文件的活用"
            "spawn_subagent(worktree=True) 或任务沙箱；子上下文没有本对话的历史。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "task": {"type": "string", "description": "子任务书（自包含：做什么、产出什么结论）"},
                                    "tools": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "本条子任务专用的工具名清单（缺省=共用 tools）",
                                    },
                                },
                                "required": ["task"],
                            },
                        ]
                    },
                    "description": "子任务书清单（每条自包含：做什么、产出什么结论；互不依赖）；可写纯文本，或用 {task, tools} 对象给单条指定工具",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "各子上下文共用的工具名清单（缺省=全部可用工具）",
                },
                "max_rounds": {"type": "integer", "description": "各子任务的工具循环预算（默认 3，上限 10）"},
            },
            "required": ["tasks"],
        },
        func=_fanout,
        receives_confirm=True,   # 子任务高危工具的确认缝透传（与 spawn 同款）
        receives_event=True,     # 各子任务过程以 sub.* 进父流（带 task 摘要区分）
        receives_cancel=True,    # 取消透传：fan-out 整批响应协作式取消
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
            if result_status is not RunResult.COMPLETED:
                step_status, prefix = "failed", "执行失败"
            elif not _EVIDENCE_ANCHOR_RE.search(conclusion):
                # ADR 117 §5 证据闸：空口断言不标 done。步骤层无 waiting-steer
                # 态（那是 Task 态）——等价物＝留 in_progress 挂起待人核；
                # finish_plan 会因悬空步骤拒收官，人工核过再 update_plan_step。
                board.update_step(step_id, "in_progress", note="缺可验证证据，待人工核")
                return (
                    f"步骤 #{step_id} 未标 done：子任务结论缺可验证证据"
                    "（无 checkpoint id/文件路径/run_id/会话 id 锚点），待人工核。\n"
                    f"子任务结论：\n{conclusion}\n\n"
                    f"当前计划：\n{format_view(board.view())}"
                )
            else:
                step_status, prefix = "done", "执行完成"
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
                "纪律：子任务结论须含复核方法/证据，无证据须明说（不许空口断言）。"
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
