"""Agent 主循环内核：run_turn —— 把一行用户输入跑成完整一轮。

内核与外设分离（S2a）：本文件只做「一轮对话」的决策与执行，所有对外 I/O
通过两条缝注入，函数体里一次都不出现 input / print——内核不知道自己在
CLI 还是 Web 里跑。

两条缝：
- on_text(str)              流式文本块（CLI = print(text, end="")，Web = SSE chunk）
- on_event(type, data)      语义事件（CLI = 映射 print，Web = 映射 SSE event）
  事件类型见 run_turn docstring。

分层向：本模块此前住在 core/（core 却 import 了 memory/tools，依赖倒置）。
S2a 把它迁到编排层 orchestrator/，core/ 收缩为纯地基。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from enum import Enum

from agent.core.llm import LLM, LLMUnavailableError, merge_stream_chunks
from agent.core.types import Message
from agent.memory.compressor import build_payload, maybe_compress, trim_incomplete_round
from agent.memory.plan import PlanBoard
from agent.memory.store import Session
from agent.orchestrator.agent import Agent
from agent.tools.plan import format_view

logger = logging.getLogger(__name__)

# S6b 并行 spawn：唯一「设计上可证明安全」的并行工具（独立 Session +
# worktree 隔离、IO-bound）。普通工具保持串行——模型常期待「先读 A 再
# 决定读 B」，并行会打乱它的预期顺序（保守默认，与 needs_confirmation 同哲学）
_SPAWN_TOOL = "spawn_subagent"

# （S5a）SYSTEM_PROMPT 已搬家：行为定义从引擎代码搬进 Agent 对象
# （agent.py::DEFAULT_SYSTEM_PROMPT）——行为定义与执行引擎分离；
# _MAX_TOOL_ROUNDS 同步退场，保险丝成为 Agent.max_tool_rounds 属性。

_WEEKDAYS = "一二三四五六日"

# DSML 泄漏（S6c 实机验收抓到）：deepseek-flash 偶发把内部函数调用格式
# 裸文本吐进 content，未被解析成合法 tool_calls——工具调用意图丢失
# （实机样本：`<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="finish_plan">…`，
# 全角竖线）。与 Qwen thinking 通道 / Gemma 空 content 同族：模型输出
# 格式故障，ScriptedLLM 测不出。标记做元组——将来别家模型的泄漏标记可加
_DSML_LEAK_MARKERS = ("<｜｜DSML｜｜",)
# 泄漏重试上限（独立计数，不吃 rounds 预算）：防点菜上瘾的保险丝不该
# 被格式故障消耗——模型病了不该扣它的行动额度
_DSML_LEAK_RETRIES = 2
_DSML_LEAK_HINT = (
    "检测到刚才的输出把内部函数调用格式当作正文文本吐出，该调用并未被执行、"
    "意图已丢失。请重试：如需调用工具，通过标准 tool_calls 字段发起；"
    "如需作答，直接输出自然语言。正文中不要出现 <｜｜DSML｜｜ 等任何调用格式。"
)
# 降级时只从 markup 里提工具名、不提参数（ADR 047 拍板 3）：参数里可能装着
# 几百字未执行成功的正文（实机样本：write_note 的整篇笔记），复述进告知
# 等于把「没做成的事」当交付物端给用户
_DSML_INVOKE_RE = re.compile(r'<｜｜DSML｜｜\s+invoke\s+name="([^"]+)"')

# 收尾段（ADR 056）：菜单与配套告知的取值，语义见 _close_out。
# 只递 finish_plan 等于递一条必被终态闸拒的菜（memory/plan.py 的 finish
# 要求全部步骤终态化），所以 update_plan_step 必须同行——菜单与前置条件
# 也要配套，这与「菜单与告知配套」是同一类缺陷
_CLOSING_TOOLS = ("update_plan_step", "finish_plan")
_CLOSING_HINT = (
    "工具预算已用完，{menu}"
    "请直接用自然语言总结：已经完成了什么、哪些没做完、为什么。"
    "正文中不要出现任何函数调用格式。"
)
_MENU_GONE = "本轮不会再执行任何工具调用。"
_MENU_PLAN_ONLY = (
    "本轮仍可为计划收官：先用 update_plan_step 把未终态步骤标 skipped 或 "
    "failed（终态必须带 note），再调 finish_plan，两者可在同一批里发。"
    "除此之外不会再执行任何工具。"
)

# P0-6 无进展检测上限（LongHorizon 基线 agent 对无响应弹窗原地重试 400+
# 步的形态）：连续完全相同的点菜批次达上限即熔断工具循环、事件升人审。
# rounds 保险丝管「点太多」，这里管「原地踏步」——每次执行都「成功」、
# 状态零变化、token 白烧。下限 2（=重复 1 次即熔断无意义，留给误报空间）
_STUCK_LIMIT = max(2, int(os.environ.get("CORTEX_STUCK_LIMIT", "3")))


def _stuck_check(
    reply: Message, prev_sig: tuple[tuple[str, str], ...] | None, streak: int
) -> tuple[tuple[tuple[str, str], ...], int, bool]:
    """P0-6 原地踏步判定：本批点菜签名 →（新签名, 新计数, 是否熔断）。

    签名 = 本批全部点菜的（工具名, 参数）序列；与上一批完全相同才算踏步
    （id 不算数）。判定在 reply 入史之前调用——熔断轮的点菜不进底片
    （进了就是孤儿 tool_calls，污染后续投影）。
    """
    sig = tuple((tc["name"], tc["arguments"]) for tc in reply.tool_calls or [])
    streak = streak + 1 if sig == prev_sig else 0
    return sig, streak, streak >= _STUCK_LIMIT - 1


class RunResult(Enum):
    """run_turn 的终态枚举——替代 None 二义性（S4 评审 #3）。

    三个值互斥，调用方一眼看清本轮怎么结束的：
    - COMPLETED  正常结束，reply 里有最终 assistant 回答
    - CANCELLED  用户取消（should_cancel 命中），半截轮已掐
    - FAILED     模型全挂（LLMUnavailableError），错误已发 error 事件
    """
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class _RunCancelled(Exception):
    """内部信号：流式消费中途被取消（检查点③），用于从生成器深处跳出。

    不外发事件——cancelled 是 Run 级终态，归调用方（同检查点①②的约定）。
    """


def _cancel_aware_stream(chunks, should_cancel):
    """把底层流包成「每块到手前查取消」的流（协作式取消检查点③）。

    检查点①②要等模型调用/工具执行的自然边界，模型一次生成几十秒时
    用户点取消要干等——检查点③在流式生成中每块到手时检查，命中则
    close() 底层生成器（触发其清理逻辑收连接）并上抛 _RunCancelled。
    should_cancel 为 None 时零行为差异（CLI 键盘中断通道不变）。
    """
    for chunk in chunks:
        if should_cancel is not None and should_cancel():
            chunks.close()
            raise _RunCancelled()
        yield chunk


def _is_dsml_leak(reply: Message) -> bool:
    """merge 后判定：tool_calls 为空但 content 含内部调用格式标记 = 泄漏。

    工具调用意图全在泄漏文本里，解析层一个都没接住——这条消息既不能
    当最终回答（用户看到裸格式串），更不能入底片（会教坏后续轮次）。
    """
    if reply.tool_calls:
        return False
    content = reply.content or ""
    return any(m in content for m in _DSML_LEAK_MARKERS)


def _dsml_leak_notice(lost: list[str]) -> str:
    """降级告知文案——不得含泄漏标记字面量（ADR 047 拍板 4）。

    写了字面量，落盘记录里就会再出现标记，任何自动化扫描都会把这条
    已降级的消息又判成泄漏；用「内部函数调用格式」指代即可。
    """
    text = "【输出格式故障】模型把内部函数调用格式当作正文吐出，该段已丢弃。"
    if lost:
        text += f"未能执行的工具调用：{'、'.join(lost)}，其意图已丢失。"
    return text + f"自动重试 {_DSML_LEAK_RETRIES} 次未恢复，请重新下达指令。"


def _salvage_dsml_leak(reply: Message, on_text: Callable[[str], None] | None) -> Message:
    """超限降级：截断泄漏 markup、保住前面的正文、追加故障告知（ADR 047 甲案）。

    截断点 = 第一个标记出现的位置。真实样本有两型（047 地面真值表，8 条）：
    - 纯 markup 型（7 条）：整条回答只有泄漏格式，正文全塞在未执行的调用
      参数里 → 截断后只剩告知。丢的是「没做成的事的参数」，不是交付物。
    - 混合型（1 条）：前半是完好的自然语言回答（实机样本：五日行情表 +
      周变动分析），「现在写入笔记：」之后才接泄漏的 write_note →
      保住截断点之前的正文，一次局部故障才不会升级成整轮零交付。

    不解析 markup 补执行（丙案已否决）：那是第二套 tool-call 解析器，且
    执行的是解析层已经拒绝过的意图，安全面变大。

    replace 而非原地改：usage（token 账目）要跟着走，调用方手里的引用也
    不该被悄悄改写。
    """
    content = reply.content or ""
    cut = min(
        (content.index(m) for m in _DSML_LEAK_MARKERS if m in content),
        default=len(content),
    )
    keep = content[:cut].rstrip()
    # 去重保序：同一工具泄漏多次只报一次名字
    lost = list(dict.fromkeys(_DSML_INVOKE_RE.findall(content[cut:])))
    notice = _dsml_leak_notice(lost)
    if on_text:
        # 已外发的泄漏块收不回来（流式固有 tradeoff），告知同样走 on_text
        # 才能让流式视图与落盘至少「都说明了故障」
        on_text("\n\n" + notice + "\n")
    return replace(reply, content=f"{keep}\n\n{notice}" if keep else notice)


def _merge_with_leak_guard(
    llm: LLM,
    payload: list[Message],
    tools: list[dict] | None,
    should_cancel: Callable[[], bool] | None,
    on_text: Callable[[str], None] | None,
) -> Message:
    """merge + DSML 泄漏检测/重试（工具循环与收尾段共用，方案 a 拍板）。

    处置三件套：
    - 泄漏消息不入底片（调用方 append 的是本函数返回的最终版）——trim
      同理由：坏消息进历史会被摘要吸收，毒害后续行为
    - 重试提示注入投影尾部——投影本轮作废，提示随轮蒸发，不进底片
    - 重试独立计数上限 _DSML_LEAK_RETRIES，不吃 rounds 预算

    超限后不再原样返回（ADR 047 甲案推翻 033 的「原样吐出」）：截掉泄漏
    markup、保住标记之前的正常正文、追加明确故障告知——诚实降级仍成立
    （不装作没事、明说哪个工具没执行成），但用户不必再从裸格式串里刨内容。

    已外发的流式块收不回来（检查点③的流是边收边喂 on_text 的）——
    用户会看到泄漏文本闪现后跟着正常回答，这是流式的固有 tradeoff，
    比缓冲整个流式的替代方案（牺牲首字延迟）便宜得多。
    """
    leaks = 0
    while True:
        reply = merge_stream_chunks(
            _cancel_aware_stream(llm.generate_stream(payload, tools), should_cancel),
            on_text=on_text,
        )
        if not _is_dsml_leak(reply):
            return reply
        leaks += 1
        if leaks > _DSML_LEAK_RETRIES:
            return _salvage_dsml_leak(reply, on_text)   # 超限：截断 + 告知（ADR 047）
        if on_text:
            on_text("\n【检测到输出格式故障，正在自动重试…】\n")
        payload.append(Message(role="system", content=_DSML_LEAK_HINT))


def _plan_stamp(board: PlanBoard) -> Message | None:
    """活跃计划注入投影（S5b 针①，时间戳同款手法：进投影不进底片）。

    位置固定：时间戳之后、摘要/对话之前——「今天几号」和「任务进行到哪」
    都属于本轮视野。无活跃计划返回 None：不注入任何东西，简单任务的
    上下文零开销（轮首快照——同轮内多步导航靠 update_plan_step 的
    工具结果回灌带最新视图，双视图分工）。
    """
    view = board.view()
    if view is None:
        return None

    return Message(
        role="system",
        content=(
            "【当前任务计划】以下任务正在进行，按计划继续执行；"
            "步骤状态变化用 update_plan_step 回写（终态必带 note），"
            "计划过时用 make_plan 修订（reason 必填），全部终态后 finish_plan 收官。\n"
            + format_view(view)
        ),
    )


def _forward_plan_events(board: PlanBoard, on_event: Callable[[str, dict], None] | None) -> None:
    """drain 计划事件并转发（S5b 针②的函数体）。

    无 on_event 也 drain——清队列防陈旧事件跨轮堆积（测试/纯文本场景
    产生的 plan 事件不能攒到下次有监听时一起冒出来）。
    """
    events = board.drain()
    if on_event is not None:
        for ev in events:
            on_event(ev.type, ev.data)


def _time_stamp(now: datetime | None = None) -> Message:
    """当前时间戳（投影专用，绝不入底片）。

    为什么要它（真实使用经验逼出来的）：跨会话恢复时，模型没有「现在」的
    概念，会拿上次对话的时间当锚点，安静地算错一切相对时间——"更新数据"
    取到半个月前的日期还不报错。时间戳管「今天是哪天」这个锚点；
    get_current_time 工具继续管秒级精度与未来时间点。

    进投影不进底片的理由：时间属于「本轮视野」而非「对话内容」——
    入底片会堆日期垃圾、被摘要吸收；投影每轮现切、随轮作废。
    now 参数留给测试注入固定时刻。
    """
    now = now or datetime.now()
    return Message(
        role="system",
        content=f"今天：{now:%Y-%m-%d}（周{_WEEKDAYS[now.weekday()]}）{now:%H:%M}",
    )


def _route_first_menu(agent: Agent, user_text: str, schemas: list[dict]) -> list[dict] | None:
    """M10 场景路由（轮首一针，只影响本轮第一次模型调用）。

    direct      → None——省全部菜单 token，且纯聊天流量因此落进
                  SemanticCacheLLM 的命中区（它只在 tools=None 时生效，免费放大既有基建）
    single_tool → 只递该工具 schema——选择权已由 Jev 行使，LLM 只填参数
                  （单工具菜单即全部强制力，不用 tool_choice 强制——那会堵死
                  Jev 误判时模型直答的逃生门）；Jev 选的名字不在菜单 → 回退全量
    complex     → 全量菜单——模型自己走 S5b make_plan
    无路由（无 key 装配缺席 / 故障降级 / 熔断跳过）→ 原生路径（v0.57 行为）
    """
    if agent.router is None:
        return schemas or None
    decision = agent.router.route(user_text)
    if decision is None:
        return schemas or None
    if decision.kind == "direct":
        return None
    if decision.kind == "single_tool" and decision.tool is not None:
        single = [s for s in schemas if s["function"]["name"] == decision.tool]
        return single or (schemas or None)
    return schemas or None   # complex


def _split_tool_batches(tool_calls: list[dict]) -> list[tuple[bool, list[dict]]]:
    """把一轮 tool_calls 切成批：连续 spawn 段 = 可并行批（True），
    其余逐个 = 串行批（False）。

    只对「连续 spawn」开并行——穿插的普通工具拆成单元素串行批，保持
    原顺序。结果按批顺序回填，模型看到的顺序与点菜顺序一致。
    """
    batches: list[tuple[bool, list[dict]]] = []
    i = 0
    n = len(tool_calls)
    while i < n:
        if tool_calls[i]["name"] == _SPAWN_TOOL:
            j = i
            while j < n and tool_calls[j]["name"] == _SPAWN_TOOL:
                j += 1
            batches.append((True, tool_calls[i:j]))
            i = j
        else:
            batches.append((False, [tool_calls[i]]))
            i += 1
    return batches


def _run_parallel(
    tool_calls: list[dict],
    agent: Agent,
    on_confirm: Callable | None,
    on_event: Callable | None,
) -> list[str]:
    """并行执行一批 spawn（线程池）；结果按提交顺序返回（点菜顺序=确定性）。

    spawn 是 IO-bound（子 agent 大量时间等 LLM），GIL 不碍事——线程池
    就够，不必上进程。f.result() 按 futures 提交序取，非完成序——
    结果顺序与模型点菜顺序一致（它靠位置对应 tool_call_id）。

    059：on_event 一并下发——各 worker 线程内的子 agent 事件会**交织**
    写进同一条父流（谁先跑完谁先到），故子事件带 task 摘要用于区分兄弟；
    emit 侧的序号原子性由 RunStore 的锁保证（共享收口点，一处修）。
    """
    with ThreadPoolExecutor(max_workers=len(tool_calls)) as ex:
        futures = [
            ex.submit(
                agent.execute,
                tc["name"],
                tc["arguments"],
                confirm=on_confirm,
                on_event=on_event,
            )
            for tc in tool_calls
        ]
        results: list[str] = []
        for f in futures:
            try:
                results.append(f.result())
            except Exception as exc:  # agent.execute 已兜底（registry 返回错误串），这里是意外
                results.append(f"错误：并行执行失败（{exc}）")
        return results


def _execute_tool_calls(
    tool_calls: list[dict],
    session: Session,
    payload: list[Message],
    agent: Agent,
    on_confirm: Callable | None,
    on_event: Callable | None,
    should_cancel: Callable | None,
) -> bool:
    """执行一轮的全部工具调用（S6b 切批：连续 spawn 段并行，其余串行）。

    结果按点菜顺序回填（tool 消息与 tool_call_id 一一对应，模型靠位置认）。
    返回 False = 取消命中（已 trim 半截轮），调用方应返回 CANCELLED。
    """
    for parallel_ok, batch in _split_tool_batches(tool_calls):
        # 协作式取消检查点②：每个批执行前（批粒度，非逐工具）
        if should_cancel and should_cancel():
            trim_incomplete_round(session.messages)
            return False
        # tool_started：并行批先全发（表示都开始了），串行批逐发
        # id（P0-3）：tool_call id 随事件外发——checkpoint 账本靠它把
        # 「点了什么菜」与「回了什么结果」配对，恢复时才能按 id 回注
        for tc in batch:
            if on_event:
                on_event("tool_started", {
                    "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"],
                })
        # 执行：连续 spawn 段用线程池并行，其余串行
        # 059：on_event 顺着 agent.execute 往下走，声明 receives_event 的
        # 工具（spawn 两件）拿到父事件缝，把子 agent 过程以 sub.* 转出来
        if parallel_ok and len(batch) > 1:
            results = _run_parallel(batch, agent, on_confirm, on_event)
        else:
            results = [
                agent.execute(
                    tc["name"], tc["arguments"], confirm=on_confirm, on_event=on_event
                )
                for tc in batch
            ]
        # 按序回填（点菜顺序，确定性——模型靠位置对应 tool_call_id）
        for tc, result in zip(batch, results, strict=True):
            # 结果以 role="tool" 回填，tool_call_id 对应是哪次调用
            tool_msg = Message(role="tool", tool_call_id=tc["id"], content=result)
            session.messages.append(tool_msg)
            payload.append(tool_msg)
            # 入史必须早于事件外发（P0-3 不变量：事件一旦外发，底片里已经
            # 有这件事）。checkpoint writer 挂在 on_event 缝上落盘 session，
            # 顺序反了就会存出「缺最后一条 tool 消息」的底片，白丢一次结果。
            #
            # S5b 针②：工具执行后立刻 drain 计划事件——在 tool_result 之前
            # 转发（plan.* 是这次执行的一部分，因果序在前）。事件走既有
            # on_event 缝，零新缝；server 侧点分命名默认透传，前端免费收到
            _forward_plan_events(session.plan, on_event)
            if on_event:
                on_event("tool_result", {"id": tc["id"], "name": tc["name"], "result": result})
    return True


def _close_out(
    llm: LLM,
    payload: list[Message],
    schemas: list[dict],
    session: Session,
    agent: Agent,
    on_confirm: Callable | None,
    on_event: Callable | None,
    should_cancel: Callable | None,
    on_text: Callable[[str], None] | None,
) -> tuple[RunResult, Message | None]:
    """收尾段（ADR 056）：保险丝熔断后的最后一问。

    这里原本恒 tools=None（「最后一问不递菜单，逼它说话」），但模型此刻的
    意图往往正是调 finish_plan 收官——请求里没有 tools 字段 ⇒ 它结构上无法
    产出 tool_calls，只能用训练时学到的文本格式把调用吐进 content（DSML
    泄漏）。重试也救不回：_merge_with_leak_guard 的 tools 参数在 while 里
    不变，而 _DSML_LEAK_HINT 要求「通过标准 tool_calls 字段发起」——一条
    结构上不存在的出路（047 记的「重试 2 次全无效」是死锁，不是采样噪声）。

    修法两半，且**菜单与告知必须配套**（不配套正是病根）：
    - 认知：生成前就说清预算已尽，不再等泄漏之后事后教训
    - 结构：只留收官这两个菜（update_plan_step + finish_plan），让计划板能
      被正常关闭。选它们的理由是意图丢失代价最大（板子挂在 active 会污染
      后续每一轮的投影），且都是幂等的收官动作、不开新战线。两个都要递：
      实机（2026-09-27 定向跑 r4）显示只递 finish_plan 时模型点了它、被
      终态闸拒（有步骤悬空），而补终态的工具已不在菜单里 ⇒ 板子照样挂在
      active，正是本修法要消除的污染。无活跃计划 / 菜单里一个都没有 →
      照旧全撤

    菜单收窄不是安全边界：收官点菜照走 needs_confirmation 的 L2 掌舵点，
    也照走取消检查点与事件缝（复用 _execute_tool_calls，不另起一套）。
    """
    closing: list[dict] | None = None
    if session.plan.active is not None:
        closing = [s for s in schemas if s["function"]["name"] in _CLOSING_TOOLS] or None
    payload.append(Message(
        role="system",
        content=_CLOSING_HINT.format(
            menu=_MENU_PLAN_ONLY if closing is not None else _MENU_GONE
        ),
    ))
    reply = _merge_with_leak_guard(llm, payload, closing, should_cancel, on_text)
    if not reply.tool_calls:            # 没点收官菜 → 这就是最终回答
        session.messages.append(reply)
        return RunResult.COMPLETED, reply

    session.messages.append(reply)
    payload.append(reply)
    if not _execute_tool_calls(
        reply.tool_calls, session, payload, agent, on_confirm, on_event, should_cancel
    ):
        return RunResult.CANCELLED, None
    # 收官跑完再撤菜单逼文字总结，并钉一条新告知盖掉上一条「仍可收官」
    # ——否则模型会再点一次，而这次没有 tools 字段可承载
    payload.append(Message(role="system", content=_CLOSING_HINT.format(menu=_MENU_GONE)))
    final = _merge_with_leak_guard(llm, payload, None, should_cancel, on_text)
    session.messages.append(final)
    return RunResult.COMPLETED, final


def run_turn(
    session: Session,
    user_text: str | None,
    *,
    agent: Agent,
    llm: LLM,
    summarizer: LLM | None = None,
    on_text: Callable[[str], None] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_confirm: Callable[[str, dict], bool] | None = None,
) -> tuple[RunResult, Message | None]:
    """跑一轮对话：用户消息入底片 → 投影 →（摘要）→ 工具循环 → 收尾。

    参数：
        session     会话状态（原地变异，不 rebind——见列表身份陷阱）
        user_text   用户本轮输入（原样进底片）。
                    None = 续跑（P0-3 崩溃恢复）：不追加新用户消息，直接从
                    底片现状接着跑——调用方须先 heal 补齐悬挂的工具轮次，
                    否则底片里的孤儿 tool_calls 会被 API 拒收。
        agent       执行本轮的 agent（S5a：菜单/执行/预算全从 Agent 来——
                    registry 参数退场，行为定义收口进对象；空菜单折叠回
                    None 不传，与旧 registry=None 的 API 语义逐字节对齐）
        llm         用户链（带语义档）
        summarizer  内部链（拆链：摘要压缩的内部调用不走语义档）
        on_text     流式文本块回调
        on_event    语义事件回调，type ∈：
                        tool_started  {"id": str, "name": str, "arguments": str}
                        tool_result   {"id": str, "name": str, "result": str}
                        （id = tool_call id，P0-3 checkpoint 账本靠它配对意图与结果）
                        max_rounds    {}       保险丝熔断，强制收尾
                        stuck         {"tools": [str]}  P0-6 原地踏步熔断，升人审
                        error         {"message": str}  模型全挂，本轮无产出
                        sub.*         spawn 的子 agent 过程事件（059）：上面五种
                                      各有一个 sub. 前缀版，data 额外带 task 摘要
                                      （并行兄弟靠它区分）。隔离的是主 agent 上下文，
                                      不是人的眼睛；不进 checkpoint 账本（writer 只认
                                      精确类型），故恢复语义与评测轨迹口径都不受影响
        should_cancel 协作式取消检查点回调：返回 True 时在下一个检查点掐半截轮、
                      返回 (CANCELLED, None)（不发事件——cancelled 是 Run 级终态，归调用方）。
                      检查点粒度 = 每次模型调用前（①）+ 流式生成中每块到手时（③，
                      即时生效）+ 每次工具执行前（②）。CLI 传 None 走键盘中断。
        on_confirm  L2 确认缝（S4b）：工具标了 needs_confirmation 时透传给
                    registry.execute 裁决（名字+参数 → 批准/拒绝）。CLI 挂
                    input()，Web 挂 Event.wait()；None=无确认通道，标确认的
                    工具一律按拒绝处理（保守默认）

    返回：
        (RunResult, Message | None)  终态 + 本轮最终 assistant 回答（仅 COMPLETED 时非 None）
        三个终态互斥，调用方不再猜 None 的含义：
        - COMPLETED  正常结束，reply 非 None
        - CANCELLED  用户取消，reply=None
        - FAILED     模型全挂（已发 error 事件），reply=None

    不碰文件、不碰 input/print：落盘归装配层，I/O 归调用方的两条缝。
    """
    summarizer = summarizer or llm
    # 菜单只在本轮生成一次，工具循环全程复用同一版 schema。
    # 空菜单折叠回 None：与旧「registry=None 不传菜单」的 API 语义逐字节
    # 对齐——tools=None（省略字段=无工具能力）与 tools=[]（有工具能力但
    # 清单为空）在 OpenAI 兼容 API 里语义不保证等价，不赌供应商实现
    schemas = agent.schemas()
    full_tools = schemas or None
    # M10 轮首一针：路由决策收口在 _route_first_menu（语义见其 docstring）；
    # 只影响本轮第一次模型调用，工具结果回灌后循环尾归还全量菜单（半路由）
    # P0-3 续跑（user_text=None）：路由照做，任务原文从底片里最后一条 user
    # 消息取——那就是本次崩溃前正在做的事
    route_text = user_text if user_text is not None else next(
        (m.content for m in reversed(session.messages) if m.role == "user"), ""
    )
    tools = _route_first_menu(agent, route_text, schemas)

    # 1) 用户这句话存进历史（底片照常全量生长，append-only 不变）
    #    续跑时不追加：底片里那句 user 消息已经在，再塞一条就是重复提问
    if user_text is not None:
        session.messages.append(Message(role="user", content=user_text))

    # 2) 发送前投影：触发式摘要（内部调用）→ 切 payload
    #    压缩缓存记在 Session 上——随底片一起落盘，重启不从头再压
    # 3) 工具循环：决策 → 执行 → 观察 → 再决策（M5 心脏）
    # 4) 收尾：最终回答只入底片（投影本轮作废，下轮重切）
    # —— 2/3/4 都罩在 LLMUnavailableError 保护下（M7.5d F 契约）：
    #    模型全挂 → 优雅结束本轮而非崩溃；半截工具轮掐掉（防孤儿 tool
    #    落盘）；用户消息留在底片；错误经 on_event 外发、不进历史
    try:
        session.summary, session.summarized_upto = maybe_compress(
            summarizer, session.messages, session.summary, session.summarized_upto
        )
        payload = build_payload(session.messages, session.summary, session.summarized_upto)
        # 时间锚点注入投影（不入底片）：位置固定在第 2 条（system 之后、
        # 摘要/对话之前）；本轮工具循环共享同一个时间戳
        payload.insert(1, _time_stamp())
        # 活跃计划注入投影（S5b 针①，不入底片）：时间戳之后；无活跃计划
        # 返回 None 不注入——简单任务上下文零开销
        plan_msg = _plan_stamp(session.plan)
        if plan_msg is not None:
            payload.insert(2, plan_msg)

        # P0-6 无进展检测的状态：上一批点菜签名 + 连续重复计数（轮级局部，
        # 一轮对话结束即弃——检出的是「这一轮内原地踏步」，跨轮重复归人管）
        prev_batch_sig: tuple[tuple[str, str], ...] | None = None
        stuck_streak = 0
        stuck = False
        for _round in range(agent.max_tool_rounds):
            # 协作式取消检查点①：每次模型调用前。取消则掐半截轮、本轮无产出
            if should_cancel and should_cancel():
                trim_incomplete_round(session.messages)
                return RunResult.CANCELLED, None

            # 流式消费：分片边收边喂 on_text，收完 merge 拼回完整回复。
            # 点菜轮 content 通常为空（不冒字），模型偶尔先冒半句再点菜。
            # 泄漏守卫罩在外面：DSML 泄漏 → 不入史、提示重试（上限 2 次）
            reply = _merge_with_leak_guard(llm, payload, tools, should_cancel, on_text)

            if not reply.tool_calls:   # 模型不点菜了 → 最终回答，退出循环
                session.messages.append(reply)
                return RunResult.COMPLETED, reply

            # P0-6 熔断判定在 reply 入史之前（语义见 _stuck_check）
            prev_batch_sig, stuck_streak, should_break = _stuck_check(
                reply, prev_batch_sig, stuck_streak
            )
            if should_break:
                stuck = True
                if on_event:
                    on_event("stuck", {"tools": [tc["name"] for tc in reply.tool_calls]})
                break

            # 模型点菜了。原「assert registry is not None」已删（S5a）：
            # 菜单为 None 时模型仍幻觉点菜是真实可能——agent.execute 走
            # registry「工具不存在」路径回错误串，模型下一轮自纠——
            # 反馈环统一接管，不再整轮炸掉（M5「错误也返回字符串」惯例
            # 从 registry 层延伸到内核层，行为升级点见 027）

            # 双写：底片入史（落盘用）+ 投影同步（本轮内模型必须看得见）
            session.messages.append(reply)
            payload.append(reply)

            # S6b 切批执行：连续 spawn 段并行（线程池），普通工具串行。
            # 结果仍按点菜顺序回填——并行只是「怎么跑」变了，「模型看到
            # 什么」与串行逐字节一致（外部行为不变，冒烟套件把关）。
            if not _execute_tool_calls(
                reply.tool_calls, session, payload, agent, on_confirm, on_event, should_cancel
            ):
                return RunResult.CANCELLED, None
            # M10 半路由归还：第一次模型调用结束后，菜单恢复全量——工具结果
            # 已回灌，循环决策权归还模型（bench 三层分解：Jev 管第一步，
            # 循环内决策归模型/harness）。direct 场景模型直答即 return，到不了这里
            tools = full_tools
        # for 循环跑满都没 break（模型点菜上瘾）→ 强制收尾
        # （P0-6 原地踏步熔断已自带 stuck 事件，不再发 max_rounds 噪声）
        if not stuck and on_event:
            on_event("max_rounds", {})
        # 收尾段整块交给 _close_out（ADR 056：菜单与告知必须配套，见其 docstring）
        return _close_out(   # noqa: TRY300  # 紧贴 for 收尾段陈述「保险丝收尾也入史」，不挪 else
            llm, payload, schemas, session, agent,
            on_confirm, on_event, should_cancel, on_text,
        )
    except _RunCancelled:
        trim_incomplete_round(session.messages)
        return RunResult.CANCELLED, None
    except LLMUnavailableError as exc:
        trim_incomplete_round(session.messages)
        if on_event:
            on_event("error", {"message": str(exc)})
        return RunResult.FAILED, None
