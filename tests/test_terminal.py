"""S4b 终端工具 + L2 确认流验收（2026-09-17）。

不变量：
- 白名单免确认双条件：白名单命令 且 无 shell 元字符——缺一即弹窗
  （防 cat x; rm y / git status && evil / $(…) 注入绕过）
- 拒绝 = 不执行 + 回灌「换方案」提示 + 落审（批准与否都留痕）
- 无 confirm 通道按拒绝处理（保守默认：没有眼睛就不动手）
- run_command：exit code 回传 / 超时终止 / 输出截断 / cwd 锚定项目根
"""

import json

import pytest

from agent.core.audit import AuditLog
from agent.tools.registry import ToolRegistry
from agent.tools.terminal import (
    _run_command,
    needs_confirm,
    register_terminal_tools,
)

# ---------- needs_confirm：白名单免确认 ----------

@pytest.mark.parametrize("command", [
    "ls -la",
    "cat src/agent/paths.py",
    "grep -r def src/",
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git show HEAD",
    "python -m pytest tests/ -q",
    "python3 -m pytest -q",
    "pytest -q",
    "rg 'def run_turn' src/",
    "echo hello",
    "/usr/bin/git status",            # 绝对路径 → 取 basename 判白名单
    # 参数级校验的回归侧（S4 评审 #17）：干净参数不误伤
    "find . -name *.py",
    "find tests/ -type f",
    "sort data.txt",
    "sort -n -r numbers.txt",
])
def test_whitelisted_commands_skip_confirm(command):
    assert needs_confirm(command) is False


# ---------- needs_confirm：绕过用例（草案承诺专打） ----------

@pytest.mark.parametrize("command", [
    # 元字符注入：白名单头也拦——防「白名单命令夹带第二条」
    "cat x; rm -rf y",
    "git status && evil",
    "echo $(evil)",
    "ls > /etc/passwd",
    "cat a | rm x",
    "ls `evil`",
    "ls\nrm -rf /",                   # 换行夹带第二条
    # 非白名单命令
    "rm -rf /",
    "git push origin main",
    "git commit -m x",
    "git checkout main",
    "sudo ls",
    "pip install requests",
    "python app.py",                  # python 只放 -m pytest
    "python -m http.server",
    "touch new_file.txt",
    "mkdir foo",
    # 参数级绕过（S4 评审 #17）：只读命令名 + 危险参数 ≠ 只读
    "find . -exec rm {} +",                # -exec 执行任意命令（无分号形式）
    "find . -execdir rm {} +",
    "find . -name *.pyc -delete",          # -delete 删文件
    "sort -o /tmp/victim data.txt",        # sort -o 覆盖任意文件
    "sort --output=/tmp/victim data.txt",  # 长选项等号形式
    # 环境变量前缀：语法不在白名单模型里 → 保守确认
    "FOO=1 ls",
    # 边界
    "",
])
def test_dangerous_or_complex_commands_need_confirm(command):
    assert needs_confirm(command) is True


# ---------- _run_command 六用例 ----------

def test_run_command_echo_and_exit_code():
    out = _run_command("echo hello")
    assert "exit code: 0" in out and "hello" in out

    # 非零退出码如实回传（模型可据此自纠）
    out = _run_command("ls /definitely/not/exist")
    assert "exit code: 1" in out


def test_run_command_timeout(monkeypatch):
    monkeypatch.setattr("agent.tools.terminal.TIMEOUT_SECONDS", 1)
    assert "超时" in _run_command("sleep 5")


def test_run_command_truncates_long_output(monkeypatch):
    monkeypatch.setattr("agent.tools.terminal.MAX_OUTPUT_CHARS", 10)
    out = _run_command("echo 01234567890123456789")
    assert "截断" in out
    assert "01234567890123456789" not in out   # 只剩前 10 字


def test_run_command_cwd_is_workspace_root():
    assert "my_project1" in _run_command("pwd")


# ---------- registry confirm 流 ----------

def _terminal_registry(audit: AuditLog | None = None) -> ToolRegistry:
    registry = ToolRegistry(audit=audit)
    register_terminal_tools(registry)
    return registry


