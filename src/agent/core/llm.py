"""LLM 接入层：把「跟大模型对话」抽象成一个统一接口。

上层代码只依赖 `LLM` 接口，不关心底层是模拟模型还是 DeepSeek。
换模型 = 换一个实现类，其他代码都不用动。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.core.telemetry import UsageLedger
from agent.core.types import Message

if TYPE_CHECKING:  # 仅类型检查期导入：运行期由 get_llm 函数内导入（避免循环依赖）
    from agent.core.gateway import GatewayConfig


class LLMUnavailableError(RuntimeError):
    """所有候选模型都不可用时的最终信号（M7.5d 降级链耗尽）。

    主循环捕获它 → 友善提示 + 优雅结束本轮，而不是让程序崩溃——
    这是「熔断保护」与「用户体验」之间的接口契约（评审 F 条）。
    """


@dataclass
class StreamChunk:
    """流式接口的最小增量单元（streaming 里程碑）。

    content:    本块新增的文本片段（可能为空串——点菜轮 content 就是空的）
    tool_calls: 本块新增的工具调用碎片，None 或元素带 index 的列表；
                碎片是「增量」不是「全量」：同一 index 的 arguments 分散在多块，
                拼回完整 JSON 是消费层的活（合并纯函数，可离线测试）
    usage:      仅最后一块携带（对应 stream_options include_usage 的末班车块）
    """

    content: str = ""
    tool_calls: list[dict] | None = None
    usage: dict | None = None


def merge_stream_chunks(
    chunks: Iterator[StreamChunk],
    on_text: Callable[[str], None] | None = None,
) -> Message:
    """把增量块流拼回一条完整回复（消费层的核心纯函数）。

    三件拼图活：
    - content：纯累加；on_text 回调让「合并」与「打印」解耦——计算是纯函数，
      打印是副作用，副作用从参数缝注入，纯函数本体离线可测
    - tool_calls：按 index 归并。id/name 只出现在第一块（后续碎片是 None），
      通行做法「旧值优先」（新片有值才覆盖）；arguments 是 JSON 字符串碎片，
      跨块纯累加
    - usage：送末班车的那块才有，取最后一块

    这就是「tool_calls 分片重组」难题的全部——模型把点菜的 JSON 参数
    切碎了发,我们按编号拼回原样,拼好的结构和非流式 generate 返回值
    一模一样,下游(工具执行/入史)零感知。
    """
    parts: list[str] = []
    slots: dict[int, dict] = {}   # index → 归并中的 tool_call
    order: list[int] = []         # 首次出现顺序（碎片理论上有交错，保序用）
    usage: dict | None = None

    for chunk in chunks:
        if chunk.content:
            parts.append(chunk.content)
            if on_text:
                on_text(chunk.content)
        if chunk.tool_calls:
            for frag in chunk.tool_calls:
                idx = frag.get("index") or 0
                slot = slots.get(idx)
                if slot is None:
                    slot = {
                        "id": frag.get("id"),
                        "name": frag.get("name"),
                        "arguments": "",
                    }
                    slots[idx] = slot
                    order.append(idx)
                slot["id"] = slot["id"] or frag.get("id")
                slot["name"] = slot["name"] or frag.get("name")
                slot["arguments"] += frag.get("arguments") or ""
        if chunk.usage:
            usage = chunk.usage

    tool_calls = [slots[i] for i in order] or None
    return Message(
        role="assistant",
        content="".join(parts),
        tool_calls=tool_calls,
        usage=usage,
    )


class LLM(ABC):
    """所有大模型实现的统一接口。

    任何模型（模拟的、DeepSeek、OpenAI…）都要实现 generate 方法。

    tools（M5 新增）: OpenAI 格式的工具清单（菜单）。
    模型可以选择"点菜"——回复里带 tool_calls；也可以不点，正常说话。
    假模型可以无视这个参数。

    tool_calls 里每个元素的结构约定（我们自己定的简化格式）：
        {"id": "调用编号", "name": "工具名", "arguments": "参数JSON字符串"}
    """

    # 展示名（降级链打印/审计认人用）：默认空串，get_llm 装配时按供应商赋值
    name: str = ""

    @abstractmethod
    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        """给定对话历史（和可选的工具清单），返回模型的回复。"""

    def generate_stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Iterator[StreamChunk]:
        """流式生成：默认实现 = 伪流（一次吐完一整块）。

        接口演进的老规矩（M5 起）：新能力带默认实现，老代码零改动。
        没有「增量」概念的实现（MockLLM / ScriptedLLM 等）自动获得伪流——
        语义与 generate 完全一致，只是享受不到首字延迟的体验增益。
        只覆写本方法的实现 = 「我支持真流」。"""
        reply = self.generate(messages, tools)
        yield StreamChunk(
            content=reply.content or "",
            tool_calls=reply.tool_calls,
            usage=getattr(reply, "usage", None),
        )


class MockLLM(LLM):
    """模拟模型：不联网、不要 key，先用它把整条链路跑通。

    它不智能，但「输入消息 -> 输出回复」的行为和真模型一致，
    刚好用来验证调用链路的正确性。

    降级兜底（S4 评审 #5）：作为 FallbackLLM 的最后一名候选时，
    回复自带「⚠️ 真模型暂时不可用」前缀——诚实降级，不装正常。
    """

    name = "mock"

    def __init__(self, degraded: bool = False) -> None:
        self._degraded = degraded

    def generate(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Message:
        last = messages[-1].content
        reply = (
            f"[mock] 本轮共收到 {len(messages)} 条历史，最新一句：「{last}」"
            "—— 接上真模型后，这里才是真正的回答。"
        )
        if self._degraded:
            reply = "⚠️ 真模型暂时不可用，这是降级回复。\n\n" + reply
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
        # M7.5b 超时：网络卡住不能把程序一起卡死（默认 30s，{PREFIX}_TIMEOUT 可调）
        timeout = float(os.environ.get(f"{prefix}_TIMEOUT", "30"))
        self._client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get(f"{prefix}_BASE_URL", base_url),
            timeout=timeout,
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

    def generate_stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Iterator[StreamChunk]:
        """真流：stream=True，逐块原始 chunk 转成 StreamChunk 增量。

        两个关键点：
        - stream_options include_usage：usage 默认不上流，不开它账单漏记
        - **生成器函数天生惰性**：函数体在第一次 next() 时才执行——
          「拿到 generate_stream 的返回值」≠「请求已发出」。这个惰性是
          网关层「第一块之前可重试/可降级」的地基（洗完第一块才锁死候选）
        """
        payload = [self._to_openai(m) for m in messages]
        kwargs: dict = {
            "model": self._model,
            "messages": payload,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        stream = self._client.chat.completions.create(**kwargs)
        for chunk in stream:
            usage = self._usage_of(chunk)
            if not chunk.choices:   # 纯尾巴块只带 usage，没有 choices（部分供应商如此发末班车）
                yield StreamChunk(usage=usage)
                continue
            delta = chunk.choices[0].delta
            # 碎片只转「增量」不做合并：同 index 的 arguments 跨块拼接归消费层纯函数。
            # id/name 只出现在第一块，后续碎片这些字段为 None——合并时用「旧的优先」
            tool_calls = None
            if delta.tool_calls:
                tool_calls = [
                    {
                        "index": tc.index,
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                    for tc in delta.tool_calls
                ]
            yield StreamChunk(
                content=delta.content or "",
                tool_calls=tool_calls,
                usage=usage,
            )

    @staticmethod
    def _usage_of(chunk) -> dict | None:
        if not getattr(chunk, "usage", None):
            return None
        return {
            "prompt_tokens": chunk.usage.prompt_tokens,
            "completion_tokens": chunk.usage.completion_tokens,
        }


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


def get_llm(
    provider: str = "mock",
    ledger: UsageLedger | None = None,
    config: GatewayConfig | None = None,
    with_mock_fallback: bool = True,
) -> LLM:
    """工厂 = 进程内网关（M7.5d 起是降级链组装器）。

    真模型模式：主模型 → 有 key 的备用真模型 → mock 兜底，
    每个候选各穿自己的防护壳（记账+重试+精确缓存+熔断）；
    链由一个 FallbackLLM 统筹切换。练习模式（假模型）单候选项，不组链。

    with_mock_fallback=False（评测用）：不挂 mock 兜底——评估时主模型
    失败就大声抛异常，而不是被 mock 顶替静默污染分数。
    """
    from agent.core.gateway import (  # 函数内导入：gateway 依赖本模块，避免循环
        FallbackLLM,
        GatewayConfig,
        RobustLLM,
    )

    if ledger is None:
        ledger = UsageLedger()
    if config is None:
        config = GatewayConfig()

    def _wrapped(inner: LLM, label: str) -> LLM:
        inner.name = label  # 降级打印时能认出谁是谁（OpenAICompatible 类名不带供应商）
        return RobustLLM(inner, ledger, config)

    if provider in ("mock", "echo", "repeat"):
        fakes: dict[str, LLM] = {
            "mock": MockLLM(),
            "echo": EchoLLM(),
            "repeat": RepeatLLM(),
        }
        return _wrapped(fakes[provider], provider)

    if provider not in PROVIDERS:
        raise ValueError(f"未知的模型提供方: {provider}")

    cfg = PROVIDERS[provider]
    chain: list[LLM] = [_wrapped(_build_openai(cfg), provider)]

    # 备用真模型：只挂「有 key 的」——没 key 的备选在启动时不报错（不是主选）
    for name, backup_cfg in PROVIDERS.items():
        if name != provider and os.environ.get(f"{backup_cfg['prefix']}_API_KEY"):
            chain.append(_wrapped(_build_openai(backup_cfg), name))

    # mock 兜底（用户拍板）：全挂也保对话可用；降级时 FallbackLLM 会打印声明，
    # 回复自带「⚠️ 真模型暂时不可用」前缀——诚实降级，不装正常（S4 评审 #5）。
    # 评测场景传 with_mock_fallback=False 关掉（失败须大声，不被 mock 顶替）
    if with_mock_fallback:
        chain.append(_wrapped(MockLLM(degraded=True), "mock"))
    if len(chain) == 1:
        return chain[0]  # 无备用无兜底：单候选，省一个 Fallback 包装层
    return FallbackLLM(chain, ledger)


def _build_openai(cfg: dict) -> LLM:
    return OpenAICompatibleLLM(
        cfg["prefix"],
        cfg["base_url"],
        cfg["model"],
        float(cfg["price_in"]),
        float(cfg["price_out"]),
    )
