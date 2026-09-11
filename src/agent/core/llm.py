"""LLM 接入层：把「跟大模型对话」抽象成一个统一接口。

上层代码只依赖 `LLM` 接口，不关心底层是模拟模型还是 DeepSeek。
换模型 = 换一个实现类，其他代码都不用动。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from agent.core.telemetry import UsageLedger
from agent.core.types import Message


class LLM(ABC):
    """所有大模型实现的统一接口。

    任何模型（模拟的、DeepSeek、OpenAI…）都要实现 generate 方法。

    tools（M5 新增）: OpenAI 格式的工具清单（菜单）。
    模型可以选择"点菜"——回复里带 tool_calls；也可以不点，正常说话。
    假模型可以无视这个参数。

    tool_calls 里每个元素的结构约定（我们自己定的简化格式）：
        {"id": "调用编号", "name": "工具名", "arguments": "参数JSON字符串"}
    """

    @abstractmethod
    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        """给定对话历史（和可选的工具清单），返回模型的回复。"""


class MockLLM(LLM):
    """模拟模型：不联网、不要 key，先用它把整条链路跑通。

    它不智能，但「输入消息 -> 输出回复」的行为和真模型一致，
    刚好用来验证调用链路的正确性。
    """

    name = "mock"

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
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

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        last = messages[-1].content
        reply = f"[echo] 你的话：「{last}」"
        return Message(role="assistant", content=reply)

class RepeatLLM(LLM):
    """模拟模型：不联网、不要 key，先用它把整条链路跑通。

    它不智能，但「输入消息 -> 输出回复」的行为和真模型一致，
    刚好用来验证调用链路的正确性。
    """

    name = "repeat"

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        # 关键：遍历【整个历史列表】，而不是只看最后一句
        lines = [f"  {m.role}: {m.content}" for m in messages]
        reply = "[repeat] 我收到的完整历史是：\n" + "\n".join(lines)
        return Message(role="assistant", content=reply)


class ScriptedLLM(LLM):
    """脚本假模型：按预设回复依次吐，用于离线测工具链路。

    MockLLM / EchoLLM / RepeatLLM 都只回纯文本，测不了 tool_calls——
    而工具循环、tool 消息与 tool_calls 配对、窗口孤儿检测，全都依赖
    「模型会点菜」。这一直是本地全绿、真模型间歇炸的根因（compressor.py
    自己写下的那句「mock 模型不校验」）。

    用法：script 是一串 Message，想怎么编怎么编——纯文本回复、带
    tool_calls 的点菜消息都在这里指定。每次 generate 都把收到的 messages
    存进 self.calls，供断言「这一轮模型到底看到了什么」。脚本弹完后再被叫，
    回一句兜底文本，防止工具循环空转。"""

    name = "scripted"

    def __init__(self, script: list[Message]) -> None:
        self._script = list(script)
        self._index = 0
        # 每次 generate 收到的 messages 快照（断言投影/配对用）
        self.calls: list[list[Message]] = []

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        self.calls.append(list(messages))   # 浅拷贝快照，而非引用——否则断言时已经变了
        if self._index < len(self._script):
            reply = self._script[self._index]
            self._index += 1
            return reply
        return Message(role="assistant", content="[script exhausted]")


class OpenAICompatibleLLM(LLM):
    """真模型的统一实现：一切 OpenAI 兼容供应商都能用这一个类。

    业界主流（DeepSeek、硅基流动、各家中转商）都兼容 OpenAI 接口，
    区别只有三样：key、base_url、模型名。
    所以不做"每家一个类"，而是【一个类 + 一张配置表】：

        {"key环境变量", "base_url默认值", "默认模型"}

    三个值都可被环境变量 {PREFIX}_API_KEY / _BASE_URL / _MODEL 覆盖。
    """

    def __init__(
        self,
        prefix: str,
        base_url: str,
        model: str,
        price_in: float = 0.0,
        price_out: float = 0.0,
    ) -> None:
        # 延迟导入：只有真正用到真模型时才需要 openai 库
        from openai import OpenAI

        api_key = os.environ.get(f"{prefix}_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                f"缺少 {prefix}_API_KEY：请先在项目根目录 .env 文件里配置它"
            )
        # base_url / model 也允许环境变量覆盖
        self._client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get(f"{prefix}_BASE_URL", base_url),
        )
        self._model = os.environ.get(f"{prefix}_MODEL", model)
        # M7.5：价目（¥/百万 tokens）记在身上，供网关把 token 换算成钱
        # 假模型没有 pricing 属性 → 网关 getattr 兜底为 None → 成本 0
        self.pricing = {"in": price_in, "out": price_out}

    @staticmethod
    def _to_openai(m: Message) -> dict:
        """我们的 Message -> OpenAI 消息格式的转换（三种情况）。"""
        # ① 工具结果消息：带 tool_call_id，和 assistant 的请求配对
        if m.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": m.tool_call_id or "",
                "content": m.content,
            }
        # ② 模型的"点菜"消息：assistant + tool_calls
        if m.tool_calls:
            return {
                "role": "assistant",
                "content": m.content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in m.tool_calls
                ],
            }
        # ③ 普通消息
        return {"role": m.role, "content": m.content}

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        payload = [self._to_openai(m) for m in messages]
        kwargs: dict = {"model": self._model, "messages": payload}
        # 有菜单才递菜单；tools=None 时不传这个字段（假菜单会干扰模型）
        if tools:
            kwargs["tools"] = tools

        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        # M7.5：采集 token 账目（极少数中转商不返回 usage，防御一下），
        # 挂在 Message 上交给网关记账——Message 多带信息，调用链不用多开一条路
        usage = None
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }

        # 模型"点菜"了：把 OpenAI 的对象结构转成我们的简化 dict 结构
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in msg.tool_calls
            ]
        return Message(
            role="assistant",
            content=msg.content or "",
            tool_calls=tool_calls,
            usage=usage,
        )


# 供应商配置表：加一家 = 加一行。
# prefix 约定：环境变量 {PREFIX}_API_KEY / {PREFIX}_BASE_URL / {PREFIX}_MODEL
# price_in/price_out：M7.5 记账价目（¥/百万 tokens），示例价，以官网实时价为准
PROVIDERS: dict[str, dict[str, str | float]] = {
    "deepseek": {
        "prefix": "DEEPSEEK",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "price_in": 1.0,
        "price_out": 2.0,
    },
    "siliconflow": {
        "prefix": "SILICONFLOW",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "price_in": 2.0,
        "price_out": 8.0,
    },
}


def get_llm(provider: str = "mock", ledger: UsageLedger | None = None) -> LLM:
    """工厂 = 进程内网关入口（M7.5）：组装实现后统一穿防护壳。

    ledger 是全局账本（__main__ 创建传入）；不传则网关自己建一个自用。
    从调用方视角返回的还是普通 LLM——agent_loop 等零改动。
    """
    from agent.core.gateway import RobustLLM  # 函数内导入：gateway 依赖本模块，避免循环

    if ledger is None:
        ledger = UsageLedger()

    if provider in PROVIDERS:
        cfg = PROVIDERS[provider]
        inner: LLM = OpenAICompatibleLLM(
            cfg["prefix"],
            cfg["base_url"],
            cfg["model"],
            float(cfg["price_in"]),
            float(cfg["price_out"]),
        )
        return RobustLLM(inner, ledger)

    fakes: dict[str, LLM] = {
        "mock": MockLLM(),
        "echo": EchoLLM(),
        "repeat": RepeatLLM(),
    }
    if provider in fakes:
        return RobustLLM(fakes[provider], ledger)
    raise ValueError(f"未知的模型提供方: {provider}")