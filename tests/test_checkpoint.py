"""P0-3（038）Run checkpoint 与崩溃恢复的离线验收。

守两条不变量：
  ① 账本只存底片表达不了的东西，且坏行不炸——SIGKILL 留下半截 JSON 是主场景，
     不是防御性编程；
  ② heal 之后底片合法（孤儿 tool_calls 会被 API 400），且**已成功的调用不重复
     执行**（038 P2：改为回注原始结果）。

真 kill 的端到端验收住在 evals/resume_eval.py（038 反方意见 1 拍板的载体）——
本文件不 fork 进程，只把语义钉死。
"""

import json

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.store import Session
from agent.orchestrator.agent import Agent
from agent.orchestrator.checkpoint import CheckpointWriter, Ledger, heal, read_ledger
from agent.orchestrator.loop import RunResult, run_turn
from agent.tools.registry import Tool, ToolRegistry

_NO_ARGS = {"type": "object", "properties": {}}


def _registry(effects: list[str]) -> ToolRegistry:
    """三档工具：非幂等写（tick）/ 只读（peek）/ 写类但可重做（bump）。

    effects 是外部可观察的副作用清单——「不重复执行」靠数它的长度断言。
    """
    reg = ToolRegistry()
    reg.register(Tool(
        name="tick", description="记一次副作用（重复调用会记两次）",
        parameters=_NO_ARGS, func=lambda: (effects.append("tick"), "已记账")[1],
    ))
    reg.register(Tool(
        name="peek", description="只读看一眼现场", parameters=_NO_ARGS,
        func=lambda: "现场干净", is_readonly=True,
    ))
    reg.register(Tool(
        name="bump", description="覆写同一个值（重复执行结果一致）",
        parameters=_NO_ARGS, func=lambda: (effects.append("bump"), "已覆写")[1],
        idempotent=True,
    ))
    return reg


def _dangling(call_id: str = "c1", name: str = "tick") -> Session:
    """崩溃现场：assistant 点了菜，tool 结果没回填（底片尾部悬挂）。"""
    session = Session()
    session.messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="跑一下"),
        Message(role="assistant", content="",
                tool_calls=[{"id": call_id, "name": name, "arguments": "{}"}]),
    ]
    return session


# ── 账本读写 ──────────────────────────────────────────────

def test_read_ledger_missing_file_is_empty(tmp_path):
    """没有账本（全新会话 / CLI 从没写过）不该炸，按「一无所知」处理。"""
    ledger = read_ledger(tmp_path / "nope.jsonl")
    assert ledger.intents == {} and ledger.results == {}


def test_read_ledger_skips_truncated_line(tmp_path):
    """SIGKILL 落在写盘中途：最后一行半截 JSON，丢掉它而不是崩掉。"""
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"type":"intent","id":"c1","name":"tick"}\n'
        '{"type":"result","id":"c1","res',      # 半截，没有结尾引号与大括号
        encoding="utf-8",
    )
    ledger = read_ledger(path)
    assert ledger.intents == {"c1": "tick"}
    assert ledger.results == {}                 # 丢的是「最后一次结果」→ 走保守档


def test_writer_started_appends_intent_before_saving(tmp_path):
    """顺序不变量：save 回调被调用时 intent 必须已在盘上。

    反了就分不清「没跑过」与「跑了但结果丢了」——恢复语义全靠这个先后。
    """
    path = tmp_path / "s.jsonl"
    snapshots: list[str] = []
    writer = CheckpointWriter(path, lambda: snapshots.append(path.read_text(encoding="utf-8")))
    writer.begin("run-1")
    writer.on_event("tool_started", {"id": "c1", "name": "tick", "arguments": "{}"})
    assert len(snapshots) == 1
    assert '"intent"' in snapshots[0]


def test_writer_result_goes_to_ledger_only(tmp_path):
    """结果全文进账本（不截断，恢复时要原样回注），但不触发底片落盘。

    每工具只写一次全量底片（tool_started 那次）——038 反方意见 2 的 I/O 答复。
    """
    path = tmp_path / "s.jsonl"
    saves: list[int] = []
    writer = CheckpointWriter(path, lambda: saves.append(1))
    writer.begin("run-1")
    writer.on_event("tool_result", {"id": "c1", "name": "tick", "result": "已记账"})
    writer.on_event("plan.step_updated", {"id": 1, "status": "done"})
    writer.end("run-1", "completed")
    assert saves == [1]                          # 只有 plan.* 落盘，result 不落
    types = [json.loads(x)["type"] for x in path.read_text(encoding="utf-8").splitlines()]
    assert types == ["run", "result", "done"]


def test_writer_ignores_sub_events(tmp_path):
    """059：子 agent 的 sub.* 事件不进账本、不触发底片落盘。

    子调用不属于父底片（父底片里只有 spawn 那一条 tool 消息）；记进账本，
    恢复时会按父 tool_call_id 找不到对应消息。writer 只认精确类型
    （tool_started / tool_result / plan.*），点分 sub.* 天然被忽略——
    这是 059 敢把子过程接进父事件流的底气。
    """
    path = tmp_path / "s.jsonl"
    saves: list[int] = []
    writer = CheckpointWriter(path, lambda: saves.append(1))
    writer.begin("run-1")
    writer.on_event("sub.tool.started", {"id": "c9", "name": "search_notes", "arguments": "{}"})
    writer.on_event("sub.tool.result", {"id": "c9", "name": "search_notes", "result": "子结果"})
    writer.end("run-1", "completed")
    assert saves == []                       # 子事件不触发底片落盘
    types = [json.loads(x)["type"] for x in path.read_text(encoding="utf-8").splitlines()]
    assert types == ["run", "done"]          # 账本里没有 intent/result 行


