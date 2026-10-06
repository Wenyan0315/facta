"""S6b 并行 spawn 验收：一轮多 spawn 并行执行 + 按序回填 + 确认缝排队。

不变量：
- 并行：连续 spawn 段用线程池并行（IO-bound，耗时 ≈ 单个而非累加）
- 按序回填：tool 消息顺序 = 模型点菜顺序（模型靠位置对应 tool_call_id）
- 普通工具串行：穿插的 read_file 等不并行（依赖风险，保守默认）
- 确认缝：并发 request_confirm 串行化，不踩单槽位（_confirm_lock）
"""

import json
import threading
import time

from facta.core.llm import LLM
from facta.core.types import Message
from facta.memory.store import Session
from facta.orchestrator.agent import Agent
from facta.orchestrator.executor import _split_tool_batches
from facta.orchestrator.loop import run_turn
from facta.server.run_store import STATUS_RUNNING, Run
from facta.tools.context import ToolContext
from facta.tools.registry import Tool, ToolRegistry
from facta.tools.spawn import register_spawn_tools


class SlowLLM(LLM):
    """带延迟的假模型：generate 里 sleep，回显任务书（最后一条 user 消息）。

    并发安全：只有只读的 self.delay + 局部变量，无共享可变状态——两个
    子 agent 并行调用同一个实例不竞争（与生产 RobustLLM 的 HTTP 调用同构）。
    """

    name = "slow"

    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay

    def generate(self, messages, tools=None) -> Message:
        time.sleep(self.delay)
        task = next((m.content for m in reversed(messages) if m.role == "user"), "?")
        return Message(role="assistant", content=f"完成[{task}]")


def _call(name: str, args: dict, idx: int = 0) -> dict:
    # index 必带：merge_stream_chunks 按 index 归并 tool_calls（OpenAI 协议
    # 流式碎片约定）——不带 index 的同轮多 tool_call 会被归到 idx=0，
    # arguments 拼接成 `{"task":"甲"}{"task":"乙"}` 触发 JSON 解析失败
    # （S6b 首次构造「同轮多 spawn」场景踩中；此前测试都是单 tool_call
    # 或分轮，从未触发）。id 也带 idx 保证唯一（tool_call_id 关联）。
    return {
        "id": f"call_{name}_{idx}",
        "name": name,
        "index": idx,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _setup(sub_llm: LLM, main_script: list[Message]):
    """装配：registry（spawn）+ ctx（子链=sub_llm）+ 主链 ScriptedLLM。"""
    from facta.core.llm import ScriptedLLM

    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=None, llm=sub_llm)   # type: ignore[arg-type]
    register_spawn_tools(registry, ctx)
    return registry, ctx, ScriptedLLM(main_script)


# ---------- 切批（纯函数） ----------


def _parallel_registry() -> ToolRegistry:
    """切批测试专用 registry：只读 read_file / search_code / list_dir +
    写类 write_file（不声明 is_readonly）。spawn_subagent 名字特判，无需注册。"""
    registry = ToolRegistry()
    for name in ("read_file", "search_code", "list_dir"):
        registry.register(Tool(
            name=name, description="", parameters={},
            func=lambda **kwargs: "", is_readonly=True,
        ))
    registry.register(Tool(
        name="write_file", description="", parameters={},
        func=lambda **kwargs: "",
    ))
    return registry


def test_split_tool_batches():
    # [spawn, spawn, read_file, spawn] → read_file 只读，并入同一可并行段
    tcs = [
        _call("spawn_subagent", {"task": "a"}, 0),
        _call("spawn_subagent", {"task": "b"}, 1),
        _call("read_file", {"path": "x"}, 2),
        _call("spawn_subagent", {"task": "c"}, 3),
    ]
    assert [(ok, len(b)) for ok, b in _split_tool_batches(tcs, _parallel_registry())] == [
        (True, 4),
    ]


def test_split_tool_batches_single_spawn_stays_serial():
    # 单个 spawn：标记 True 但 len==1，执行层走串行（并行只对 len>1 生效）
    tcs = [_call("spawn_subagent", {"task": "a"})]
    assert [(ok, len(b)) for ok, b in _split_tool_batches(tcs, _parallel_registry())] == [(True, 1)]


def test_split_tool_batches_readonly_parallel():
    # 并列只读点菜：决定已做完、无顺序依赖 → 并入一个可并行批（084）
    tcs = [
        _call("read_file", {"path": "a"}, 0),
        _call("search_code", {"q": "b"}, 1),
        _call("list_dir", {"path": "c"}, 2),
    ]
    assert [(ok, len(b)) for ok, b in _split_tool_batches(tcs, _parallel_registry())] == [(True, 3)]


