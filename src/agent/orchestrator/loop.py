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

from datetime import datetime
from typing import Callable

from agent.core.llm import LLM, LLMUnavailableError, merge_stream_chunks
from agent.core.types import Message
from agent.memory.compressor import build_payload, maybe_compress, trim_incomplete_round
from agent.memory.store import Session
from agent.tools.registry import ToolRegistry

SYSTEM_PROMPT = (
    "你是一个 AI 学习助手，帮助我学习 AI Agent 开发。"
    "信息使用政策（按优先级）："
    "①优先用 search_notes 检索我的个人知识库，基于笔记回答；"
    "②资料不足时，可用其他工具（如读取完整笔记）补充；"
    "③以上都没有时，用你自己的知识回答，"
    "但必须标注「以下来自我的通用知识，非笔记内容」。"
    "需要事实信息（比如当前时间）时，主动使用工具获取。"
    "你的历史对话由系统自动保存、跨重启恢复——恢复的历史与当前对话属于"
    "同一个持续会话；用户说'这轮对话''这轮对话''我们聊过的'时，指含恢复历史的"
    "整个会话，而非最近一次问答。历史过长时自动压缩为摘要；"
    "摘要中的信息等同于你的亲历记忆，可直接引用，不要声称自己记不住。"
    "需要早前对话的逐字原话时，用 search_history 检索完整历史。"
)

# 工具循环保险丝：模型理论上可能一直点菜不收敛，永远要给循环设上限
_MAX_TOOL_ROUNDS = 5

_WEEKDAYS = "一二三四五六日"


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
    llm: LLM,
    registry: ToolRegistry | None,
    summarizer: LLM | None = None,
    on_text: Callable[[str], None] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
) -> Message | None:
    """跑一轮对话：用户消息入底片 → 投影 →（摘要）→ 工具循环 → 收尾。

    参数：
        session     会话状态（原地变异，不 rebind——见列表身份陷阱）
        user_text   用户本轮输入（原样进底片）
        llm         用户链（带语义档）
        registry    工具注册表；None 时模型无菜单可点（纯文本测试场景）
        summarizer  内部链（拆链：摘要压缩的内部调用不走语义档）
        on_text     流式文本块回调
        on_event    语义事件回调，type ∈：
                        tool_started  {"name": str, "arguments": str}
                        tool_result   {"name": str, "result": str}
                        max_rounds    {}       保险丝熔断，强制收尾
                        error         {"message": str}  模型全挂，本轮无产出

    返回：
        Message   本轮最终 assistant 回答（已入底片）
        None      模型不可用（LLMUnavailableError 已被捕获：掐半截轮、user 消息留底片、
                  发 error 事件）——调用方据此决定「跳过本轮」或提示恢复

    不碰文件、不碰 input/print：落盘归装配层，I/O 归调用方的两条缝。
    """
    summarizer = summarizer or llm
    # 菜单只在本轮生成一次，工具循环全程复用同一版 schema
    tools = registry.schemas() if registry else None

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

        for _round in range(_MAX_TOOL_ROUNDS):
            # 流式消费：分片边收边喂 on_text，收完 merge 拼回完整回复。
            # 点菜轮 content 通常为空（不冒字），模型偶尔先冒半句再点菜
            reply = merge_stream_chunks(
                llm.generate_stream(payload, tools),   # 发的是投影，不是底片
                on_text=on_text,
            )

            if not reply.tool_calls:   # 模型不点菜了 → 最终回答，退出循环
                session.messages.append(reply)
                return reply

            # 双写：底片入史（落盘用）+ 投影同步（本轮内模型必须看得见）
            session.messages.append(reply)
            payload.append(reply)

            for tc in reply.tool_calls:   # 模型一次可能点多个菜
                if on_event:
                    on_event("tool_started", {"name": tc["name"], "arguments": tc["arguments"]})
                result = registry.execute(tc["name"], tc["arguments"])
                if on_event:
                    on_event("tool_result", {"name": tc["name"], "result": result})
                # 结果以 role="tool" 回填，tool_call_id 对应是哪次调用
                tool_msg = Message(role="tool", tool_call_id=tc["id"], content=result)
                session.messages.append(tool_msg)
                payload.append(tool_msg)
        else:
            # for 循环跑满都没 break（模型点菜上瘾）→ 强制收尾
            if on_event:
                on_event("max_rounds", {})
            # 最后一问不递菜单，逼它说话（同流式消费）
            reply = merge_stream_chunks(
                llm.generate_stream(payload, None),
                on_text=on_text,
            )

        session.messages.append(reply)
        return reply
    except LLMUnavailableError as exc:
        trim_incomplete_round(session.messages)
        if on_event:
            on_event("error", {"message": str(exc)})
        return None
