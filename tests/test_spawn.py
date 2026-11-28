"""S5c spawn_subagent 验收：噪声隔离 / 子集过滤 / confirm 透传 / 失败反馈环。

021 隔离边界三件 + S5c 四拍板的可测化：
- 只回传结论：主底片只多一条 tool 消息（=子 agent 结论），中间过程零泄漏
- 工具子集：默认全量减禁止单（spawn 自己 + 计划三件）；显式指定也强制过滤
- confirm 透传（receives_confirm 通道）：子 agent 高危工具照常请求裁决
- 事件透传（059，receives_event 通道）：子过程以 sub.* 进父事件流——
  隔离的是主 agent 上下文，不是人的眼睛
- 失败走反馈环：错误串回灌，主轮不炸
"""

import json
import re
import subprocess
import threading
import time

import pytest

from facta.core.llm import LLM, LLMUnavailableError, ScriptedLLM
from facta.core.types import Message
from facta.memory.store import Session
from facta.orchestrator.agent import Agent
from facta.orchestrator.loop import RunResult, run_turn
from facta.tools.context import ToolContext, WorkspaceBinding
from facta.tools.registry import Tool, ToolRegistry
from facta.tools.spawn import fan_out_subagents, register_spawn_tools, spawn_subagent


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


def test_sub_events_reach_parent_stream_namespaced():
    """059：子过程以 sub.* 进父事件流，父命名空间与主底片都不受污染。

    「只回传结论」的精确语义是**上下文**隔离（主底片只多一条 tool 消息），
    不是事件流隔离——人的眼睛该看见子过程（architecture 活清单原话：
    隔离的是主 agent 上下文，不是人的眼睛）。
    """
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[_call("search_notes", {"query": "X"})]),
        Message(role="assistant", content="结论：X 是向量库"),
    ])
    registry, _ = _setup(sub_llm, "search_notes")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "查一下 X 是什么"})]),
        Message(role="assistant", content="已让子任务查完"),
    ])

    got: list[tuple[str, dict]] = []
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    result, _ = run_turn(
        session, "查 X", agent=_main_agent(registry), llm=main_llm,
        on_event=lambda t, d: got.append((t, d)),
    )

    assert result is RunResult.COMPLETED
    types = [t for t, _ in got]
    # 子过程可见：sub.tool.started/result 带子工具名 + task 摘要（并行兄弟靠它区分）
    sub_started = [d for t, d in got if t == "sub.tool.started"]
    assert sub_started and sub_started[0]["name"] == "search_notes"
    assert "查一下 X 是什么" in sub_started[0]["task"]
    assert "sub.tool.result" in types
    # 父命名空间干净：不带 sub 前缀的 tool_started 只有 spawn 自己——
    # checkpoint writer 与 frozen_eval 都按精确类型匹配，口径逐字不变
    assert {d["name"] for t, d in got if t == "tool_started"} == {"spawn_subagent"}
    # 上下文隔离仍在：主底片只多一条 tool 消息（=子结论）
    assert len([m for m in session.messages if m.role == "tool"]) == 1


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


def test_registry_receives_event_channel():
    # 059：receives_event 通道与 receives_confirm 同款——标记的工具收到 event
    # 参数，未标记的工具收不到（不给未来工具埋 TypeError 陷阱）
    registry = ToolRegistry()
    seen: dict = {}

    def _probe(task: str, event=None) -> str:
        seen["event"] = event
        return "ok"

    def _plain(task: str) -> str:
        return "plain"

    registry.register(Tool(
        name="probe", description="", parameters={}, func=_probe, receives_event=True,
    ))
    registry.register(Tool(name="plain", description="", parameters={}, func=_plain))

    sink = lambda type_, data: None   # noqa: E731
    assert registry.execute("probe", json.dumps({"task": "t"}), on_event=sink) == "ok"
    assert seen["event"] is sink
    assert registry.execute("plain", json.dumps({"task": "t"}), on_event=sink) == "plain"


def test_registry_receives_cancel_channel():
    # 082 ①：receives_cancel 通道与 receives_confirm/event 同款——标记的工具
    # 收到 should_cancel 参数，未标记的工具收不到
    registry = ToolRegistry()
    seen: dict = {}

    def _probe(task: str, should_cancel=None) -> str:
        seen["should_cancel"] = should_cancel
        return "ok"

    def _plain(task: str) -> str:
        return "plain"

    registry.register(Tool(
        name="probe", description="", parameters={}, func=_probe, receives_cancel=True,
    ))
    registry.register(Tool(name="plain", description="", parameters={}, func=_plain))

    sentinel = lambda: False   # noqa: E731
    assert registry.execute("probe", json.dumps({"task": "t"}), should_cancel=sentinel) == "ok"
    assert seen["should_cancel"] is sentinel   # 原样注入
    assert registry.execute("plain", json.dumps({"task": "t"}), should_cancel=sentinel) == "plain"


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
    status, out = spawn_subagent("  ", llm=ScriptedLLM([]), registry=registry)
    assert status is RunResult.FAILED   # 083：装配错误也归 FAILED（结构化返回）
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


