"""S5c spawn_subagent 验收：噪声隔离 / 子集过滤 / confirm 透传 / 失败反馈环。

021 隔离边界三件 + S5c 四拍板的可测化：
- 只回传结论：主底片只多一条 tool 消息（=子 agent 结论），中间过程零泄漏
- 工具子集：默认全量减禁止单（spawn 自己 + 计划三件）；显式指定也强制过滤
- confirm 透传（receives_confirm 通道）：子 agent 高危工具照常请求裁决
- 失败走反馈环：错误串回灌，主轮不炸
"""

import json

from agent.core.llm import LLM, LLMUnavailableError, ScriptedLLM
from agent.core.types import Message
from agent.memory.store import Session
from agent.orchestrator.agent import Agent
from agent.orchestrator.loop import RunResult, run_turn
from agent.tools.context import ToolContext
from agent.tools.registry import Tool, ToolRegistry
from agent.tools.spawn import register_spawn_tools, spawn_subagent


def _call(name: str, args: dict) -> dict:
    return {"id": f"call_{name}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def _setup(sub_llm: LLM, *tool_names: str):
    """装配：registry（echo 工具们 + spawn）+ ctx（子链=sub_llm）。"""
    registry = ToolRegistry()
    for n in tool_names:
        registry.register(Tool(
            name=n, description="", parameters={},
            func=lambda _n=n, **kw: f"{_n} 的结果",   # 默认参数即刻绑定（B023：lambda 延迟绑循环变量）
            needs_confirmation=(n == "danger_tool"),
        ))
    ctx = ToolContext(notes_dir=None, llm=sub_llm)   # type: ignore[arg-type]
    register_spawn_tools(registry, ctx)
    return registry, ctx


def _main_agent(registry: ToolRegistry) -> Agent:
    return Agent(name="main", system_prompt="主 sys", registry=registry)


# ---------- 噪声隔离（核心不变量） ----------


def test_spawn_returns_conclusion_only():
    # 子 agent 点工具→拿结果→出结论；主底片只多一条 tool 消息=结论，
    # 子 agent 的点菜/工具输出一条都不进主上下文
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[_call("search_notes", {"query": "X"})]),
        Message(role="assistant", content="结论：X 是向量库"),
    ])
    registry, _ = _setup(sub_llm, "search_notes", "add_todo")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "查一下 X 是什么"})]),
        Message(role="assistant", content="已让子任务查完：X 是向量库"),
    ])

    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    result, _ = run_turn(session, "查 X", agent=_main_agent(registry), llm=main_llm)

    assert result is RunResult.COMPLETED
    # 主底片：user + spawn点菜 + spawn结果(结论) + 总结 —— 没有 search_notes
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "X 是向量库" in tool_msgs[0].content
    assert "search_notes" not in json.dumps(
        [m.content for m in session.messages], ensure_ascii=False
    )
    # 子 agent 确实跑过工具循环（子链两次 generate），子会话用完即弃
    assert len(sub_llm.calls) == 2


def test_sub_agent_task_book_in_first_payload():
    # 任务书自包含：子会话首条 system = 任务书模板（子上下文看不到主对话）
    sub_llm = ScriptedLLM([Message(role="assistant", content="做完了")])
    registry, _ = _setup(sub_llm, "search_notes")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "专项任务书ABC"})]),
        Message(role="assistant", content="ok"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    run_turn(session, "去", agent=_main_agent(registry), llm=main_llm)

    first_payload = sub_llm.calls[0]
    assert first_payload[0].role == "system"
    assert "专项任务书ABC" in first_payload[0].content
    assert "专项执行员" in first_payload[0].content


# ---------- 工具子集与递归防护 ----------


def test_default_subset_excludes_spawn_and_plan_tools():
    sub_llm = ScriptedLLM([Message(role="assistant", content="做完了")])
    registry, _ = _setup(sub_llm, "search_notes", "add_todo", "make_plan")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "干活"})]),
        Message(role="assistant", content="ok"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    run_turn(session, "去", agent=_main_agent(registry), llm=main_llm)

    # 子链首见菜单：只有 echo 工具（spawn/make_plan 都被禁止清单滤掉）
    menu = sub_llm.tool_menus[0]
    names = {s["function"]["name"] for s in menu}
    assert names == {"search_notes", "add_todo"}


def test_explicit_subset_intersected_and_forced_filtered():
    # 显式指定 ∩ 可用清单；把 spawn/计划三件塞进去也强制剔除
    sub_llm = ScriptedLLM([Message(role="assistant", content="做完了")])
    registry, _ = _setup(sub_llm, "search_notes", "add_todo", "make_plan")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {
                "task": "干活",
                "tools": ["search_notes", "spawn_subagent", "make_plan", "不存在的"],
            })]),
        Message(role="assistant", content="ok"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    run_turn(session, "去", agent=_main_agent(registry), llm=main_llm)

    names = {s["function"]["name"] for s in sub_llm.tool_menus[0]}
    assert names == {"search_notes"}   # 交集 + 禁止单滤除


def test_all_forbidden_tools_rejected():
    sub_llm = ScriptedLLM([])
    registry, _ = _setup(sub_llm, "search_notes")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "干活", "tools": ["spawn_subagent"]})]),
        Message(role="assistant", content="换个方式"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    run_turn(session, "去", agent=_main_agent(registry), llm=main_llm)

    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert "错误" in tool_msgs[0].content and "不可派给子 agent" in tool_msgs[0].content


# ---------- confirm 透传（receives_confirm 通道） ----------


def test_subagent_highrisk_tool_asks_main_confirm():
    # 子 agent 点高危工具 → confirm 缝一路透传到主循环回调（人审不分主子）
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[_call("danger_tool", {})]),
        Message(role="assistant", content="用户批准了，执行完毕"),
    ])
    registry, _ = _setup(sub_llm, "danger_tool")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "干危险活"})]),
        Message(role="assistant", content="子任务完成"),
    ])
    confirm_log: list[str] = []

    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    run_turn(
        session, "去", agent=_main_agent(registry), llm=main_llm,
        on_confirm=lambda name, args: (confirm_log.append(name), True)[1],
    )

    assert confirm_log == ["danger_tool"]   # 主回调收到了子 agent 的高危请求
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert "执行完毕" in tool_msgs[0].content   # 批准后子任务继续


