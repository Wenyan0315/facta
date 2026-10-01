"""S4b 终端工具 + L2 确认流验收（2026-09-17）。

不变量：
- 白名单免确认双条件：白名单命令 且 无 shell 元字符——缺一即弹窗
  （防 cat x; rm y / git status && evil / $(…) 注入绕过）
- 拒绝 = 不执行 + 回灌「换方案」提示 + 落审（批准与否都留痕）
- 无 confirm 通道按拒绝处理（保守默认：没有眼睛就不动手）
- run_command：exit code 回传 / 超时终止 / 输出截断 / cwd 锚定项目根 /
  非 UTF-8 输出降级为替换字符而不是炸掉整轮
"""

import json

import pytest

from facta.core.audit import AuditLog
from facta.tools.registry import Tool, ToolRegistry
from facta.tools.terminal import (
    WORKSPACE_ROOT,
    _confirm_rule,
    _run_command,
    needs_confirm,
    register_terminal_tools,
)

# ---------- needs_confirm：白名单免确认 ----------

@pytest.mark.parametrize("command", [
    "ls -la",
    "cat src/facta/paths.py",
    "grep -r def src/",
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git show HEAD",
    "python -m pytest tests/ -q",
    "python3 -m pytest -q",
    "echo hello",
    # 参数级校验的回归侧（S4 评审 #17）：干净参数不误伤
    "find . -name *.py",
    "find tests/ -type f",
    "sort data.txt",
    "sort -n -r numbers.txt",
])
def test_whitelisted_commands_skip_confirm(command):
    assert needs_confirm(command) is False


# ADR 071：白名单 basename + 真实路径必须在 _SAFE_SYSTEM_DIRS 内才免确认。
# 裸 `pytest` 不在 PATH 里 = which None = 保守确认；开发用 `python -m pytest`
# 走另一条免确认分支（test_whitelisted_commands_skip_confirm 已覆盖）。
# `rg` 在本机 PATH 找到的是 IDE 自带 ripgrep（@vscode/ripgrep）——不在
# 系统目录里，按规要确认；如需 rg 在 PATH 注入场景下做端到端跑进
# /usr/local/bin/rg（Homebrew）才会免确认。
@pytest.mark.parametrize("command", [
    "pytest -q",               # PATH 缺 pytest 时 which=None → 保守确认
    "rg 'def run_turn' src/",  # 真实路径在 IDE 自带 ripgrep（@vscode）目录 → 不在系统目录
])
def test_whitelisted_basename_outside_system_dirs_requires_confirm(command):
    # ADR 071：白名单只是第一步——真实路径解析后必须在 _SAFE_SYSTEM_DIRS 内
    # 才算「PATH 里那个公认只读的程序」。PATH 注入（~/.local/bin/echo）或
    # IDE 携带的非系统目录命令（Trae 自带 ripgrep）都走这条确认。
    assert needs_confirm(command) is True


# P1-3 评审修复①：程序带路径不再享受 basename 免确认——白名单的语义是
# 「PATH 里那个公认只读的程序」，./tools/echo、bin/evil 证明不了自己是它。
# 绝对路径调用只读命令多弹一次确认，不值得为省这次点击赌程序来源。
@pytest.mark.parametrize("command", [
    "/usr/bin/git status",
    "./tools/echo hello",             # 评审实测用例：仓库内同名程序冒充系统 echo
    "bin/custom-ls -la",
])
def test_pathed_programs_require_confirm(command):
    assert needs_confirm(command) is True


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
    # P1-3 评审修复②③：短选项粘连与 git 只读子命令的写参数
    "sort -o/tmp/victim data.txt",         # 粘连形态：单个 token，精确匹配抓不到
    "git diff --no-index --output=/tmp/out a b",   # 评审实测：git diff 名下的写文件
    "git log --output=/tmp/out",           # git log 同款 --output
    "find . -name x -fprint /tmp/out",     # find 的 -fprint 家族同 -o 一样写任意路径
    # 环境变量前缀：语法不在白名单模型里 → 保守确认
    "FOO=1 ls",
    # 边界
    "",
])
def test_dangerous_or_complex_commands_need_confirm(command):
    assert needs_confirm(command) is True


# ---------- needs_confirm：凭证路径（049，优先级高于白名单）----------

