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
  ⑤ 进程级沙箱（048）：macOS seatbelt 写围栏——批准语义从「全机权限」
     变为「围栏内跑」（写限项目根+TMPDIR；.env*、.git/hooks、.git/config、
     记忆/审计/venv 等黑名单不可写）。确认弹窗不再是唯一防线；无后端
     环境诚实降级原样跑 + 审计打标 off（FACTA_SANDBOX=off 可强制关）
  ⑥ 凭证双层围栏（049）：沙箱 deny read 围死 .env 一族（进程级硬挡，
     cat/open 都拿不到内容）；home 凭证（~/.ssh/id_rsa、.pem、.aws 等）
     不进沙箱 deny（会打断沙箱内 git 的 SSH 认证），改由 needs_confirm
     的凭证模式一律弹窗——白名单免确认对凭证失效
  ⑦ 围栏归因（050）：判定抽成 _confirm_rule → 规则名（shell-meta /
     env-prefix / dangerous-arg / credential-path / not-whitelisted /
     empty），经 needs_confirmation 进审计的 extra["guard"]。「这次弹窗
     撞的是哪道围栏」从此是记录里的事实，不再靠模型自述
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from facta.paths import WORKSPACE_ROOT
from facta.tools.context import ToolContext
from facta.tools.registry import Tool, ToolRegistry
from facta.tools.sandbox import wrap_command

TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 6000          # stdout+stderr 合并截断（与 read_file 同纪律）

# 免确认白名单（S4b 草案裁定）：纯读取单命令；git 只给只读子命令
# （git push / git branch -D 也是 git，不能整只放）；python 只给 -m pytest。
# ruff/mypy（issue #15 ①）：CI 同口径验证要求 bot 能跑 lint 和类型检查；
# ruff 的写形态（format/--fix）在 _DANGEROUS_ARGS 里拦，mypy 只写缓存。
_WHITELIST_SIMPLE = frozenset({
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "pwd",
    "which", "file", "sort", "uniq", "diff", "echo", "pytest",
    "ruff", "mypy",
})
_GIT_READONLY = frozenset({"status", "log", "diff", "show"})
# shell 元字符：出现一个即弹窗（防 cat x; rm y / git status && evil / $(…) 绕过）
_SHELL_META = frozenset(";&|><`$()\n")

# ADR 071：白名单 basename 的真实路径必须落在公认系统目录里才算「PATH 里那个
# 公认只读的程序」。挡 ~/.local/bin/echo 之类的 PATH 注入（同 basename、不同
# 真实路径）。这是「白名单是系统程序的指针」语义的最后一道闸门——前道闸门是
# P1-3 已修的「带路径程序一律确认」（./tools/echo 已挡），本道挡 PATH 注入。
# 不挡真实写入：seatbelt 沙箱（048）才是写操作的硬隔离，本层只过滤 basename 一致
# 但 PATH 里被劫持的程序；触发 seatbelt 缺位时本层是单层防线。
# `.venv/bin` 同样放行——开发者对自己虚拟环境有控制力，且这是 `python -m pytest`
# 之外 pytest 命令的常驻路径（测试用例的典型调用）。
_SAFE_SYSTEM_DIRS = frozenset({
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    "/usr/local/bin", "/opt/homebrew/bin",      # Homebrew
    str(WORKSPACE_ROOT / ".venv" / "bin"),       # 开发虚拟环境
})

# 参数级危险参数（S4 评审 #17）：只读命令名 + 危险参数仍可执行任意代码
# 或覆盖文件。只收有真实危险面的参数——grep -x 是整行匹配（纯只读），
# 评审提及但不成立，误报确认正是白名单要防的疲劳源。
# P1-3 评审修复补全：git 只读子命令也带写文件参数（git diff --output=x）；
# find 的 -fprint 家族同 -o 一样把输出写进任意路径。
_DANGEROUS_ARGS: dict[str, frozenset[str]] = {
    "find": frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete",
                       "-fprint", "-fprint0", "-fprintf", "-fls"}),
    "sort": frozenset({"-o", "--output"}),  # -o/--output 可覆盖任意文件
    "git": frozenset({"--output"}),          # diff/log/show 的 --output= 写文件
    # ruff（issue #15 ①）：check 是只读的，但 format / --fix 家族会改写源文件——
    # bot 没有人可以弹确认，这些形态必须落回确认闸门（bot 场景=拒绝）
    "ruff": frozenset({"format", "--fix", "--fix-only", "--add-noqa",
                       "--unsafe-fixes", "-w", "--watch"}),
}
# 短选项粘连形态（P1-3）：`sort -o/tmp/x` 是单个 token，精确匹配抓不到——
# 按前缀补刀。误报方向安全（sort 没有其他 -o 开头的选项，多弹一次确认可接受）。
_DANGEROUS_PREFIXES: dict[str, frozenset[str]] = {
    "sort": frozenset({"-o"}),
}

