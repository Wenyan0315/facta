"""核心数据类型：跨层共享的「地基」。

Message 是全项目最基础的类型——LLM 接入、记忆落盘、摘要压缩、工具检索、
主循环五处共用。它不属于任何一个模块，所以从 llm.py（接入层）抽出来，
让「用一条消息」的模块不必 import「怎么跟大模型说话」。

（P1-1 依赖方向收口：持久化层/memory 不再反向蹭接入层。）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Message:
    """一条对话消息。

    role: "system"(人设/规则) / "user"(用户) / "assistant"(模型回复)
          / "tool"(工具执行结果，M5 新增)
    content: 消息正文
    tool_calls: assistant 消息专属——模型"点菜"的请求列表（M5 新增）
    tool_call_id: tool 消息专属——标记这条结果对应哪次调用（M5 新增）

    新字段都带默认值 None：老代码只写 (role, content) 照样合法——
    接口演进的标准手法：只加可选字段，不破坏既有使用者。
    """

    role: str
    content: str
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None

    def __repr__(self) -> str:
        if self.tool_calls:
            calls = ", ".join(f"{tc['name']}({tc['arguments']})" for tc in self.tool_calls)
            return f"{self.role}: [请求调用工具] {calls}"
        return f"{self.role}: {self.content}"
