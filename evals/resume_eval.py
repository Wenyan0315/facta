"""evalkit kill-resume 场景集（038 反方意见 1 拍板的验收载体，P0-3）。

验收原文：「kill 进程 → 从 checkpoint 恢复 → 任务完成且副作用不重复」。
离线单测（tests/test_checkpoint.py）钉语义，本文件钉**真崩溃**：SIGKILL 不给
任何清理机会（finally / atexit / 缓冲区 flush 全都不跑），只有盘上的东西算数。

三段式，每段独立进程：
    父进程    建场景临时目录 → 起子进程跑「kill 段」→ 起子进程跑「resume 段」→ verify
    kill 段   真模型 + 真 run_command 工具 + CheckpointWriter 挂在 on_event 上，
              数到第 N 次 tool_result 落账后 `os.kill(os.getpid(), SIGKILL)` 自杀
    resume 段 load 底片 → heal 补齐悬挂轮次 → run_turn(user_text=None) 续跑到底

kill 时机不靠模型配合（那不可复现），由 runner 在事件缝上数数——所以场景只需
声明 kill_after，任务本身是普通的多步 shell 活。

「副作用不重复」的可观测载体是累积量（追加行数 / 计数器数值）：重放一次已完成
的调用，数字就会多出来，verify 立刻失败。write_file 是覆写语义，重复执行看不
出来，所以场景一律用 shell 累积。

一键出分（仓库根，要真模型与 .env 里的 API key）：
    .venv/bin/python -m evals.resume_eval [--provider deepseek-flash]

场景文件（JSONL，每行一条）：
    {"id", "task", "verify", "kill_after", "setup"（可选）}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from facta.core.llm import get_llm
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.checkpoint import CheckpointWriter, heal, ledger_path, read_ledger
from facta.orchestrator.loop import run_turn
from facta.tools.context import ToolContext
from facta.tools.registry import ToolRegistry
from facta.tools.terminal import register_terminal_tools

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = Path(__file__).parent / "scenarios" / "kill_resume.jsonl"

# 会话 id 必须匹配 SessionStore 的白名单（`^\d{8}-\d{6}(-\d+)?$`）；
# 固定值即可——每个场景有自己的临时目录，不会撞车
SID = "20260101-000000"

_SYSTEM = (
    "你是评测用的执行 agent，只有 run_command 一个工具（shell 命令，工作目录就是任务目录）。"
    "严格按用户要求的步数分次调用工具，不要用 && 或分号把多步串成一条命令。"
    "任务完成后直接用文字回答，不要再调用工具。"
)

_CHILD_TIMEOUT = 300      # 单段（kill / resume）超时秒数：真模型多轮，给足但不无限

# 父进程靠这行 stdout 判断「resume 段真的补过残局」，见 _run_child 的 resume 分支
_HEALED_RE = re.compile(r"heal 补齐 (\d+) 条")


@dataclass
class ScenarioResult:
    id: str
    passed: bool
    note: str = ""


@dataclass
class ChildState:
    """父→子的全部输入（落成 JSON 文件传，避免超长命令行与引号地狱）。"""

    phase: str            # kill | resume
    provider: str
    task: str
    workdir: str
    sessions: str
    checkpoints: str
    kill_after: int

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ChildState:
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def _run_child(state: ChildState) -> int:
    """子进程入口：跑一段（kill 段中途自杀，resume 段跑到底）。"""
    load_dotenv()   # 与装配层同款：API key 从项目根 .env 进环境变量
    workdir = Path(state.workdir)
    store = SessionStore(Path(state.sessions))
    ledger_file = ledger_path(SID, directory=Path(state.checkpoints))

    # 只挂 run_command：场景是 shell 活，多挂工具只会给模型分心的机会
    registry = ToolRegistry()
    register_terminal_tools(registry, ToolContext(
        notes_dir=workdir / "notes", workspace_root=workdir,
    ))
    agent = Agent(name="resume-eval", system_prompt=_SYSTEM, registry=registry)
    # with_mock_fallback=False：评测失败须大声抛，不被 mock 顶替静默污染结论
    llm = get_llm(state.provider, ledger=UsageLedger(), with_mock_fallback=False)

    if state.phase == "kill":
        session = Session()
        session.messages.append(Message(role="system", content=_SYSTEM))
        store.save(SID, session)
        user_text: str | None = state.task
    else:
        session = store.load(SID)
        healed = heal(session, read_ledger(ledger_file), registry)
        print(f"[resume] heal 补齐 {healed} 条悬挂工具调用", flush=True)
        user_text = None            # 续跑：不追加新提问，从底片现场往前走

    writer = CheckpointWriter(ledger_file, lambda: store.save(SID, session))
    done = 0

    def on_event(type_: str, data: dict) -> None:
        nonlocal done
        writer.on_event(type_, data)          # 先落盘：盘上没有的事实等于没发生
        if state.phase == "kill" and type_ == "tool_result":
            done += 1
            if done >= state.kill_after:
                # 第 N 次结果已进账本、但底片还没存到它——正是 038 要治的残局
                print(f"[kill] 第 {done} 次工具结果落账，SIGKILL 自杀", flush=True)
                os.kill(os.getpid(), signal.SIGKILL)

    run_turn(
        session, user_text, agent=agent, llm=llm,
        on_event=on_event,
        on_confirm=lambda name, args: True,   # 评测无人值守：高危命令一律批准
    )
    store.save(SID, session)
    return 0


def _spawn(state: ChildState, tmp: Path) -> subprocess.CompletedProcess[str]:
    """起一个子进程跑一段。cwd=仓库根：`-m evals.resume_eval` 与 `import facta` 都靠它。"""
    state_path = tmp / f"state-{state.phase}.json"
    state.dump(state_path)
    try:
        return subprocess.run(
            [sys.executable, "-m", "evals.resume_eval", "--child", str(state_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=_CHILD_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 1, "", f"超时（>{_CHILD_TIMEOUT}s）")


def _shell(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, shell=True, cwd=cwd, capture_output=True, text=True, check=False
    )


def run_scenario(scenario: dict, provider: str) -> ScenarioResult:
    """一个场景 = 临时目录里跑完「setup → kill → resume → verify」四段。"""
    sid = str(scenario.get("id", "?"))
    with tempfile.TemporaryDirectory(prefix="resume-eval-") as raw:
        tmp = Path(raw)
        workdir = tmp / "work"
        workdir.mkdir()

        setup = scenario.get("setup")
        if setup:
            proc = _shell(setup, workdir)
            if proc.returncode != 0:
                return ScenarioResult(sid, False, f"setup 失败：{proc.stderr.strip()[:200]}")

        common = dict(
            provider=provider, task=str(scenario["task"]), workdir=str(workdir),
            sessions=str(tmp / "sessions"), checkpoints=str(tmp / "checkpoints"),
            kill_after=int(scenario.get("kill_after", 1)),
        )
        killed = _spawn(ChildState(phase="kill", **common), tmp)
        # SIGKILL 的 returncode 是 -9。不是 -9 说明没崩在预期位置，场景本身失效
        if killed.returncode != -signal.SIGKILL:
            tail = (killed.stderr or killed.stdout or "").strip()[-300:]
            return ScenarioResult(sid, False, f"kill 段没被 SIGKILL（rc={killed.returncode}）{tail}")

        resumed = _spawn(ChildState(phase="resume", **common), tmp)
        if resumed.returncode != 0:
            tail = (resumed.stderr or resumed.stdout or "").strip()[-300:]
            return ScenarioResult(sid, False, f"resume 段失败（rc={resumed.returncode}）{tail}")
        # 必须真的补过残局，否则这个场景压根没走到恢复路径（kill 时机漂了）——
        # 悄悄 pass 等于验收作废，宁可大声失败
        healed = _HEALED_RE.search(resumed.stdout or "")
        if healed is None or healed.group(1) == "0":
            return ScenarioResult(sid, False, "resume 段没有悬挂轮次可补（heal=0）")

        proc = _shell(str(scenario["verify"]), workdir)
        if proc.returncode != 0:
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()[:200]
            return ScenarioResult(sid, False, f"verify exit {proc.returncode}：{out}")
        return ScenarioResult(sid, True, f"heal 补齐 {healed.group(1)} 条")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="evalkit kill-resume 场景集（038 / P0-3）")
    parser.add_argument("--provider", default="deepseek-flash", help="模型提供方（默认 deepseek-flash）")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)   # 内部：子进程入口
    args = parser.parse_args(argv)

    if args.child is not None:
        return _run_child(ChildState.load(args.child))

    scenarios = [
        json.loads(line)
        for line in args.scenarios.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        try:
            result = run_scenario(scenario, args.provider)
        except Exception as exc:   # 单场景失败不炸整批，但要大声报告
            result = ScenarioResult(str(scenario.get("id", "?")), False, f"异常：{exc}")
        results.append(result)
        mark = "pass" if result.passed else "FAIL"
        suffix = f" —— {result.note}" if result.note else ""
        print(f"[{mark}] {result.id}{suffix}", flush=True)

    passed = sum(r.passed for r in results)
    print(f"\nkill-resume 出分：{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