@pytest.mark.parametrize("command", [
    # .env 一族：cat 的免确认特权在这里失效（048 只围了写，读侧曾全放开）
    "cat .env",
    "cat .env.local",
    "head -20 .env.example",
    "cat config/.env",
    "grep API_KEY .env",
    "diff .env .env.bak",
    # home 凭证：沙箱不 deny read（会打断沙箱内 git 的 SSH 认证），全靠这层
    "cat ~/.ssh/id_rsa",
    "cat /Users/x/.ssh/id_ed25519",
    "cat ~/.ssh/config",
    "cat server.pem",
    "cat keys/app.key",
    "cat keys/id_rsa",                    # 私钥落在别处也认
    "cat ~/.aws/credentials",
    "cat ~/.netrc",
    "cat ~/.npmrc",
    "cat ~/.git-credentials",
    # 等号形式：取右值再判（否则 --file=.env 会漏）
    "tar --file=.env",
    # P1-4：token 被引号包裹时曾是盲区（锚点要求 .env 在串首或 / 之后）
    'cat ".env"',
    "cat '.env'",
    'cat "./.env"',
    'head -20 ".env.local"',
    'cat ".ssh/id_rsa"',
    "cat '~/.ssh/id_rsa'",
    'cat "server.pem"',
    "tar --file='.env'",
    'tar --file=".env"',
])
def test_credential_paths_need_confirm(command):
    assert needs_confirm(command) is True


@pytest.mark.parametrize("command", [
    # 误报口径（049 ADR）：读代码/读文档/列目录名都不该触发——白名单要防的
    # 正是确认疲劳，凭证规则不能把它毁掉
    "grep -r env src/",
    "grep -rn os.environ src/",           # .environ 不是 .env
    "cat docs/env.md",
    "cat docs/environment.md",
    "cat src/facta/env.py",
    "ls .ssh",                            # 列目录名不泄内容
    "cat keyboard.md",                    # .key 只认后缀不认词中
    "cat monkey.py",
    "ls -la",
    "cat README.md",
    "find . -name *.py",
    "python -m pytest tests/ -q",
    'grep -r "env" src/',
    'cat "docs/env.md"',
    'ls ".ssh"',
])
def test_non_credential_paths_stay_whitelisted(command):
    assert needs_confirm(command) is False


# P1-4 复现：路径被引号包裹时锚点够不着 → 退回 cat 白名单静默免确认。
# 修法（剥成对引号）的靶子：同一路径的三种写法必须同判。
@pytest.mark.parametrize("path", [".env", "./.env", "~/.ssh/id_rsa", "keys/app.key"])
def test_quoted_credential_path_parity(path):
    for probe in (path, f'"{path}"', f"'{path}'"):
        assert needs_confirm(f"cat {probe}") is True, probe


# ADR 071 PATH 注入复现：用户把 ~/.local/bin 放 PATH 最前，里面有自己写的
# fake echo——白名单 basename=echo 通过，但真实路径不在系统目录，必须确认。
def test_path_injected_basename_requires_confirm(monkeypatch, tmp_path):
    from facta.tools import terminal
    # 1) 在 tmp_path 造一个 fake echo（不是 symlink——shutil.which 直接拿到 fake）
    bin_dir = tmp_path / "evil_bin"
    bin_dir.mkdir()
    fake = bin_dir / "echo"
    fake.write_text("#!/bin/sh\necho evil\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{__import__('os').pathsep}{__import__('os').environ.get('PATH', '')}")
    # 2) shutil.which 缓存清理（caching 行为）
    import shutil as _shutil
    _shutil.which.cache_clear() if hasattr(_shutil.which, "cache_clear") else None
    # 3) 解析：fake echo 在 ~/.local/bin 风格的用户目录 → 走确认
    assert terminal._basename_resolves_to_system("echo") is False
    assert needs_confirm("echo hello") is True


# ---------- 确认规则归因（050）：撞了哪道围栏是可记录的事实 ----------

@pytest.mark.parametrize("command,rule", [
    ("cat .env", "credential-path"),
    ('cat ".env"', "credential-path"),
    ("cat '.env'", "credential-path"),
    ("cat ~/.ssh/id_rsa", "credential-path"),
    ("echo a; rm -rf b", "shell-meta"),
    ("FOO=1 pytest", "env-prefix"),
    ("sort --output=x in", "dangerous-arg"),
    ("lsof -i :8000", "not-whitelisted"),
    ("touch pwned.txt", "not-whitelisted"),
    ("", "empty"),
    ("   ", "empty"),
])
def test_confirm_rule_names(command, rule):
    assert _confirm_rule(command) == rule


@pytest.mark.parametrize("command", [
    "ls -la", "cat README.md", "git status", "python -m pytest -q", "grep -r env src/",
])
def test_confirm_rule_is_none_when_whitelisted(command):
    assert _confirm_rule(command) is None


