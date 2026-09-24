"""S2 验收修复轮：会话标题提炼（LLM）。

（清单展示「提炼标签优先、缺失回落首句」的验收随 S8a 迁到 test_sessions.py 的
list_metas 组——那是 SessionStore 的职责，不再是归档清单的。）
"""

from agent.core.llm import LLM, LLMUnavailableError, ScriptedLLM
from agent.core.types import Message
from agent.memory.store import Session
from agent.memory.title import summarize_title


class _UnavailableLLM(LLM):
    """模型链耗尽：专测标题提炼的降级路径（LLM 挂不阻断归档）。"""

    name = "unavailable"

    def generate(self, messages, tools=None):
        raise LLMUnavailableError("链耗尽")


def _session_with_talk() -> Session:
    session = Session()
    session.messages.append(Message(role="user", content="PHP 结合 AI Agent 可以做什么"))
    session.messages.append(Message(role="assistant", content="PHP 可以当工具层，决策交给大模型"))
    session.messages.append(Message(role="user", content="怎么封装成工具"))
    session.messages.append(Message(role="assistant", content="把函数签名翻译成 schema"))
    return session


def test_summarize_title_uses_llm():
    # ScriptedLLM 回「PHP 做 Agent 工具层」，提炼函数返回它（并做 strip）
    llm = ScriptedLLM([Message(role="assistant", content="PHP 做 Agent 工具层")])
    title = summarize_title(_session_with_talk(), llm)
    assert title == "PHP 做 Agent 工具层"


def test_summarize_title_strips_quotes_and_truncates():
    # 模型爱带引号；超长标题截到上限
    llm = ScriptedLLM([Message(role="assistant", content='"这是一个非常非常非常非常长的标题超出二十字限制"')])
    title = summarize_title(_session_with_talk(), llm)
    assert title == "这是一个非常非常非常非常长的标题超出二十字"[:20]
    assert not title.startswith('"')


def test_summarize_title_fallback_on_llm_error():
    # LLM 挂 → 返回 None，调用方 fallback 到首句派生，不阻断归档
    assert summarize_title(_session_with_talk(), _UnavailableLLM()) is None


def test_summarize_title_empty_session_returns_none():
    # 空会话无对话材料 → 不烧 LLM，直接 None
    llm = ScriptedLLM([])
    assert summarize_title(Session(), llm) is None
    assert llm.calls == []   # 没调过模型