def test_begin_truncates_previous_run(tmp_path):
    """每轮重写账本：历史 run 的记录没有消费者，留着就是无界增长。"""
    path = tmp_path / "s.jsonl"
    saved: list[int] = []
    first = CheckpointWriter(path, lambda: saved.append(1))
    first.begin("run-1")
    first.on_event("tool_started", {"id": "c1", "name": "tick", "arguments": "{}"})

    CheckpointWriter(path, lambda: saved.append(1)).begin("run-2")
    records = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [r["type"] for r in records] == ["run"]
    assert records[0]["run_id"] == "run-2"


# ── heal：补齐悬挂轮次 ────────────────────────────────────

def test_heal_reinjects_recorded_result():
    """038 P2：已成功的调用不重复执行，回注原始结果。"""
    session = _dangling()
    assert heal(session, Ledger(results={"c1": "已记账"}), _registry([])) == 1
    tail = session.messages[-1]
    assert (tail.role, tail.tool_call_id, tail.content) == ("tool", "c1", "已记账")


def test_heal_marks_unfinished_non_idempotent_as_unverified():
    """发起过、结果未知 + 非幂等 → 叫模型先核验现场，不许盲目重试。"""
    session = _dangling(name="tick")
    heal(session, Ledger(intents={"c1": "tick"}), _registry([]))
    assert "核验" in session.messages[-1].content


def test_heal_allows_redo_for_idempotent_and_readonly():
    """幂等写（bump）与只读（peek，免声明）→ 直接说可以重做。"""
    for name in ("bump", "peek"):
        session = _dangling(name=name)
        heal(session, Ledger(intents={"c1": name}), _registry([]))
        assert "重新调用" in session.messages[-1].content


def test_heal_reports_never_started_call():
    """账本里连发起记录都没有 → 没跑过；非幂等的仍提醒看一眼现场。"""
    session = _dangling(name="tick")
    heal(session, Ledger(), _registry([]))
    assert "没跑过" in session.messages[-1].content


def test_heal_noop_on_complete_tail():
    """底片尾部是完整边界（assistant 纯文本）→ 一条不补。"""
    session = _dangling()
    session.messages.append(Message(role="assistant", content="收工"))
    assert heal(session, Ledger(intents={"c1": "tick"}), _registry([])) == 0
    assert len(session.messages) == 4


def test_heal_fills_only_unanswered_calls():
    """并行批崩在中间：已回填的原样留着，只补缺的那条。"""
    session = _dangling()
    session.messages[-1] = Message(role="assistant", content="", tool_calls=[
        {"id": "c1", "name": "tick", "arguments": "{}"},
        {"id": "c2", "name": "peek", "arguments": "{}"},
    ])
    session.messages.append(Message(role="tool", tool_call_id="c1", content="已记账"))

    assert heal(session, Ledger(results={"c2": "现场干净"}), _registry([])) == 1
    assert [m.content for m in session.messages[-2:]] == ["已记账", "现场干净"]


# ── loop 侧接线 ───────────────────────────────────────────

def test_tool_events_carry_call_id():
    """账本靠 id 把意图与结果配对——事件 payload 必须带 tool_call id。"""
    events: list[tuple[str, dict]] = []
    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    llm = ScriptedLLM([
        Message(role="assistant", content="",
                tool_calls=[{"id": "c9", "name": "peek", "arguments": "{}"}]),
        Message(role="assistant", content="好了"),
    ])
    run_turn(session, "看看", agent=Agent(name="t", system_prompt="sys", registry=_registry([])),
             llm=llm, on_event=lambda t, d: events.append((t, d)))
    assert {t: d["id"] for t, d in events if t in ("tool_started", "tool_result")} == {
        "tool_started": "c9", "tool_result": "c9",
    }


def test_resume_does_not_reexecute_completed_call(tmp_path):
    """端到端（离线）：kill → heal → run_turn(user_text=None) 续跑，副作用不翻倍。"""
    effects: list[str] = []
    reg = _registry(effects)
    agent = Agent(name="t", system_prompt="sys", registry=reg)
    path = tmp_path / "s.jsonl"

    # 崩溃前：模型点了 tick、工具真跑了、账本记全，进程在回填前被杀
    session = _dangling()
    effects.append("tick")
    writer = CheckpointWriter(path, lambda: None)
    writer.begin("run-1")
    writer.on_event("tool_started", {"id": "c1", "name": "tick", "arguments": "{}"})
    writer.on_event("tool_result", {"id": "c1", "name": "tick", "result": "已记账"})

    # 恢复：heal 读账本补齐底片，续跑不追加新提问
    assert heal(session, read_ledger(path), reg) == 1
    result, reply = run_turn(
        session, None, agent=agent,
        llm=ScriptedLLM([Message(role="assistant", content="收工")]),
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content == "收工"
    assert effects == ["tick"]                                     # 没有第二次副作用
    assert [m.content for m in session.messages if m.role == "user"] == ["跑一下"]   # 不重复提问
