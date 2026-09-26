"""evalkit bash-only mini 基线（037 P4）离线验收：ScriptedLLM 驱动 runner 机制。

不变量：
- 点菜 → 独立 subprocess 执行 → 结果回灌线性历史 → 收尾后 verify 判定
- 模型一直点菜 → 步数熔断，verify 照跑判 fail（不白烧、不炸批）
- setup 失败 → 直接 fail，不烧 LLM
真模型出分归实机（一键：.venv/bin/python -m evals.baseline_agent）。
"""

import json

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from evals.baseline_agent import run_scenario


def _ordering(command: str) -> Message:
    return Message(role="assistant", content="", tool_calls=[
        {"id": "call_1", "name": "bash", "arguments": json.dumps({"command": command})}
    ])


def test_happy_path_runs_bash_and_passes(tmp_path):
    llm = ScriptedLLM([
        _ordering("echo hello world > hello.txt"),
        Message(role="assistant", content="搞定"),
    ])
    scenario = {"id": "t", "task": "写文件", "verify": "grep -qx 'hello world' hello.txt"}

    result = run_scenario(llm, scenario, tmp_path)

    assert result.passed is True
    assert result.steps == 1
    assert result.commands == ["echo hello world > hello.txt"]   # 冻结集喂裁判的地面真值
    assert (tmp_path / "hello.txt").read_text() == "hello world\n"
    # 线性历史：点菜与工具结果都回灌（基线无投影/压缩，模型看见全过程）
    assert [m.role for m in llm.calls[-1]] == ["system", "user", "assistant", "tool"]


def test_step_fuse_stops_endless_ordering(tmp_path):
    # 剧本长度 > max_steps：模型每轮都点菜，熔断即停（不白烧第 4 次调用）
    llm = ScriptedLLM([_ordering("true") for _ in range(20)])
    scenario = {"id": "t", "task": "空转", "verify": "false"}   # verify 恒 fail

    result = run_scenario(llm, scenario, tmp_path, max_steps=3)

    assert result.passed is False
    assert result.steps == 3
    assert "步数熔断" in result.note
    assert len(llm.calls) == 3            # 熔断即停，不白烧第 4 次


def test_setup_failure_skips_llm(tmp_path):
    llm = ScriptedLLM([])
    scenario = {"id": "t", "task": "跑", "setup": "exit 1", "verify": "true"}

    result = run_scenario(llm, scenario, tmp_path)

    assert result.passed is False
    assert "setup 失败" in result.note
    assert llm.calls == []                # setup 挂了不烧 LLM
