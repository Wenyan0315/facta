"""S6c spawn_step 验收：计划步骤派发（真编排的焊缝）。

不变量：
- 自动回写：spawn_step 把步骤标 in_progress → 派子任务 → 按成败回写
  done/failed，模型不用手动 update_plan_step
- 校验统一：无活跃计划 / step_id 不在计划 / 已终态 → 错误串回灌（薄包装，
  与 plan.py 工具同款——board 是唯一校验闸门）
- 复用 spawn：噪声隔离/工具子集/worktree 隔离/确认透传全保留（调 spawn_subagent）
- 子 agent 禁 spawn_step（_FORBIDDEN 加新成员，执行者不编排）
"""

import json

from facta.core.llm import LLM, LLMUnavailableError, ScriptedLLM
from facta.core.types import Message
from facta.memory.plan import StepStatus
from facta.memory.store import Session
from facta.tools.context import ToolContext
from facta.tools.plan import register_plan_tools
from facta.tools.registry import ToolRegistry
from facta.tools.spawn import _FORBIDDEN, register_spawn_tools


class FailLLM(LLM):
    """一叫就挂的假模型：子 agent 的 run_turn 撞 LLMUnavailableError → FAILED。"""

    name = "fail"

    def generate(self, messages, tools=None) -> Message:
        raise LLMUnavailableError("模型不可用")


def _setup(sub_llm: LLM) -> tuple[ToolRegistry, Session]:
    """装配：registry（计划三件 + spawn 双件）+ ctx（带 session + 子链）。"""
    registry = ToolRegistry()
    session = Session()
    ctx = ToolContext(notes_dir=None, session=session, llm=sub_llm)   # type: ignore[arg-type]
    register_plan_tools(registry, ctx)
    register_spawn_tools(registry, ctx)
    return registry, session


def _call(name: str, args: dict) -> dict:
    return {"id": f"call_{name}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def _make_plan(session: Session) -> None:
    session.plan.make_plan([{"title": "查资料"}, {"title": "写总结"}])


# ---------- 核心：自动回写步骤状态 ----------


def test_spawn_step_executes_and_marks_done():
    # spawn_step 一步到位：步骤 1 自动 done（带子任务结论），步骤 2 保持 pending
    sub_llm = ScriptedLLM([Message(role="assistant", content="结论：查到了 RAG 笔记")])
    registry, session = _setup(sub_llm)
    _make_plan(session)

    out = registry.execute("spawn_step", json.dumps({"step_id": 1, "task": "查资料"}))
    assert "执行完成" in out
    assert "查到了 RAG 笔记" in out          # 子任务结论回灌进 tool 结果
    assert "当前计划" in out                 # S5b 回灌导航：带最新视图

    view = session.plan.view()
    assert view.steps[0].status is StepStatus.DONE
    assert view.steps[0].note == "结论：查到了 RAG 笔记"   # 结论即步骤产出
    assert view.steps[1].status is StepStatus.PENDING      # 未动的步骤不受影响


def test_spawn_step_failure_marks_failed():
    # 子任务模型挂 → 步骤 failed（诚实标注，不虚报 done）
    registry, session = _setup(FailLLM())
    _make_plan(session)

    out = registry.execute("spawn_step", json.dumps({"step_id": 1, "task": "查资料"}))
    assert "执行失败" in out
    assert session.plan.view().steps[0].status is StepStatus.FAILED


# ---------- 校验（board 统一闸门） ----------


def test_spawn_step_requires_active_plan():
    registry, session = _setup(ScriptedLLM([]))
    # 无活跃计划
    out = registry.execute("spawn_step", json.dumps({"step_id": 1, "task": "x"}))
    assert "没有活跃计划" in out


def test_spawn_step_rejects_bad_step_id():
    registry, session = _setup(ScriptedLLM([]))
    _make_plan(session)
    out = registry.execute("spawn_step", json.dumps({"step_id": 99, "task": "x"}))
    assert "不在当前计划" in out


def test_spawn_step_rejects_terminal_step():
    registry, session = _setup(ScriptedLLM([]))
    _make_plan(session)
    # 步骤 2 先终态化，再派发它 → 拒绝
    session.plan.update_step(2, "done", note="已经做完")
    out = registry.execute("spawn_step", json.dumps({"step_id": 2, "task": "x"}))
    assert "已是终态" in out


# ---------- 子 agent 权限 ----------


def test_spawn_step_forbidden_to_subagent():
    # spawn_step 加进 _FORBIDDEN：子 agent 是执行者不是编排者
    assert "spawn_step" in _FORBIDDEN
