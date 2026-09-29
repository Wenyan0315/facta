"""M6.2 摘要压缩：上下文窗口管理（只造投影，不碰底片）。

三件套分工：
- 底片 messages：完整历史，agent_loop append-only 维护，落盘 session.json
- 缓存 summary/summarized_upto：滚动摘要 + 覆盖进度，触发式重算、平时复用
- 投影 payload：每次 llm.generate 前由 build_payload 现切，纯函数零副作用

两条铁律：
① 窗口左边界必须落在 user 消息上。裸切 messages[-K:] 可能把 role="tool"
   消息和它的 tool_calls 爹切分家——孤儿 tool 消息会被 API 直接 400 拒收
  （M5 的血泪教训；mock 模型不校验，本地全绿、真模型间歇炸）
② 摘要是内部 LLM 调用，tools 参数永远传 None——防"摘要调工具、工具的
   回答再触发摘要"的无限递归（与 search_and_summarize 同款防线）
"""

import logging

from agent.core.llm import LLM
from agent.core.types import Message

logger = logging.getLogger(__name__)

# 原文窗口大小：最近 6 条（约 3 轮对话）逐字进 payload
KEEP_LAST = 6
# 触发阈值：窗口外积压 ≥ 6 条还没进摘要时，才花一次 LLM 调用做滚动摘要
# （早触发策略：宁可每次压小块，不做"快满了才压"的自指挤压/悬崖赌博）
TRIGGER_MARGIN = 6

_SUMMARY_PROMPT = (
    "你是对话记忆压缩器。请把【已有摘要】与【待并入的对话】合并成一段新的中文摘要。"
    "必须保留：用户的个人信息与偏好、重要决定与结论、承诺或待办、"
    "关键事实（暗号、日期、数字、文件名等）。"
    "可以丢弃：寒暄、重复内容、与任务无关的细节。"
    "纪律：你是档案员，不是评论员——只记录对话中发生的事实。"
    "禁止写入：对助手自身能力/表现的评价或建议（如'检索偶发不命中，需兜底'）、"
    "对用户意图的猜测（如'用户在测试边界'）、任何策略性元叙事。"
    "这类内容混进摘要会污染后续行为（自证预言）。"
    "直接输出摘要正文，不要前言、标题或解释。"
)


def window_start(messages: list[Message], keep_last: int = KEEP_LAST) -> int:
    """计算原文窗口的起始下标（铁律①的执行者）。

    先取裸切点 max(1, len-keep_last)（1 = 保住第 0 条 system 人设），
    再向右推进，跳过开头的 tool / 点菜 assistant，直到踩到 user。
    极端情况：窗口内一条 user 都没有（工具轮超长）→ 向左扩展到最近的
    user——宁可窗口大一点，绝不制造孤儿。
    """
    raw = max(1, len(messages) - keep_last)
    i = raw
    while i < len(messages) and messages[i].role != "user":
        i += 1
    if i < len(messages):
        return i            # 窗口内有 user：右移收口到轮次边界
    j = raw - 1             # 窗口内没有 user：向左扩
    while j >= 1 and messages[j].role != "user":
        j -= 1
    return max(1, j)


def build_payload(
    messages: list[Message],
    summary: str | None,
    summarized_upto: int = 1,
    keep_last: int = KEEP_LAST,
) -> list[Message]:
    """从底片切出发送用投影：[system 人设] + [摘要] + [尚未进摘要的原文]。

    纯函数：绝不修改 messages。没有摘要时退化为全量直发（M6.1 语义）。

    覆盖不变量（验收翻车后立下的规矩）：
    每条消息必须可见——要么已被摘要覆盖，要么原文在 payload 里。
    窗口外、但还没攒够触发阈值的积压（死区）必须原样带上；
    第一版没有这条，暗号恰好落在死区 → 模型看不见 → 当场编了个假暗号。
    """
    start = window_start(messages, keep_last)
    start = min(start, summarized_upto)   # 死区防御：没进摘要的消息绝不从视野里消失
    # 第 0 条 system 人设永远在位，不随窗口滑动丢失
    head = messages[:1] if messages and messages[0].role == "system" else []
    if not summary:
        return head + messages[start:]
    summary_msg = Message(
        role="system",
        # 措辞即语义：说"本会话较早内容"而非"更早对话"——后者会被模型
        # 误读成"上一次对话"（4.0 验收翻车点：同会话压缩区≠另一个会话）
        content=f"以下是本会话较早内容的摘要（原文已压缩，逐字原话可用 search_history 检索）：\n{summary}",
    )
    return head + [summary_msg] + messages[start:]


def maybe_compress(
    llm: LLM,
    messages: list[Message],
    summary: str | None,
    summarized_upto: int,
    keep_last: int = KEEP_LAST,
    margin: int = TRIGGER_MARGIN,
) -> tuple[str | None, int]:
    """触发式滚动摘要：不到阈值就复用缓存（零 token），到了才付一次 LLM 调用。

    返回 (新摘要, 新覆盖进度 summarized_upto)。
    覆盖进度 = 摘要已涵盖到 messages 的第几条（下一条还没进摘要）。
    """
    start = window_start(messages, keep_last)
    fresh = messages[summarized_upto:start]    # 窗口外、尚未进摘要的积压
    if len(fresh) < margin:
        return summary, summarized_upto        # 缓存复用：本轮零成本
    logger.debug("记忆压缩：窗口外积压 %s 条 → 滚动摘要（一次内部 LLM 调用）", len(fresh))
    summary = _summarize(llm, summary, fresh)
    logger.debug("摘要完成：%s", summary)   # 可观测性：摘要不再是黑箱，当场查验保真度
    return summary, start


def _summarize(llm: LLM, old_summary: str | None, fresh: list[Message]) -> str:
    """滚动摘要：旧摘要 + 新积压 → 新摘要（铁律②：tools=None）。"""
    parts = []
    if old_summary:
        parts.append(f"【已有摘要】\n{old_summary}")
    dialog = "\n".join(f"[{m.role}] {m.content}" for m in fresh)
    parts.append(f"【待并入的对话】\n{dialog}")
    reply = llm.generate(
        [
            Message(role="system", content=_SUMMARY_PROMPT),
            Message(role="user", content="\n\n".join(parts)),
        ],
        None,   # 铁律②：内部调用绝不递菜单
    )
    return reply.content


def trim_incomplete_round(messages: list[Message]) -> list[Message]:
    """崩溃修复：掐掉尾部不完整的工具轮（Ctrl+C 打断的残局）。

    中断可能落在 assistant(tool_calls) 已入史、tool 结果还没回填的瞬间——
    这种状态直接落盘，下次启动会被 API 400 拒收。
    从尾部弹出孤儿（tool 消息 / 带点菜的 assistant），恢复到最后一个合法边界。
    这不算篡改历史：不完整的轮次从来就不是合法的对话状态。
    """
    while messages and (
        messages[-1].role == "tool"
        or (messages[-1].role == "assistant" and messages[-1].tool_calls)
    ):
        messages.pop()
    return messages
