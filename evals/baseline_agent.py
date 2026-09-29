"""evalkit bash-only mini 基线（037 P4）：mini-swe-agent 形态的永久回归基线。

用途：任何执行类机制（hooks、ACI、模型分层……）评审必须回答
「同场景比基线好在哪、贵多少」。形态对照 mini-swe-agent：
  - 仅 bash 工具（每步独立 subprocess，无 shell 状态残留）
  - 线性历史（无摘要/投影/计划/路由/记忆——项目机制一概不挂）
基线故意极简：它就是「机制虚荣」的对照组，不复用项目内任何编排件。

另一用法：`frozen_eval --baseline` 把冻结集的真任务喂给本基线（同一 worktree
副本、同一 setup、同一把判分尺子，只有执行体不同）——机制的价值 = full 臂减 bash 臂。

一键出分（仓库根）：
    .venv/bin/python -m evals.baseline_agent [--provider deepseek-flash] [--max-steps 15]

场景文件（JSONL，每行一条）：
    {"id": "...", "task": "...", "verify": "...", "setup": "..."（可选）}
判定：任务跑完后在场景临时目录执行 verify，exit 0 = 通过。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from facta.core.llm import LLM, get_llm
from facta.core.telemetry import UsageLedger
from facta.core.types import Message

MAX_STEPS = 15          # 步数熔断：基线卡死不允许白烧（出分记 fail 即可）
MAX_OUTPUT_CHARS = 4000

_SYSTEM = (
    "你是 bash-only 迷你 agent：只能靠 bash 工具一步步完成任务。"
    "每次调用都是独立子进程（cd 等状态不会残留）。任务完成后直接用文字回答，不要再调用工具。"
)

_BASH_TOOL = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a bash command in the working directory (fresh subprocess each call).",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "bash 命令"}},
            "required": ["command"],
        },
    },
}]


@dataclass
class ScenarioResult:
    id: str
    passed: bool
    steps: int            # 实际执行的 bash 批数（点菜轮数）
    note: str = ""
    answer: str = ""      # 收尾那一轮的正文：冻结集要给基线臂打质量分，不能只留通过与否
    commands: list[str] = field(default_factory=list)   # 实际执行过的命令（冻结集喂裁判的地面真值）


def _bash(command: str, cwd: Path, timeout: int = 60) -> str:
    """每步独立 subprocess（mini-swe-agent 形态）：exit code + 合并输出（截断）。"""
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True,
            text=True, errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out (>{timeout}s)"
    output = ((proc.stdout or "") + (proc.stderr or ""))[:MAX_OUTPUT_CHARS]
    return f"exit code: {proc.returncode}\n{output}".strip()


def run_scenario(
    llm: LLM, scenario: dict, workdir: Path, max_steps: int = MAX_STEPS
) -> ScenarioResult:
    """跑一个场景：setup（可选）→ 线性循环（点菜→执行→回灌）→ verify 判定。

    循环只有两个出口：模型不点菜了（收尾）或步数熔断。verify 恒执行——
    熔断/半途而废的场景由 verify 判 fail，不需要额外状态机。
    """
    sid = str(scenario.get("id", "?"))
    setup = scenario.get("setup")
    if setup:
        proc = subprocess.run(
            setup, shell=True, cwd=workdir, capture_output=True,
            text=True, errors="replace", check=False,
        )
        if proc.returncode != 0:
            return ScenarioResult(sid, False, 0, f"setup 失败：{proc.stderr.strip()[:200]}")

    messages = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content=str(scenario["task"])),
    ]
    steps = 0
    answer = ""
    commands: list[str] = []
    for step in range(1, max_steps + 1):
        reply = llm.generate(messages, tools=_BASH_TOOL)
        if not reply.tool_calls:      # 模型认为做完了 → 收尾
            answer = reply.content or ""
            break
        steps = step
        messages.append(reply)
        for tc in reply.tool_calls:
            command = json.loads(tc["arguments"] or "{}").get("command", "")
            commands.append(str(command))
            messages.append(Message(
                role="tool", tool_call_id=tc["id"], content=_bash(command, workdir)
            ))

    proc = subprocess.run(
        str(scenario["verify"]), shell=True, cwd=workdir,
        capture_output=True, text=True, errors="replace", check=False,
    )
    note = "" if proc.returncode == 0 else f"verify exit {proc.returncode}"
    if steps >= max_steps:
        note = (note + "；步数熔断").strip("；")
    return ScenarioResult(sid, proc.returncode == 0, steps, note, answer, commands)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="evalkit bash-only mini 基线（037 P4）")
    parser.add_argument("--provider", default="deepseek-flash", help="模型提供方（默认 deepseek-flash）")
    parser.add_argument("--scenarios", type=Path,
                        default=Path(__file__).parent / "scenarios" / "mini_baseline.jsonl")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    args = parser.parse_args(argv)

    load_dotenv()   # 与装配层同款：API key 从项目根 .env 进环境变量
    scenarios = [
        json.loads(line)
        for line in args.scenarios.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ledger = UsageLedger()
    # with_mock_fallback=False：评测失败须大声抛，不被 mock 顶替静默污染分数
    llm = get_llm(args.provider, ledger=ledger, with_mock_fallback=False)

    results: list[ScenarioResult] = []
    for scenario in scenarios:
        with tempfile.TemporaryDirectory() as tmp:   # 场景隔离：不污染仓库
            try:
                result = run_scenario(llm, scenario, Path(tmp), args.max_steps)
            except Exception as exc:   # 单场景失败不炸整批，但要大声报告
                result = ScenarioResult(str(scenario.get("id", "?")), False, 0, f"异常：{exc}")
        results.append(result)
        mark = "pass" if result.passed else "FAIL"
        suffix = f" {result.note}" if result.note else ""
        print(f"[{mark}] {result.id}（{result.steps} 步）{suffix}", flush=True)

    passed = sum(r.passed for r in results)
    print(
        f"\n基线出分：{passed}/{len(results)} 通过；"
        f"token in={ledger.tokens_in} out={ledger.tokens_out}，cost={ledger.llm_cost:.4f}"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
