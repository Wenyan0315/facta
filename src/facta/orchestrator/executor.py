"""批执行器（P2-6 ③）：把一轮点菜切成批、并行/串行跑完、按序回填。

从 loop.py 搬出，纯搬移（① 的取消透传落在 _run_parallel/_execute_tool_calls）。
内核 loop.py 是唯一消费者；测试直接 import 内部函数是历史惯例，随搬移同步改路径。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from facta.core.types import Message
from facta.memory.compressor import trim_incomplete_round
from facta.memory.plan import PlanBoard
from facta.memory.store import Session
from facta.orchestrator.agent import Agent
from facta.tools.registry import ToolRegistry

# S6b 并行 spawn：唯一「设计上可证明安全」的并行工具（独立 Session +
# worktree 隔离、IO-bound）。084 起扩展到只读工具——「模型常期待先读 A 再
# 决定读 B」对同一轮并列点菜不成立（决定已做完，无顺序依赖），只读工具
# 无副作用、无顺序依赖，与 spawn 一样可并行（可并行判定见 _split_tool_batches）
_SPAWN_TOOL = "spawn_subagent"


def _forward_plan_events(board: PlanBoard, on_event: Callable[[str, dict], None] | None) -> None:
    """drain 计划事件并转发（S5b 针②的函数体）。

    无 on_event 也 drain——清队列防陈旧事件跨轮堆积（测试/纯文本场景
    产生的 plan 事件不能攒到下次有监听时一起冒出来）。
    """
    events = board.drain()
    if on_event is not None:
        for ev in events:
            on_event(ev.type, ev.data)


def _split_tool_batches(
    tool_calls: list[dict], registry: ToolRegistry,
) -> list[tuple[bool, list[dict]]]:
    """把一轮 tool_calls 切成批：连续可并行段（spawn 或只读）并一批
    （True），其余逐个 = 串行批（False）。

    可并行 = 名字 == `_SPAWN_TOOL` 或 `registry.get(name).is_readonly`
    （084）：只读工具无副作用、无顺序依赖，同一轮并列点菜可并行。
    未注册/未声明只读的工具按写类串行（保守方向）。结果按批顺序回填，
    模型看到的顺序与点菜顺序一致。
    """

    def _parallel(name: str) -> bool:
        if name == _SPAWN_TOOL:
            return True
        tool = registry.get(name)
        return tool is not None and tool.is_readonly

    batches: list[tuple[bool, list[dict]]] = []
    i = 0
    n = len(tool_calls)
    while i < n:
        if _parallel(tool_calls[i]["name"]):
            j = i
            while j < n and _parallel(tool_calls[j]["name"]):
                j += 1
            batches.append((True, tool_calls[i:j]))
            i = j
        else:
            batches.append((False, [tool_calls[i]]))
            i += 1
    return batches


def _run_parallel(
    tool_calls: list[dict],
    agent: Agent,
    on_confirm: Callable | None,
    on_event: Callable | None,
    should_cancel: Callable | None,
) -> list[str]:
    """并行执行一批 spawn（线程池）；结果按提交顺序返回（点菜顺序=确定性）。

    spawn 是 IO-bound（子 agent 大量时间等 LLM），GIL 不碍事——线程池
    就够，不必上进程。f.result() 按 futures 提交序取，非完成序——
    结果顺序与模型点菜顺序一致（它靠位置对应 tool_call_id）。

    059：on_event 一并下发——各 worker 线程内的子 agent 事件会**交织**
    写进同一条父流（谁先跑完谁先到），故子事件带 task 摘要用于区分兄弟；
    emit 侧的序号原子性由 RunStore 的锁保证（共享收口点，一处修）。

    082 ①：should_cancel 一并下发——子 agent 的取消透传进子 run_turn。
    """
    with ThreadPoolExecutor(max_workers=len(tool_calls)) as ex:
        futures = [
            ex.submit(
                agent.execute,
                tc["name"],
                tc["arguments"],
                confirm=on_confirm,
                on_event=on_event,
                should_cancel=should_cancel,
            )
            for tc in tool_calls
        ]
        results: list[str] = []
        for f in futures:
            try:
                results.append(f.result())
            except Exception as exc:  # agent.execute 已兜底（registry 返回错误串），这里是意外
                results.append(f"错误：并行执行失败（{exc}）")
        return results


def _execute_tool_calls(
    tool_calls: list[dict],
    session: Session,
    payload: list[Message],
    agent: Agent,
    on_confirm: Callable | None,
    on_event: Callable | None,
    should_cancel: Callable | None,
) -> bool:
    """执行一轮的全部工具调用（S6b 切批：连续 spawn/只读段并行，其余串行）。

    结果按点菜顺序回填（tool 消息与 tool_call_id 一一对应，模型靠位置认）。
    返回 False = 取消命中（已 trim 半截轮），调用方应返回 CANCELLED。
    """
    for parallel_ok, batch in _split_tool_batches(tool_calls, agent.registry):
        # 协作式取消检查点②：每个批执行前（批粒度，非逐工具）
        if should_cancel and should_cancel():
            trim_incomplete_round(session.messages)
            return False
        # tool_started：并行批先全发（表示都开始了），串行批逐发
        # id（P0-3）：tool_call id 随事件外发——checkpoint 账本靠它把
        # 「点了什么菜」与「回了什么结果」配对，恢复时才能按 id 回注
        for tc in batch:
            if on_event:
                on_event("tool_started", {
                    "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"],
                })
        # 执行：连续 spawn 段用线程池并行，其余串行
        # 059：on_event 顺着 agent.execute 往下走，声明 receives_event 的
        # 工具（spawn 两件）拿到父事件缝，把子 agent 过程以 sub.* 转出来
        if parallel_ok and len(batch) > 1:
            results = _run_parallel(batch, agent, on_confirm, on_event, should_cancel)
        else:
            results = [
                agent.execute(
                    tc["name"], tc["arguments"], confirm=on_confirm, on_event=on_event,
                    should_cancel=should_cancel,
                )
                for tc in batch
            ]
        # 按序回填（点菜顺序，确定性——模型靠位置对应 tool_call_id）
        for tc, result in zip(batch, results, strict=True):
            # 结果以 role="tool" 回填，tool_call_id 对应是哪次调用
            tool_msg = Message(role="tool", tool_call_id=tc["id"], content=result)
            session.messages.append(tool_msg)
            payload.append(tool_msg)
            # 入史必须早于事件外发（P0-3 不变量：事件一旦外发，底片里已经
            # 有这件事）。checkpoint writer 挂在 on_event 缝上落盘 session，
            # 顺序反了就会存出「缺最后一条 tool 消息」的底片，白丢一次结果。
            #
            # S5b 针②：工具执行后立刻 drain 计划事件——在 tool_result 之前
            # 转发（plan.* 是这次执行的一部分，因果序在前）。事件走既有
            # on_event 缝，零新缝；server 侧点分命名默认透传，前端免费收到
            _forward_plan_events(session.plan, on_event)
            if on_event:
                on_event("tool_result", {"id": tc["id"], "name": tc["name"], "result": result})
    return True