def test_confirm_rule_never_drifts_from_needs_confirm():
    # needs_confirm 是薄封装：真值必须逐条一致，否则 60+ 条既有断言与归因各说各话
    commands = [
        "ls", "cat .env", 'cat ".env"', "cat README.md", "git status", "git push", "rm -rf x",
        "python -m pytest", "python x.py", "echo a; rm b", "", "sort -o out in",
        "find . -delete", "FOO=1 ls", "cat ~/.ssh/id_rsa", "cat keyboard.md",
    ]
    for cmd in commands:
        assert needs_confirm(cmd) is (_confirm_rule(cmd) is not None), cmd


# ---------- _run_command 六用例 ----------

def test_run_command_echo_and_exit_code():
    out = _run_command("echo hello")
    assert "exit code: 0" in out and "hello" in out

    # 非零退出码如实回传（模型可据此自纠）。用 shell 内建 exit 3 定码——
    # ls 不存在路径的退出码跨平台不同（BSD=1 / GNU=2），不可断言具体值
    # （CI 15 连红的根因之一：本地 macOS 全绿掩盖了 Linux runner 的差异）
    out = _run_command("exit 3")
    assert "exit code: 3" in out


def test_run_command_timeout(monkeypatch):
    monkeypatch.setattr("facta.tools.terminal.TIMEOUT_SECONDS", 1)
    assert "超时" in _run_command("sleep 5")


def test_run_command_truncates_long_output(monkeypatch):
    monkeypatch.setattr("facta.tools.terminal.MAX_OUTPUT_CHARS", 10)
    out = _run_command("echo 01234567890123456789")
    assert "截断" in out
    assert "01234567890123456789" not in out   # 只剩前 10 字


def test_run_command_cwd_is_workspace_root():
    # cwd 锚定的行为验证 = pwd 输出就是 WORKSPACE_ROOT 本身。
    # 不断言目录名（本地 my_project1 / CI checkout 到 facta——
    # 目录名假设是 CI 15 连红的另一个根因）
    assert str(WORKSPACE_ROOT) in _run_command("pwd")


def test_run_command_survives_non_utf8_output():
    """命令吐非 UTF-8 字节不能炸整轮——grep 二进制向量库是日常操作。

    text=True 默认严格解码，UnicodeDecodeError 会一路冒到 loop 的工具兜底，
    模型只看见「错误：并行执行失败」，输出全丢（比截断糟得多）。
    """
    out = _run_command("printf 'ok\\xff\\xfe tail\\n'")
    assert "exit code: 0" in out and "ok" in out and "tail" in out


# ---------- registry confirm 流 ----------

def _terminal_registry(audit: AuditLog | None = None, root=WORKSPACE_ROOT) -> ToolRegistry:
    from pathlib import Path

    from facta.tools.context import ToolContext

    registry = ToolRegistry(audit=audit)
    register_terminal_tools(registry, ToolContext(notes_dir=Path("data/notes"), workspace_root=root))
    return registry


def test_confirm_rejected_never_runs(tmp_path):
    registry = _terminal_registry(root=tmp_path)

    out = registry.execute(
        "run_command", json.dumps({"command": "touch pwned.txt"}),
        confirm=lambda name, args: False,
    )

    assert "用户拒绝了" in out and "换方案" in out   # 回灌的是指引不是异常
    assert not (tmp_path / "pwned.txt").exists()      # 拒绝 = 根本没执行


def test_confirm_approved_executes(tmp_path):
    registry = _terminal_registry(root=tmp_path)

    out = registry.execute(
        "run_command", json.dumps({"command": "touch approved.txt"}),
        confirm=lambda name, args: True,
    )

    assert "exit code: 0" in out
    assert (tmp_path / "approved.txt").exists()


def test_confirm_approved_leaves_trace_for_model(tmp_path):
    # 054：批准路径原先对模型静默——回灌的只有工具原始输出，模型只能从
    # 「结果回来了」反推「大概没弹框」。实测（audit 2026-09-27T14:16:00
    # 那条 sleep 30，guard 明记 not-whitelisted）它幻觉出「被当只读放行」+
    # 「sleep 居然在只读白名单里」，还据此提议去摘一个不存在的白名单项。
    registry = _terminal_registry(root=tmp_path)

    out = registry.execute(
        "run_command", json.dumps({"command": "sleep 0"}),
        confirm=lambda name, args: True,
    )

    assert "exit code: 0" in out        # 原始输出不被痕迹挤掉
    assert "经用户确认批准" in out        # 裁决结果回灌：模型不必再猜
    assert "not-whitelisted" in out     # 规则名复用 050 的 guard，不另立真值源