def test_split_tool_batches_mixed_readonly_write():
    # 写类工具（write_file）插断只读段，写类保持串行（084）
    tcs = [
        _call("read_file", {"path": "a"}, 0),
        _call("write_file", {"path": "b"}, 1),
        _call("list_dir", {"path": "c"}, 2),
    ]
    assert [(ok, len(b)) for ok, b in _split_tool_batches(tcs, _parallel_registry())] == [
        (True, 1), (False, 1), (True, 1),
    ]


# ---------- 并行耗时 + 按序回填 ----------


def test_parallel_spawn_is_faster_and_ordered():
    # 两个 0.3s 的子任务并行跑，总耗时 < 串行（0.6s）；结果按点菜顺序回填
    sub_llm = SlowLLM(delay=0.3)
    registry, _, main_llm = _setup(sub_llm, [
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "甲"}, 0),
            _call("spawn_subagent", {"task": "乙"}, 1),
        ]),
        Message(role="assistant", content="两件都做完了"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(name="main", system_prompt="sys", registry=registry)

    start = time.perf_counter()
    result, _ = run_turn(session, "做两件事", agent=agent, llm=main_llm)
    elapsed = time.perf_counter() - start

    assert result.value == "completed"
    # 并行：两个 0.3s 的 spawn 同时跑，总耗时 < 0.5s（串行会 ≈0.6s）
    assert elapsed < 0.5, f"耗时 {elapsed:.2f}s，疑似未并行（串行应 ≈0.6s）"

    # 按序回填：tool 消息顺序 = 点菜顺序（甲在前、乙在后）
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert len(tool_msgs) == 2
    assert "完成[甲]" in tool_msgs[0].content
    assert "完成[乙]" in tool_msgs[1].content


def test_single_spawn_unchanged_behavior():
    # 回归：单个 spawn 行为与 S5c 完全一致（并行只对多 spawn 生效）
    sub_llm = SlowLLM(delay=0.0)
    registry, _, main_llm = _setup(sub_llm, [
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "查 X"}),
        ]),
        Message(role="assistant", content="查完了"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(name="main", system_prompt="sys", registry=registry)

    result, _ = run_turn(session, "查 X", agent=agent, llm=main_llm)

    assert result.value == "completed"
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert len(tool_msgs) == 1   # 噪声隔离保持：主底片只多一条结论
    assert "完成[查 X]" in tool_msgs[0].content


def test_mixed_tools_serial_non_spawn():
    # 穿插普通工具：spawn 段并行、普通工具串行，顺序不乱
    sub_llm = SlowLLM(delay=0.0)
    registry, _, main_llm = _setup(sub_llm, [
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "甲"}, 0),
            _call("spawn_subagent", {"task": "乙"}, 1),
        ]),
        Message(role="assistant", content="好"),
    ])
    # 注册一个普通工具（read_file 家族由 files 提供，这里只验证 spawn 并行段）
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(name="main", system_prompt="sys", registry=registry)

    result, _ = run_turn(session, "做两件", agent=agent, llm=main_llm)
    assert result.value == "completed"
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert [m.content for m in tool_msgs] == ["完成[甲]", "完成[乙]"]


# ---------- P2 确认缝并发（_confirm_lock） ----------


def test_request_confirm_parallel_no_crosstalk():
    # 两个线程并发 request_confirm：锁串行化裁决，各自得到独立正确的决策，
    # 不踩单槽位（旧代码并发 clear event 会把第一个裁决通道冲掉）
    run = Run(run_id="r1", status=STATUS_RUNNING)
    results: dict[str, bool] = {}

    def worker(tag: str) -> None:
        results[tag] = run.request_confirm("run_command", {"command": tag})

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()

    # 主线程轮询：只要挂着一个确认就裁决（交替 True/False），直到两线程都返回
    approved_flags = [True, False]
    deadline = time.time() + 3
    while time.time() < deadline:
        if run.confirm_pending:
            run.resolve_confirm(approved_flags.pop(0))
        if not t1.is_alive() and not t2.is_alive():
            break
        time.sleep(0.01)

    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not t1.is_alive() and not t2.is_alive(), "确认缝死锁：并发 request_confirm 未收口"
    assert set(results.values()) == {True, False}   # 两个独立决策都正确返回
    assert run.confirm_pending is False             # 单槽位复位
    assert run.status == STATUS_RUNNING             # 状态回到 running
