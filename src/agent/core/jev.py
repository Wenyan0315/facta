"""Jev 决策模型接入（M10）：场景路由与原子决策外挂。

评测依据（2026-09-20 model-bench，git 外独立目录；证据摘要见 028）：
- 工具调用轨道 19/20 全场第一（choice 原生路由），成本约为主力 LLM 1/20
- 甜点区 = choice/noul；无参数通道（参数填充是 LLM 主场）；score 评级 53% 弱
- 结论适用域 = 原子决策场景，非对模型的整体判语

架构角色（bench 三层分解的落地）：
- 意图识别层 → 本模块 ScenarioRouter（Jev choice 独占）
- 槽位填充层 → LLM 原生 tool_calls（主力切 deepseek-flash）
- 循环决策层 → harness 程序侧（run_turn 循环 + 确认闸门白名单硬编码）

三态生命周期（硬约束：Jev 是可选增强层，不是依赖）：
  状态A 无 key       装配层不构造 ScenarioRouter（Agent.router=None → 原生路径）
  状态B 单次故障      route() 内部捕获 → fail-open 返回 None + 计入熔断计数
  状态C 持续故障      熔断冷却期直接 None（不打网络）；半开试探同 RobustLLM 语义

fail-open 的全部语义在返回值设计里：route() 返回 None = 没有路由 =
原生路径（v0.57 行为）——降级不是「切到备用路由」，是「没有路由」，
零新代码路径。route() 永不抛异常（装配层不必为它设防）。

注入免疫（选项空间封闭）：choice 的选项集是封闭集合（工具名 ∪
{direct, complex}），state 里的任何注入文字最多让 Jev 选错选项，
产生不了自由文本动作——与 S3 注入界碑同一威胁模型下的结构性防御。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from agent.core.telemetry import UsageLedger

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteDecision:
    """路由结果：direct / single_tool / complex 三类场景。

    frozen + 显式 kind 字段（而非继承体系）：三个分支在 run_turn 里
    是 if/elif 平铺，值对象比多态更贴消费方形状。
    """

    kind: str          # "direct" | "single_tool" | "complex"
    tool: str | None = None   # 仅 single_tool：Jev 选定的工具名


class JevClient:
    """TypeSafe Jev API 的最小同步客户端（标准库 transport，零新依赖）。

    只实现 choice 通道（甜点区）；noul 预留位（v1 未用——确认闸门程序侧
    硬编码，bench 实测五模型无一全对，不能信模型自觉；触发信号：实测
    出现白名单漏放案例时再挂补充信号）。
    """

    name = "jev"

    def __init__(self, api_key: str, base_url: str, timeout: float = 3.0) -> None:
        self._key = api_key
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self._timeout = timeout

    def choice(self, state: str, question_id: str, instructions: str,
               options: list[str]) -> tuple[str, dict]:
        """问一个 choice 问题；返回 (选中项, 响应)。

        抛 urllib.error.URLError / HTTPError / TimeoutError —— 由
        ScenarioRouter.route() 统一捕获（fail-open 语义在此层之上）。
        payload 形状与 model-bench/toolcall.py 的 run_jev 同构（已验证）。
        """
        payload = {
            "model": "jev-latest",
            "state": state,
            "questions": {
                question_id: {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": options,
                }
            },
        }
        req = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = (data.get("answers", {}).get(question_id) or {})
        picked = answer.get("choice")
        if not isinstance(picked, str):
            raise ValueError("Jev 响应缺 choice 字段")
        return picked, data


class ScenarioRouter:
    """场景路由器：Jev choice 判断用户消息该走哪条路径（三态生命周期）。

    熔断三态与 RobustLLM 同款（closed → open → half_open），参数同款
    （fail_threshold=3 / cooldown=30s / 半开 1 次）——一致性优先于微调；
    v1 不抽公共基类（两处 ~30 行重复 vs 重构 gateway 的风险，触发信号
    = 第三个消费者出现）。
    """

    def __init__(
        self,
        client: JevClient,
        tool_names: list[str],
        ledger: UsageLedger | None = None,
        fail_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._tools = list(tool_names)
        self._ledger = ledger or UsageLedger()
        self._fail_threshold = fail_threshold
        self._cooldown = cooldown_seconds
        # 熔断状态：与 gateway.RobustLLM 三态同款（时间基 time.monotonic）
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open = False

    @property
    def breaker_state(self) -> str:
        """熔断三态（测试断言用）：closed / open / half_open。"""
        if self._opened_at is not None:
            return "open"
        return "half_open" if self._half_open else "closed"

    def route(self, user_text: str) -> RouteDecision | None:
        """一段式 choice：选项 = 工具名 ∪ {direct, complex}。

        返回 RouteDecision，或 None（= 原生路径：无 key 装配缺席之外，
        还包括 Jev 故障 fail-open、熔断 open 期快速跳过、Jev 选项
        超出预期集——最后一种是防御：不认识的选择当没有路由处理）。
        """
        if self._breaker_blocks():
            return None

        options = self._tools + ["direct", "complex"]
        state = (
            f"用户对个人助手说：「{user_text}」\n可用工具：{', '.join(self._tools)}"
        )
        try:
            picked, _ = self._client.choice(
                state=state,
                question_id="scenario",
                instructions="这条消息应该走哪条处理路径？",
                options=options,
            )
        except Exception as exc:  # noqa: BLE001  # fail-open：任何故障都降级，不上抛
            self._ledger.record_jev_degradation()
            self._on_failure()
            logger.warning("[路由降级] Jev 调用失败，本轮走 LLM 原生路径：%s", exc)
            return None

        self._ledger.record_jev()
        self._on_success()
        if picked == "direct":
            logger.info("场景路由：direct（纯生成，不递菜单）")
            return RouteDecision(kind="direct")
        if picked == "complex":
            logger.info("场景路由：complex（完整菜单，模型自决）")
            return RouteDecision(kind="complex")
        if picked in self._tools:
            logger.info("场景路由：single_tool → %s", picked)
            return RouteDecision(kind="single_tool", tool=picked)
        # 选项空间封闭的最后一道防御：Jev 返回了不认识的串——当没有路由
        logger.warning("场景路由：Jev 返回未知选项「%s」，当无路由处理", picked)
        return None

    # ---- 熔断三态（与 RobustLLM 同款语义；时间基 monotonic）----

    def _breaker_blocks(self) -> bool:
        """open 且冷却未到 → True（快速跳过，不打网络）。冷却期满转半开。"""
        if self._opened_at is not None:
            left = self._cooldown - (time.monotonic() - self._opened_at)
            if left > 0:
                return True
            self._opened_at = None
            self._half_open = True   # 放行一次试探
        return self._half_open and False   # half_open 放行（试探结果定去留）

    def _on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._half_open:
            self._half_open = False
            self._opened_at = time.monotonic()
            self._consecutive_failures = 0
        elif self._consecutive_failures >= self._fail_threshold:
            self._opened_at = time.monotonic()
            self._consecutive_failures = 0

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._half_open = False