# 凭证路径模式（049）：命中即要确认，优先级高于白名单——`cat` 免确认对
# `.env` 失效。只收「读了就等于泄漏」的几类；沙箱那边 deny read 只围 .env
# 一族（home 凭证 deny read 会打断沙箱内 git 的 SSH 认证），这层补上缺口。
# 误报口径（tests/test_terminal.py 钉住）：grep -r env src/、cat docs/env.md、
# ls .ssh（列目录名不泄内容）都不触发。
_CREDENTIAL_RE = re.compile(
    r"(?:^|/)\.env"                       # .env / .env.local / .env.example
    r"|\.ssh/"                            # ~/.ssh/xxx（裸目录名 .ssh 不算）
    r"|(?:^|/)id_(?:rsa|ed25519)$"        # 落在别处的私钥
    r"|\.(?:pem|key)$"
    r"|\.aws/credentials"
    r"|(?:^|/)\.(?:netrc|npmrc|git-credentials)$"
)


def _has_credential_path(command: str) -> bool:
    """命令任一 token 是否指向凭证路径（049）。

    token 级检查 + 等号形式取右值（--file=.env → .env），避免整串匹配
    把无关文本误判成路径。
    """
    return any(
        _CREDENTIAL_RE.search(token.rsplit("=", 1)[-1]) for token in command.split()
    )


def _confirm_rule(command: str) -> str | None:
    """命中哪道确认规则；免确认返回 None（050 归因）。

    与 needs_confirm 是同一份判定的两种读法——needs_confirm 是它的薄封装，
    真值不可能漂移。规则名进审计的 extra["guard"]，回答「这次弹窗到底撞了
    哪道围栏」：049 执行校正 ⑥ 的缺口是 i4 那 1 次 run_command 确认，记录里
    查不出是凭证围栏还是非白名单命令，只能靠模型自述。
    """
    if any(ch in _SHELL_META for ch in command):
        return "shell-meta"
    tokens = command.split()
    if not tokens:
        return "empty"
    # P1-3 评审修复①：程序带路径（./tools/echo、bin/evil、/usr/local/bin/evil）
    # 不再享受 basename 免确认——白名单的语义是「PATH 里那个公认只读的程序」，
    # 仓库内同名程序证明不了自己是它（评审实测 ./tools/echo 因 basename=echo
    # 被放行）。裸命令名才走 PATH 解析。误报方向安全：绝对路径调用只读命令
    # 多弹一次确认，不值得为省这次点击赌程序来源。
    if "/" in tokens[0]:
        return "not-whitelisted"
    head = tokens[0].rsplit("/", 1)[-1]      # /usr/bin/git → git
    if "=" in head:                          # FOO=1 cmd 环境变量前缀 → 保守确认
        return "env-prefix"
    # 参数级拦截：白名单命令带危险参数 → 确认（S4 评审 #17）
    # split("=") 兼容长选项等号形式（sort --output=file）；
    # 前缀刀（P1-3 修复②）抓短选项粘连 `sort -o/tmp/x`（单个 token）
    dangerous = _DANGEROUS_ARGS.get(head)
    if dangerous is not None and any(
        arg.split("=", 1)[0] in dangerous
        or any(arg.startswith(p) for p in _DANGEROUS_PREFIXES.get(head, frozenset()))
        for arg in tokens[1:]
    ):
        return "dangerous-arg"
    # 凭证路径拦截（049）：优先级高于白名单——cat/head 等免确认命令读 .env
    # 或私钥同样要人裁决（048 只围了写，读侧全放开，注入载荷一条 cat 就穿）
    if _has_credential_path(command):
        return "credential-path"
    if head in _WHITELIST_SIMPLE:
        # ADR 071：白名单 basename 还要查真实路径——挡 PATH 注入的同名程序
        # （~/.local/bin/echo 之类）。解析后不在 _SAFE_SYSTEM_DIRS 内 = 确认。
        # 注意：这是 P1-3 评审修复①（带路径一律确认）的延伸——前者挡的是
        # 「用户在仓库里写了 ./tools/echo 冒充系统 echo」，本层挡的是「用户在
        # PATH 里放了 ~/.local/bin/echo 抢在 /bin/echo 前面」。两层补完 PATH
        # 注入的两个入口；未解决的是「真改 /bin/echo 本身」——seatbelt 才是
        # 那种情形的硬隔离（048）。
        if not _basename_resolves_to_system(head):
            return "not-whitelisted"
        return None
    if head == "git" and len(tokens) > 1 and tokens[1] in _GIT_READONLY:
        if not _basename_resolves_to_system("git"):
            return "not-whitelisted"
        return None
    if head in ("python", "python3") and tokens[1:3] == ["-m", "pytest"]:
        return None
    return "not-whitelisted"


