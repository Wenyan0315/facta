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
from datetime import datetime
from enum import Enum

from agent.core.llm import LLM, LLMUnavailableError, merge_stream_chunks
from agent.core.types import Message
from agent.memory.compressor import build_payload, maybe_compress, trim_incomplete_round
from agent.memory.store import Session
from agent.orchestrator.agent import Agent

logger = logging.getLogger(__name__)

# （S5a）SYSTEM_PROMPT 已搬家：行为定义从引擎代码搬进 Agent 对象
# （agent.py::DEFAULT_SYSTEM_PROMPT）——行为定义与执行引擎分离；
# _MAX_TOOL_ROUNDS 同步退场，保险丝成为 Agent.max_tool_rounds 属性。

_WEEKDAYS = "一二三四五六日"


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
    tools = schemas or None

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

        for _round in range(agent.max_tool_rounds):
            # 协作式取消检查点①：每次模型调用前。取消则掐半截轮、本轮无产出
            if should_cancel and should_cancel():
                trim_incomplete_round(session.messages)
                return RunResult.CANCELLED, None

            # 流式消费：分片边收边喂 on_text，收完 merge 拼回完整回复。
            # 点菜轮 content 通常为空（不冒字），模型偶尔先冒半句再点菜
            reply = merge_stream_chunks(
                _cancel_aware_stream(llm.generate_stream(payload, tools), should_cancel),
                on_text=on_text,
            )

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

            for tc in reply.tool_calls:   # 模型一次可能点多个菜
                # 协作式取消检查点②：每次工具执行前
                if should_cancel and should_cancel():
                    trim_incomplete_round(session.messages)
                    return RunResult.CANCELLED, None
                if on_event:
                    on_event("tool_started", {"name": tc["name"], "arguments": tc["arguments"]})
                result = agent.execute(tc["name"], tc["arguments"], confirm=on_confirm)
                if on_event:
                    on_event("tool_result", {"name": tc["name"], "result": result})
                # 结果以 role="tool" 回填，tool_call_id 对应是哪次调用
                tool_msg = Message(role="tool", tool_call_id=tc["id"], content=result)
                session.messages.append(tool_msg)
                payload.append(tool_msg)
        # for 循环跑满都没 break（模型点菜上瘾）→ 强制收尾
        if on_event:
            on_event("max_rounds", {})
        # 最后一问不递菜单，逼它说话（同流式消费，同样罩检查点③）
        reply = merge_stream_chunks(
            _cancel_aware_stream(llm.generate_stream(payload, None), should_cancel),
            on_text=on_text,
        )

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