# ---------- 线五② fan-out 聚合 + 只回传摘要收严 ----------


class _EchoLLM(LLM):
    """逐调用确定性的假模型：回显任务书（最后一条 user 消息）。

    并发安全（fan-out 并行共用一个实例）：只有只读 self.delay + 局部变量。
    """

    name = "echo"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    def generate(self, messages, tools=None) -> Message:
        if self.delay:
            time.sleep(self.delay)
        task = next((m.content for m in reversed(messages) if m.role == "user"), "?")
        return Message(role="assistant", content=f"完成[{task}]")


def test_fanout_aggregates_in_order_with_header():
    # 一次派两件：聚合块按任务序号排列（确定性，不靠模型对位），
    # 头行给成败计数；直调函数验证聚合格式
    registry, _ = _setup(_EchoLLM(), "search_notes")
    out = fan_out_subagents(["甲", "乙"], llm=_EchoLLM(), registry=registry)

    assert "〔fan-out 聚合：2 件，2 完成、0 未完成〕" in out
    assert out.index("完成[甲]") < out.index("完成[乙]")
    assert "[1/2]" in out and "[2/2]" in out


def test_fanout_via_registry_single_tool_message():
    # 菜单路径：一次调用一条结果回灌主底片（噪声隔离同款），聚合块完整
    registry, _ = _setup(_EchoLLM(), "search_notes")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("fan_out_subagents", {"tasks": ["甲", "乙"]})]),
        Message(role="assistant", content="两件都做完了"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    result, _ = run_turn(session, "并行查两件", agent=_main_agent(registry), llm=main_llm)

    assert result is RunResult.COMPLETED
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "〔fan-out 聚合：2 件" in tool_msgs[0].content


def test_fanout_runs_tasks_in_parallel():
    # 两件 0.3s 的子任务并行跑，总耗时 < 串行（0.6s）——fan-out 卖点就是并行
    registry, _ = _setup(_EchoLLM(), "search_notes")
    start = time.perf_counter()
    out = fan_out_subagents(["甲", "乙"], llm=_EchoLLM(delay=0.3), registry=registry)
    elapsed = time.perf_counter() - start

    assert "2 完成" in out
    assert elapsed < 0.5, f"耗时 {elapsed:.2f}s，疑似未并行（串行应 ≈0.6s）"


def test_fanout_rejects_blank_and_oversized_batches():
    registry, _ = _setup(_EchoLLM(), "search_notes")
    assert "tasks 不能为空" in fan_out_subagents(["  ", ""], llm=_EchoLLM(), registry=registry)
    # 超上限拒单（不静默截批）：9 件 > MAX_FANOUT=8，错误串回灌让模型拆批
    out = fan_out_subagents([f"任务{i}" for i in range(9)], llm=_EchoLLM(), registry=registry)
    assert "最多 fan-out 8 件" in out


def test_fanout_forbidden_for_sub_agents():
    # 递归防护：fan_out_subagents 进 _FORBIDDEN，子 agent 菜单里拿不到（2 层封顶）
    sub_llm = ScriptedLLM([Message(role="assistant", content="做完了")])
    registry, _ = _setup(sub_llm, "search_notes")
    main_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "干活"})]),
        Message(role="assistant", content="ok"),
    ])
    session = Session()
    session.messages.append(Message(role="system", content="主 sys"))
    run_turn(session, "去", agent=_main_agent(registry), llm=main_llm)

    names = {s["function"]["name"] for s in sub_llm.tool_menus[0]}
    assert "fan_out_subagents" not in names


def test_conclusion_capped_with_pointer():
    # 只回传摘要收严：超长结论截断 + 指针（细节走事件流/落盘，不进主底片）
    long_text = "长" * 5000
    sub_llm = ScriptedLLM([Message(role="assistant", content=long_text)])
    registry, _ = _setup(sub_llm, "search_notes")
    status, out = spawn_subagent("干活", llm=sub_llm, registry=registry)

    assert status is RunResult.COMPLETED
    assert len(out) < 3000
    assert out.startswith("长")
    assert "已截断" in out


# ---------- P0 B6：子沙箱绑定继承（Spec 1 §2.4） ----------


def _git(repo, *args: str) -> str:
    """真 git 命令（测试地基，test_workspace_binding 同款）：成功回 stdout。"""
    proc = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture()
def bound_repo(tmp_path, monkeypatch):
    """绑定仓库地基：真 git 仓库（main + 基线提交）+ worktree 模块根指过来。

    仓库与 WORKTREES_DIR 分家（test_worktree.git_repo 同款拓扑）：沙箱目录
    住 tmp/worktrees，git 元数据归属 tmp/repo——与生产同构（沙箱集中住
    Facta data/，分支/提交进用户绑定仓库）。
    """
    from facta.tools import worktree as wt_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "base.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    monkeypatch.setattr(wt_mod, "WORKSPACE_ROOT", repo)
    monkeypatch.setattr(wt_mod, "WORKTREES_DIR", tmp_path / "worktrees")
    return repo