def _basename_resolves_to_system(head: str) -> bool:
    """白名单 basename 的真实路径解析后是否落在公认系统目录里（ADR 071）。

    `shutil.which` 按当前 PATH 顺序查找；realpath 解析 symlink 拿到最终落点。
    shim/包装库（mise/asdf 的 ~/.local/share/.../bin/ls）→ realpath 跳到系统目录，
    仍算安全；纯前端用户脚本（~/.local/bin/echo 不是 symlink）→ 落在用户目录，
    直接挡。PATH 里压根找不到 → False（保守确认，宁多弹不错）。
    """
    real = shutil.which(head)
    if real is None:
        return False
    try:
        resolved = str(Path(real).resolve())
    except OSError:
        return False
    return any(resolved.startswith(d + "/") for d in _SAFE_SYSTEM_DIRS)


def needs_confirm(command: str) -> bool:
    """保守判定：白名单命令 且 无 shell 元字符 且 无危险参数 且 不含凭证路径
    → 免确认；其余一律确认。

    纯函数，绕过用例的测试靶子。拿不准就确认——误弹窗的代价是一次点击，
    误放行的代价不可控（保守默认与 Tool.is_readonly 同一哲学）。

    参数级校验（S4 评审 #17）：find -exec / sort -o 等只读命令名+
    危险参数仍可执行任意代码或覆盖文件，必须拦截。

    凭证路径校验（049）：.env / 私钥一类命中即确认，优先级高于白名单。
    """
    return _confirm_rule(command) is not None


def _run_command(command: str, *, root: Path = WORKSPACE_ROOT) -> str:
    """跑一条 shell 命令，返回 exit code + 合并输出（截断）。

    root（S6a 注入化）：cwd 锚点——主 agent = 主工作区；子 agent = worktree。
    shell=True 是裁定不是疏忽：管道/重定向是日常刚需，危险面由白名单 +
    确认机制兜住，不在工具内做命令解析（那不归它管，且解析不全）。

    048 沙箱：有后端时改为 argv 形式（sandbox-exec -p profile /bin/sh -c
    command，shell=False——shell 语义由围栏内的 sh 承担）；无后端降级
    走原 shell=True 路径，行为与从前完全一致。
    """
    argv, _backend = wrap_command(command, root=root)
    try:
        proc = subprocess.run(
            argv if argv is not None else command,
            shell=argv is None,
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",   # 命令吐非 UTF-8 字节（grep 二进制库等）不能让整轮炸
            timeout=TIMEOUT_SECONDS,
            check=False,   # 非零退出码是回传给模型的信息，不是异常
        )
    except subprocess.TimeoutExpired:
        return f"错误：命令超时（>{TIMEOUT_SECONDS}s），已终止。命令：{command[:200]}"
    output = (proc.stdout or "") + (proc.stderr or "")
    truncated = ""
    if len(output) > MAX_OUTPUT_CHARS:
        truncated = f"……（输出共 {len(output)} 字，截断为前 {MAX_OUTPUT_CHARS} 字）"
        output = output[:MAX_OUTPUT_CHARS]
    return f"exit code: {proc.returncode}\n{output}{truncated}".strip()


def register_terminal_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """注册终端工具（恒注册；L2 高危——needs_confirmation=True）。

    S6a 注入化：cwd 锚点从 ctx 取（与 files.py 同款闭包模式）。
    """
    root = ctx.workspace_root
    registry.register(
        Tool(
            name="run_command",
            description=(
                "在项目根目录执行一条 shell 命令（测试/脚本/git 查询等）。"
                "只读白名单命令直接执行；其余命令会先请求用户确认，被拒绝时"
                "换方案，不要重试同一命令。超时 60 秒，输出过长会被截断。"
                "命令在写围栏内运行：项目目录之外、.env、.git/hooks 等位置"
                "不可写，报 Operation not permitted 即围栏拦截，换项目内路径；"
                "git init/clone 需写 .git/config 也不在围栏内跑，请用户代为执行。"
                ".env 同样不可读（Operation not permitted），私钥/证书一类凭证"
                "路径会触发用户确认；需要密钥或配置值时不要自己去读，"
                "直接请用户提供或代为执行。"
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
            func=lambda command: _run_command(command, root=root),
            # 050：吐规则名而非 bool——registry 据此写 audit 的 extra["guard"]。
            # 非空 str 为真、None 为假，真值语义与 needs_confirm 完全一致。
            needs_confirmation=lambda args: _confirm_rule(args["command"]),
            sandboxed=True,   # 048：审计条目带 sandbox=seatbelt/off 标记
        )
    )