def test_whitelisted_command_leaves_no_confirm_trace():
    # 对称的另一半：免确认路径**不带**痕迹——否则模型会以为白名单命令也
    # 弹过窗，把「静默直跑」这个既有认知也搞错。
    registry = _terminal_registry()

    out = registry.execute("run_command", json.dumps({"command": "echo free"}))

    assert "exit code: 0" in out and "经用户确认批准" not in out


def test_confirm_trace_appended_after_empty_output_marker():
    # 顺序回归靶子：痕迹必须拼在 037 P2 的空输出显式化**之后**——
    # 先拼会让 result 非空，「（无输出）」永不触发，两条信息一起丢。
    registry = ToolRegistry()
    registry.register(Tool(
        name="silent_danger", description="", parameters={},
        func=lambda: "", needs_confirmation=True,
    ))

    out = registry.execute("silent_danger", "{}", confirm=lambda n, a: True)

    assert "（无输出）" in out
    assert "经用户确认批准" in out


def test_no_confirm_channel_defaults_to_reject(tmp_path):
    # 无 confirm 通道（on_confirm=None）→ 按拒绝：没有眼睛就不动手
    registry = _terminal_registry(root=tmp_path)

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


def test_guard_rule_is_audited_on_both_paths(tmp_path):
    # 050 归因：批准与拒绝都要带 guard——i4 那次是批准后执行的，
    # 只记拒绝路径就正好漏掉要归因的那一条。白名单命令不带 guard 键。
    audit = AuditLog(tmp_path)
    registry = _terminal_registry(audit, root=tmp_path)

    registry.execute(
        "run_command", json.dumps({"command": "cat .env"}),
        confirm=lambda name, args: True,
    )
    registry.execute(
        "run_command", json.dumps({"command": "rm -rf x"}),
        confirm=lambda name, args: False,
    )
    registry.execute("run_command", json.dumps({"command": "echo free"}))

    records = audit.read()
    assert [r.get("guard") for r in records] == [
        "credential-path", "not-whitelisted", None,
    ]


def test_default_tools_unaffected_by_confirm_seam():
    # needs_confirmation=False 的既有工具：不传 confirm 也照常执行（回归）
    from pathlib import Path

    from facta.tools.context import ToolContext
    from facta.tools.files import register_file_tools

    registry = ToolRegistry()
    register_file_tools(registry, ToolContext(notes_dir=Path("data/notes")))

    out = registry.execute(
        "read_file", json.dumps({"path": "src/facta/paths.py", "offset": 17})
    )

    assert "拒绝" not in out and "WORKSPACE_ROOT" in out


# ---------- loop 第四条缝：on_confirm 透传 ----------

def test_run_turn_passes_confirm_through(tmp_path, monkeypatch):
    # 缝契约：壳层的 on_confirm 透传到 registry.execute——拒绝结果作为
    # tool 消息回灌（模型看得见原因），会话继续（拒绝不炸会话）
    from facta.core.llm import ScriptedLLM
    from facta.core.types import Message
    from facta.memory.store import Session
    from facta.orchestrator.agent import Agent
    from facta.orchestrator.loop import RunResult, run_turn

    llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "run_command",
             "arguments": json.dumps({"command": "touch x.txt"})},
        ]),
        Message(role="assistant", content="好的，我换个方案"),
    ])
    registry = _terminal_registry(root=tmp_path)
    agent = Agent(name="test", system_prompt="sys", registry=registry)
    session = Session()
    session.messages.append(Message(role="system", content="sys"))

    result, reply = run_turn(
        session, "建个文件", llm=llm, agent=agent,
        on_confirm=lambda name, args: False,
    )

    assert result is RunResult.COMPLETED
    assert reply is not None and reply.content == "好的，我换个方案"
    assert not (tmp_path / "x.txt").exists()
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert any("用户拒绝了" in (m.content or "") for m in tool_msgs)


# ---------- CLI 确认缝 ----------

def test_cli_confirm_explicit_y_approves(monkeypatch):
    from facta.cli import _cli_on_confirm

    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert _cli_on_confirm("run_command", {"command": "rm x"}) is True


@pytest.mark.parametrize("answer", ["", "n", "no", "yes", "x"])
def test_cli_confirm_anything_else_rejects(monkeypatch, answer):
    # 默认拒绝：空回车/任意非 y 输入都算拒——批准必须是显式动作
    from facta.cli import _cli_on_confirm

    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert _cli_on_confirm("run_command", {"command": "rm x"}) is False
