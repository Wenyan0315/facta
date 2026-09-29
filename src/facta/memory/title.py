"""会话标题提炼：把一段对话浓缩成一个主题标签（S2 验收修复轮）。

derive_title（store.py）是「首句机械截断」——首句是「你好」「2」这种时，
标签毫无信息量（列表里一排看不出各段在聊什么）。本模块用 LLM 把它升级为
「主题提炼」：读滚动摘要 + 尾窗原文，产出一个代表对话主题的短标题。

职责边界：store.py 是纯数据层（不 import LLM），提炼逻辑放这里；
derive_title 保留为 fallback——LLM 挂了也归档，不因标题失败而中断。
归档是低频动作，标题提炼只在归档时付一次成本（列表读取永远零 LLM 调用）。
"""

from __future__ import annotations

from facta.core.llm import LLM, LLMUnavailableError
from facta.core.types import Message
from facta.memory.store import Session

TITLE_MAX_LEN = 20   # 与 derive_title 的 TITLE_MAX_LEN 对齐：标签是一行，不是摘要

# 标题只看对话「故事线」：system 是人设、tool 是中间产物，都不参与主题判断
_WINDOW = 8

TITLE_TEMPLATE = """你是会话标题提炼员。给下面这段对话起一个 {max_len} 字以内的标题，概括这段对话在聊什么。

要求：
1. 用对话的主要语言
2. 只输出标题本身，不要引号、冒号、解释、句号
3. 提炼主题，不要照抄第一句

对话材料：
{transcript}"""


def _transcript(session: Session) -> str:
    """对话材料 = 滚动摘要（如有）+ 尾窗 user/assistant 原文。"""
    parts = []
    if session.summary:
        parts.append(f"【摘要】{session.summary}")
    tail = [m for m in session.messages if m.role in ("user", "assistant") and m.content]
    parts.extend(f"{m.role}: {m.content}" for m in tail[-_WINDOW:])
    return "\n".join(parts)


def summarize_title(session: Session, llm: LLM) -> str | None:
    """提炼会话标题；任何失败（模型挂/空输出/坏输出）都返回 None，调用方 fallback。

    llm 传内部链（internal_llm）：内部调用不穿语义档（拆链原则），
    且 tools=None 防「拿菜单的模型点菜」递归。
    """
    transcript = _transcript(session)
    if not transcript.strip():
        return None
    prompt = TITLE_TEMPLATE.format(max_len=TITLE_MAX_LEN, transcript=transcript)
    try:
        title = llm.generate([Message(role="user", content=prompt)]).content
    except LLMUnavailableError:
        return None

    title = title.strip().strip('"\'`「」『』').strip()
    if not title:
        return None
    return title if len(title) <= TITLE_MAX_LEN else title[:TITLE_MAX_LEN]
