"""LLM 接入层：把「跟大模型对话」抽象成一个统一接口。

上层代码只依赖 `LLM` 接口，不关心底层是模拟模型还是 DeepSeek。
换模型 = 换一个实现类，其他代码都不用动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Message:
    """一条对话消息。

    role: "system"(人设/规则) / "user"(用户) / "assistant"(模型回复)
    content: 消息正文
    """

    role: str
    content: str

    def __repr__(self) -> str:
        return f"{self.role}: {self.content}"


class LLM(ABC):
    """所有大模型实现的统一接口。

    任何模型（模拟的、DeepSeek、OpenAI…）都要实现 generate 方法。
    """

    @abstractmethod
    def generate(self, messages: list[Message]) -> Message:
        """给定一段对话历史，返回模型的回复。"""


class MockLLM(LLM):
    """模拟模型：不联网、不要 key，先用它把整条链路跑通。

    它不智能，但「输入消息 -> 输出回复」的行为和真模型一致，
    刚好用来验证调用链路的正确性。
    """

    name = "mock"

    def generate(self, messages: list[Message]) -> Message:
        last = messages[-1].content
        reply = (
            f"[mock] 本轮共收到 {len(messages)} 条历史，最新一句：「{last}」"
            "—— 接上真模型后，这里才是真正的回答。"
        )
        return Message(role="assistant", content=reply)

class EchoLLM(LLM):
    """模拟模型：不联网、不要 key，先用它把整条链路跑通。

    它不智能，但「输入消息 -> 输出回复」的行为和真模型一致，
    刚好用来验证调用链路的正确性。
    """

    name = "echo"

    def generate(self, messages: list[Message]) -> Message:
        last = messages[-1].content
        reply = f"[echo] 你的话：「{last}」"
        return Message(role="assistant", content=reply)

class RepeatLLM(LLM):
    """模拟模型：不联网、不要 key，先用它把整条链路跑通。

    它不智能，但「输入消息 -> 输出回复」的行为和真模型一致，
    刚好用来验证调用链路的正确性。
    """

    name = "repeat"

    def generate(self, messages: list[Message]) -> Message:
        # 关键：遍历【整个历史列表】，而不是只看最后一句
        lines = [f"  {m.role}: {m.content}" for m in messages]
        reply = "[repeat] 我收到的完整历史是：\n" + "\n".join(lines)
        return Message(role="assistant", content=reply)

def get_llm(provider: str = "mock") -> LLM:
    """工厂函数：按名字返回对应的模型实现，方便以后切换。

    以后接 DeepSeek 时，在这里加一个 "deepseek": DeepSeekLLM() 即可。
    """
    providers: dict[str, LLM] = {
        "mock": MockLLM(),
        "echo": EchoLLM(),
        "repeat": RepeatLLM(),
    }
    if provider not in providers:
        raise ValueError(f"未知的模型提供方: {provider}")
    return providers[provider]