def test_b6_sub_sandbox_inherits_binding(bound_repo, tmp_path):
    # 父子继承：子沙箱建在绑定仓库、合回落父工作区（任务沙箱）、target_branch 不被直接写
    repo = bound_repo
    baseline = _git(repo, "rev-parse", "HEAD")
    # 父工作区（任务沙箱）：绑定仓库的另一 worktree——子沙箱改动的合回落点
    parent_wt = tmp_path / "parent_wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(parent_wt), "-b", "task/sandbox", baseline],
        cwd=repo, check=True,
    )
    binding = WorkspaceBinding(repo_root=repo, target_branch="main", baseline=baseline)
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("write_file", {"path": "feat.py", "content": "print('feat')\n"}),
        ]),
        Message(role="assistant", content="结论：已创建 feat.py"),
    ])
    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=tmp_path / "notes", workspace_root=parent_wt,
                      binding=binding, llm=sub_llm)
    register_spawn_tools(registry, ctx)

    refs_seen: list[str] = []

    def _confirm(name, args):
        # 裁决缝跑在合回之前：此刻子分支应已挂在绑定仓库 refs 上
        refs_seen.append(_git(repo, "branch", "--list", "agent/*"))
        return True

    status, conclusion = spawn_subagent(
        "建 feat.py", llm=sub_llm, registry=registry,
        worktree=True, ctx=ctx, confirm=_confirm,
    )

    assert status is RunResult.COMPLETED
    assert "feat.py" in conclusion
    # ① 子沙箱分支/提交出现在绑定仓库 refs（不落 Facta 主仓、不落别处）
    assert refs_seen and "agent/" in refs_seen[0]
    assert "spawn:" in _git(repo, "log", "--oneline", "task/sandbox")
    # ② 合回落父工作区（任务沙箱）：改动并入任务候选分支
    assert (parent_wt / "feat.py").read_text(encoding="utf-8") == "print('feat')\n"
    # ③ 绑定 target_branch 不被直接写：main 头不动、检出工作区零污染
    assert _git(repo, "rev-parse", "main") == baseline
    assert not (repo / "feat.py").exists()
    assert _git(repo, "status", "--short") == ""


# ---------- P0 B7/B8：fan-out 只读默认 + 显式写拦截（Spec 1 §2.5） ----------


def test_b7_fanout_default_toolset_excludes_write_tools():
    # 缺省条目工具集不含 write_file/run_command（程序侧判定：看程序递给子链的菜单）
    sub_llm = ScriptedLLM([Message(role="assistant", content="做完了")])
    registry, _ = _setup(sub_llm, "search_notes", "write_file", "run_command")
    fan_out_subagents(["查甲"], llm=sub_llm, registry=registry)

    names = {s["function"]["name"] for s in sub_llm.tool_menus[0]}
    assert names == {"search_notes"}
    assert "write_file" not in names and "run_command" not in names


class _CountingEcho(_EchoLLM):
    """回显 + 调用计数（线程安全）：B8 断言被拦条目零执行。"""

    name = "counting-echo"

    def __init__(self, delay: float = 0.0) -> None:
        super().__init__(delay)
        self.calls = 0
        self._lock = threading.Lock()

    def generate(self, messages, tools=None) -> Message:
        with self._lock:
            self.calls += 1
        return super().generate(messages, tools)


def test_b8_fanout_write_request_entry_intercepted():
    # 显式请求写工具的条目：零执行、聚合该行固定回执；同批兄弟条目照常有结论
    registry, _ = _setup(ScriptedLLM([]), "search_notes", "write_file")
    llm = _CountingEcho()
    out = fan_out_subagents(
        [
            {"task": "改配置文件", "tools": ["write_file"]},
            "查甲",
            {"task": "查乙", "tools": ["search_notes"]},
        ],
        llm=llm, registry=registry,
    )

    # 零执行：被拦条目一次都没进模型（兄弟两条各跑一次）
    assert llm.calls == 2
    # 聚合该行含固定回执（不静默裁剪：不是把 write_file 摘掉照跑）
    blocked = next(b for b in re.split(r"(?m)^(?=\[\d+/\d+\])", out) if "改配置文件" in b)
    assert "已拦截：fan-out 不允许写工具，请改用 spawn_subagent(worktree=True) 或任务沙箱" in blocked
    # 同批其余条目照常并行聚合出结论
    assert "〔fan-out 聚合：3 件，2 完成、1 未完成〕" in out
    assert "完成[查甲]" in out and "完成[查乙]" in out


def test_b8_shared_write_tools_intercepted_without_rejecting_call():
    # 共用 tools 点名写工具：依赖它的条目同样拦截回执，整个调用不拒单
    registry, _ = _setup(ScriptedLLM([]), "search_notes", "write_file")
    llm = _CountingEcho()
    out = fan_out_subagents(
        ["查甲", {"task": "查乙", "tools": ["search_notes"]}],
        llm=llm, registry=registry, tools=["write_file", "search_notes"],
    )

    assert llm.calls == 1                      # 只有自带干净工具的条目执行
    assert "已拦截：fan-out 不允许写工具" in out
    assert "完成[查乙]" in out                  # 兄弟条目照常出结论