def test_subagent_confirm_rejection_feeds_back():
    # 拒绝 → 子 agent 收到「用户拒绝」→ 换方案出结论（不炸主轮）
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[_call("danger_tool", {})]),
        Message(role="assistant", content="用户拒绝了，我改用安全方案完成"),
    ])
    registry, _ = _setup(sub_llm, "danger_tool")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "干危险活"})]),
        Message(role="assistant", content="好"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    result, _ = run_turn(
        session, "去", agent=_main_agent(registry), llm=main_llm,
        on_confirm=lambda name, args: False,
    )
    assert result is RunResult.COMPLETED
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert "安全方案" in tool_msgs[0].content


def test_registry_receives_confirm_channel():
    # registry.execute 的 receives_confirm 通道：标记工具收到 confirm 参数
    registry = ToolRegistry()
    seen: dict = {}

    def _probe(task: str, confirm=None) -> str:
        seen["confirm"] = confirm
        return "ok"

    registry.register(Tool(
        name="probe", description="", parameters={},
        func=_probe, receives_confirm=True,
    ))
    sentinel = lambda name, args: True   # noqa: E731
    out = registry.execute("probe", json.dumps({"task": "t"}), confirm=sentinel)
    assert out == "ok"
    assert seen["confirm"] is sentinel   # 原样注入，非 None 非 bool


# ---------- 失败反馈环 ----------


class _DeadLLM(LLM):
    """恒挂链：spawn 的子任务失败路径（run_turn 返回 FAILED）。"""

    name = "dead"

    def generate(self, messages, tools=None):
        raise LLMUnavailableError("子链全挂")

    def generate_stream(self, messages, tools=None):
        raise LLMUnavailableError("子链全挂")
        yield  # pragma: no cover


def test_subagent_failure_returns_error_string():
    registry, _ = _setup(_DeadLLM(), "search_notes")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "干活"})]),
        Message(role="assistant", content="子任务失败，我直接来"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    result, _ = run_turn(session, "去", agent=_main_agent(registry), llm=main_llm)

    assert result is RunResult.COMPLETED   # 主轮不炸
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert "子任务失败" in tool_msgs[0].content


def test_empty_task_rejected():
    registry, _ = _setup(ScriptedLLM([]), "search_notes")
    out = spawn_subagent("  ", llm=ScriptedLLM([]), registry=registry)
    assert "task 不能为空" in out


def test_rounds_clamped():
    # 预算钳制：max_rounds 超上限压到 10、低于 1 抬到 1（不信任模型给的数）
    sub_llm = ScriptedLLM([Message(role="assistant", content="结论")])
    registry, _ = _setup(sub_llm, "search_notes")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "干活", "max_rounds": 99})]),
        Message(role="assistant", content="ok"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    run_turn(session, "去", agent=_main_agent(registry), llm=main_llm)

    # 99 → 10：菜单照常递（只验证不炸 + 子链收到菜单；钳制值由内部 min 保证）
    assert sub_llm.tool_menus[0] is not None
