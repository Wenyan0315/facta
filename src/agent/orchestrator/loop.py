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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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

    超限后返回最后一次泄漏消息，调用方按普通回答诚实降级——无药可救
    时把原样吐给用户（带故障文本），好过装作没事。

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
            return reply   # 超限：最后一次泄漏消息按普通回答降级
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


def _run_parallel(tool_calls: list[dict], agent: Agent, on_confirm: Callable | None) -> list[str]:
    """并行执行一批 spawn（线程池）；结果按提交顺序返回（点菜顺序=确定性）。

    spawn 是 IO-bound（子 agent 大量时间等 LLM），GIL 不碍事——线程池
    就够，不必上进程。f.result() 按 futures 提交序取，非完成序——
    结果顺序与模型点菜顺序一致（它靠位置对应 tool_call_id）。
    """
    with ThreadPoolExecutor(max_workers=len(tool_calls)) as ex:
        futures = [
            ex.submit(agent.execute, tc["name"], tc["arguments"], confirm=on_confirm)
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
        for tc in batch:
            if on_event:
                on_event("tool_started", {"name": tc["name"], "arguments": tc["arguments"]})
        # 执行：连续 spawn 段用线程池并行，其余串行
        if parallel_ok and len(batch) > 1:
            results = _run_parallel(batch, agent, on_confirm)
        else:
            results = [agent.execute(tc["name"], tc["arguments"], confirm=on_confirm) for tc in batch]
        # 按序回填（点菜顺序，确定性——模型靠位置对应 tool_call_id）
        for tc, result in zip(batch, results, strict=True):
            # S5b 针②：工具执行后立刻 drain 计划事件——在 tool_result 之前
            # 转发（plan.* 是这次执行的一部分，因果序在前）。事件走既有
            # on_event 缝，零新缝；server 侧点分命名默认透传，前端免费收到
            _forward_plan_events(session.plan, on_event)
            if on_event:
                on_event("tool_result", {"name": tc["name"], "result": result})
            # 结果以 role="tool" 回填，tool_call_id 对应是哪次调用
            tool_msg = Message(role="tool", tool_call_id=tc["id"], content=result)
            session.messages.append(tool_msg)
            payload.append(tool_msg)
    return True


def run_turn(
    session: Session,
    user_text: str,
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
        user_text   用户本轮输入（原样进底片）
        agent       执行本轮的 agent（S5a：菜单/执行/预算全从 Agent 来——
                    registry 参数退场，行为定义收口进对象；空菜单折叠回
                    None 不传，与旧 registry=None 的 API 语义逐字节对齐）
        llm         用户链（带语义档）
        summarizer  内部链（拆链：摘要压缩的内部调用不走语义档）
        on_text     流式文本块回调
        on_event    语义事件回调，type ∈：
                        tool_started  {"name": str, "arguments": str}
                        tool_result   {"name": str, "result": str}
                        max_rounds    {}       保险丝熔断，强制收尾
                        error         {"message": str}  模型全挂，本轮无产出
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
    tools = _route_first_menu(agent, user_text, schemas)

    # 1) 用户这句话存进历史（底片照常全量生长，append-only 不变）
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
        if on_event:
            on_event("max_rounds", {})
        # 最后一问不递菜单，逼它说话（同流式消费，同样罩检查点③）；
        # 泄漏守卫同款——保险丝已熔断再泄漏也无菜单可点，超限即降级
        reply = _merge_with_leak_guard(llm, payload, None, should_cancel, on_text)

        session.messages.append(reply)
        return RunResult.COMPLETED, reply   # noqa: TRY300  # 紧贴 for 收尾段陈述「保险丝收尾也入史」，不挪 else
    except _RunCancelled:
        trim_incomplete_round(session.messages)
        return RunResult.CANCELLED, None
    except LLMUnavailableError as exc:
        trim_incomplete_round(session.messages)
        if on_event:
            on_event("error", {"message": str(exc)})
        return RunResult.FAILED, None
