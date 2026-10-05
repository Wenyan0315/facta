"""投影装配（P2-6 ③）：把「本轮视野」切成投影尾部的一批 stamp。

从 loop.py 搬出，纯搬移（② 的时间戳降精度落在 _time_stamp）。这些 stamp
共同契约：进投影不进底片、缺席不注入零开销、按 time→route→plan 顺序追加。
内核 loop.py 是唯一消费者；测试直接 import 内部函数是历史惯例，随搬移同步改路径。
"""

from __future__ import annotations

from datetime import datetime

from facta.core.jev import RouteDecision
from facta.core.types import Message
from facta.memory.plan import PlanBoard
from facta.orchestrator.agent import Agent
from facta.tools.plan import format_view

_WEEKDAYS = "一二三四五六日"


def _plan_stamp(board: PlanBoard) -> Message | None:
    """活跃计划注入投影（S5b 针①，时间戳同款手法：进投影不进底片）。

    位置固定：时间戳之后、摘要/对话之前——「今天几号」和「任务进行到哪」
    都属于本轮视野。无活跃计划返回 None：不注入任何东西，简单任务的
    上下文零开销（轮首快照——同轮内多步导航靠 update_plan_step 的
    工具结果回灌带最新视图，双视图分工）。
    """
    view = board.view()
    if view is None:
        return None

    return Message(
        role="system",
        content=(
            "【当前任务计划】以下任务正在进行，按计划继续执行；"
            "步骤状态变化用 update_plan_step 回写（终态必带 note），"
            "计划过时用 make_plan 修订（reason 必填），全部终态后 finish_plan 收官。\n"
            + format_view(view)
        ),
    )


def _time_stamp(now: datetime | None = None) -> Message:
    """当前时间戳（投影专用，绝不入底片）。

    为什么要它（真实使用经验逼出来的）：跨会话恢复时，模型没有「现在」的
    概念，会拿上次对话的时间当锚点，安静地算错一切相对时间——"更新数据"
    取到半个月前的日期还不报错。时间戳管「今天是哪天」这个锚点；
    get_current_time 工具继续管秒级精度与未来时间点。

    进投影不进底片的理由：时间属于「本轮视野」而非「对话内容」——
    入底片会堆日期垃圾、被摘要吸收；投影每轮现切、随轮作废。
    now 参数留给测试注入固定时刻。

    082 ② 降精度：分钟级内容每轮必变 ⇒ 投影尾部字节不稳 ⇒ 作废 prefix 缓存；
    降到「日期 + 上午/下午/晚间」后同半天内字节稳定（秒级需求走 get_current_time）。
    """
    now = now or datetime.now()
    period = "上午" if now.hour < 12 else ("下午" if now.hour < 18 else "晚间")
    return Message(
        role="system",
        content=f"今天：{now:%Y-%m-%d}（周{_WEEKDAYS[now.weekday()]}）{period}",
    )


def _route_first_menu(
    agent: Agent, user_text: str, schemas: list[dict]
) -> tuple[list[dict] | None, RouteDecision | None]:
    """M10 场景路由（轮首一针，只影响本轮第一次模型调用）。

    返回 (菜单, 路由决定)：决定随菜单一起上浮给 run_turn 做投影注入
    （073 路由 stamp——决定显式化，不再只以「菜单收窄」的间接信号存在）。

    direct      → (None, decision)——省全部菜单 token，且纯聊天流量因此落进
                  SemanticCacheLLM 的命中区（它只在 tools=None 时生效，免费放大既有基建）
    single_tool → (只递该工具 schema, decision)——选择权已由 Jev 行使，LLM 只填参数
                  （单工具菜单即全部强制力，不用 tool_choice 强制——那会堵死
                  Jev 误判时模型直答的逃生门）；Jev 选的名字不在菜单 → 回退全量
                  且决定作废（stamp 不注入——说收窄却给全量等于骗模型）
    complex     → (全量菜单, decision)——模型自己走 S5b make_plan
    无路由（无 key 装配缺席 / 故障降级 / 熔断跳过）→ 原生路径（v0.57 行为）
    """
    if agent.router is None:
        return schemas or None, None
    decision = agent.router.route(user_text)
    if decision is None:
        return schemas or None, None
    if decision.kind == "direct":
        return None, decision
    if decision.kind == "single_tool" and decision.tool is not None:
        single = [s for s in schemas if s["function"]["name"] == decision.tool]
        if single:
            return single, decision
        return schemas or None, None   # 名字不在菜单：决定未生效，回退全量
    return schemas or None, decision   # complex


def _route_stamp(decision: RouteDecision | None) -> Message | None:
    """路由决定注入投影（073，时间戳/计划 stamp 同款手法：进投影不进底片）。

    Confidence Routing ⑦③：single_tool 的决定此前只以「菜单收窄」间接信号
    存在（漂移温床）——主模型不知道路由器判了什么、也不知道收窄的菜单
    在误判时可直答逃生。stamp 把决定显式写进本轮视野（谁做了决定、
    决定是什么）；逃生门照 028 决策 4 保留（不强制 tool_choice）。
    理由是程序从 kind+tool 推导的确定性文案——Jev choice 是一段式协议，
    响应里没有理由字段可解析，不猜协议（伪造一个「Jev 的理由」反而失真）。
    """
    if decision is None:
        return None
    if decision.kind == "direct":
        content = "【场景路由】本轮判定为直接回答：不挂工具菜单，用自身知识作答即可。"
    elif decision.kind == "single_tool" and decision.tool is not None:
        content = (
            f"【场景路由】本轮判定为单工具任务，首轮菜单已收窄为 {decision.tool}。"
            "若判定有误可直接文字作答；工具结果回灌后将恢复全量菜单。"
        )
    else:
        content = (
            "【场景路由】本轮判定为多步骤任务，全量工具菜单已挂载；"
            "建议先用 make_plan 拆解步骤再逐个执行。"
        )
    return Message(role="system", content=content)


def _append_stamps(
    payload: list[Message], decision: RouteDecision | None, board: PlanBoard
) -> None:
    """投影尾部三件 stamp 就位：时间锚点 → 路由决定 → 活跃计划（S2b/M10/073）。

    080 从头部（`payload.insert(1, …)`）改为尾部（`payload.append`）：三个 stamp
    是每轮必变的动态内容，插头部会把其后整段前缀缓存全部作废；后置后
    `[system 人设] + [摘要] + [原文]` 前缀字节稳定，同会话连续轮次 prefix 命中
    （system 消息任意位置均指令，后置不牺牲注入语义）。

    三件共同契约：进投影不进底片、缺席不注入零开销、按 time→route→plan 顺序
    追加（append 天然不留空洞，无需原来的动态游标）。
    """
    payload.append(_time_stamp())
    route_msg = _route_stamp(decision)
    if route_msg is not None:
        payload.append(route_msg)
    plan_msg = _plan_stamp(board)
    if plan_msg is not None:
        payload.append(plan_msg)