def test_confirm_rejected_never_runs(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.terminal.WORKSPACE_ROOT", tmp_path)
    registry = _terminal_registry()

    out = registry.execute(
        "run_command", json.dumps({"command": "touch pwned.txt"}),
        confirm=lambda name, args: False,
    )

    assert "用户拒绝了" in out and "换方案" in out   # 回灌的是指引不是异常
    assert not (tmp_path / "pwned.txt").exists()      # 拒绝 = 根本没执行


def test_confirm_approved_executes(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.terminal.WORKSPACE_ROOT", tmp_path)
    registry = _terminal_registry()

    out = registry.execute(
        "run_command", json.dumps({"command": "touch approved.txt"}),
        confirm=lambda name, args: True,
    )

    assert "exit code: 0" in out
    assert (tmp_path / "approved.txt").exists()


def test_no_confirm_channel_defaults_to_reject(tmp_path, monkeypatch):
    # 无 confirm 通道（on_confirm=None）→ 按拒绝：没有眼睛就不动手
    monkeypatch.setattr("agent.tools.terminal.WORKSPACE_ROOT", tmp_path)
    registry = _terminal_registry()

    out = registry.execute("run_command", json.dumps({"command": "touch pwned.txt"}))

    assert "用户拒绝了" in out
    assert not (tmp_path / "pwned.txt").exists()


def test_whitelisted_command_runs_without_confirm_channel():
    # 白名单（needs=False）短路确认——无通道也能跑（读取类不弹窗）
    registry = _terminal_registry()

    out = registry.execute("run_command", json.dumps({"command": "echo free"}))

    assert "exit code: 0" in out and "free" in out


def test_rejection_is_audited(tmp_path):
    # 拒绝也落审：裁决留痕（事后可查「谁在什么时候想干什么」）
    audit = AuditLog(tmp_path)
    registry = _terminal_registry(audit)

    registry.execute(
        "run_command", json.dumps({"command": "rm -rf x"}),
        confirm=lambda name, args: False,
    )

    records = audit.read()
    assert len(records) == 1
    assert records[0]["tool"] == "run_command"
    assert "用户拒绝了" in records[0]["result"]


def test_default_tools_unaffected_by_confirm_seam():
    # needs_confirmation=False 的既有工具：不传 confirm 也照常执行（回归）
    from agent.tools.files import register_file_tools

    registry = ToolRegistry()
    register_file_tools(registry)

    out = registry.execute(
        "read_file", json.dumps({"path": "src/agent/paths.py", "offset": 17})
    )

    assert "拒绝" not in out and "WORKSPACE_ROOT" in out


# ---------- loop 第四条缝：on_confirm 透传 ----------

def test_run_turn_passes_confirm_through(tmp_path, monkeypatch):
    # 缝契约：壳层的 on_confirm 透传到 registry.execute——拒绝结果作为
    # tool 消息回灌（模型看得见原因），会话继续（拒绝不炸会话）
    from agent.core.llm import ScriptedLLM
    from agent.core.types import Message
    from agent.memory.store import Session
    from agent.orchestrator.loop import RunResult, run_turn

    monkeypatch.setattr("agent.tools.terminal.WORKSPACE_ROOT", tmp_path)
    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "run_command",
             "arguments": json.dumps({"command": "touch x.txt"})},
        ]),
        Message(role="assistant", content="好的，我换个方案"),
    ])
    registry = _terminal_registry()
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    result, reply = run_turn(
        session, "建个文件", llm=llm, registry=registry,
        on_confirm=lambda name, args: False,
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content == "好的，我换个方案"
    assert not (tmp_path / "x.txt").exists()
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert any("用户拒绝了" in (m.content or "") for m in tool_msgs)


# ---------- CLI 确认缝 ----------

def test_cli_confirm_explicit_y_approves(monkeypatch):
    from agent.cli import _cli_on_confirm

    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert _cli_on_confirm("run_command", {"command": "rm x"}) is True


@pytest.mark.parametrize("answer", ["", "n", "no", "yes", "x"])
def test_cli_confirm_anything_else_rejects(monkeypatch, answer):
    # 默认拒绝：空回车/任意非 y 输入都算拒——批准必须是显式动作
    from agent.cli import _cli_on_confirm

    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert _cli_on_confirm("run_command", {"command": "rm x"}) is False
