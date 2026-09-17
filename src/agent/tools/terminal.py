"""S4b 终端执行工具：run_command——coding agent 的手（执行侧）。

与文件工具的分工：files.py 管「读写项目内容」，本件管「跑项目命令」
（测试/脚本/git 查询）。威力大一个量级，所以配 L2 确认机制（S3 判据：
不可逆 × 可出本机边界 × 可被外部内容诱导）。

安全设计（S4b 草案裁定）：
  ① 全量审计：执行/拒绝都落审（registry 收口，批准与否都留痕）
  ② 白名单免确认：只读命令 + git 只读子命令 + pytest，且无 shell 元字符
     ——两条同时满足才放行；为开发循环减负（防确认疲劳/狼来了效应）
  ③ 其余一律 confirm 回调裁决（CLI input / Web 弹窗），裁决权永远在用户
  ④ 超时 60s kill、输出截断、cwd 锚定项目根
"""

from __future__ import annotations

import subprocess

from agent.paths import WORKSPACE_ROOT
from agent.tools.registry import Tool, ToolRegistry

TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 6000          # stdout+stderr 合并截断（与 read_file 同纪律）

# 免确认白名单（S4b 草案裁定）：纯读取单命令；git 只给只读子命令
# （git push / git branch -D 也是 git，不能整只放）；python 只给 -m pytest
_WHITELIST_SIMPLE = frozenset({
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "pwd",
    "which", "file", "sort", "uniq", "diff", "echo", "pytest",
})
_GIT_READONLY = frozenset({"status", "log", "diff", "show"})
# shell 元字符：出现一个即弹窗（防 cat x; rm y / git status && evil / $(…) 绕过）
_SHELL_META = frozenset(";&|><`$()\n")


def needs_confirm(command: str) -> bool:
    """保守判定：白名单命令 且 无 shell 元字符 → 免确认；其余一律确认。

    纯函数，绕过用例的测试靶子。拿不准就确认——误弹窗的代价是一次点击，
    误放行的代价不可控（保守默认与 Tool.is_readonly 同一哲学）。
    """
    if any(ch in _SHELL_META for ch in command):
        return True
    tokens = command.split()
    if not tokens:
        return True
    head = tokens[0].rsplit("/", 1)[-1]      # /usr/bin/git → git
    if "=" in head:                          # FOO=1 cmd 环境变量前缀 → 保守确认
        return True
    if head in _WHITELIST_SIMPLE:
        return False
    if head == "git" and len(tokens) > 1 and tokens[1] in _GIT_READONLY:
        return False
    if head in ("python", "python3") and tokens[1:3] == ["-m", "pytest"]:
        return False
    return True


def _run_command(command: str) -> str:
    """跑一条 shell 命令，返回 exit code + 合并输出（截断）。

    shell=True 是裁定不是疏忽：管道/重定向是日常刚需，危险面由白名单 +
    确认机制兜住，不在工具内做命令解析（那不归它管，且解析不全）。
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"错误：命令超时（>{TIMEOUT_SECONDS}s），已终止。命令：{command[:200]}"
    output = (proc.stdout or "") + (proc.stderr or "")
    truncated = ""
    if len(output) > MAX_OUTPUT_CHARS:
        truncated = f"……（输出共 {len(output)} 字，截断为前 {MAX_OUTPUT_CHARS} 字）"
        output = output[:MAX_OUTPUT_CHARS]
    return f"exit code: {proc.returncode}\n{output}{truncated}".strip()


def register_terminal_tools(registry: ToolRegistry) -> None:
    """注册终端工具（恒注册；L2 高危——needs_confirmation=True）。"""
    registry.register(
        Tool(
            name="run_command",
            description=(
                "在项目根目录执行一条 shell 命令（测试/脚本/git 查询等）。"
                "只读白名单命令直接执行；其余命令会先请求用户确认，被拒绝时"
                "换方案，不要重试同一命令。超时 60 秒，输出过长会被截断。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令，如 'pytest tests/ -q'、'git status'",
                    }
                },
                "required": ["command"],
            },
            func=_run_command,
            needs_confirmation=lambda args: needs_confirm(args["command"]),
        )
    )
