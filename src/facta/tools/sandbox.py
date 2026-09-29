"""run_command 进程级沙箱（048）：确认弹窗不再是唯一防线。

形态（048 裁定·甲案）：平台派发抽象层——detect_backend() 探测本机可用
后端，wrap_command() 把 shell 命令包进沙箱 argv；无后端环境诚实降级
（原样跑 + 审计打标 off），不假装有围栏。macOS 用系统自带 sandbox-exec
（seatbelt）；Linux/Windows 后端挂触发信号，检出也不假装支持。

seatbelt 实战语义（本机探测地面真值，048 ADR）：
- (deny default) 一刀切会掐死 shell 启动（getcwd/dyld 被拒）——实战
  profile 必须「放读放基础操作，只限写」。
- 同一 operation 多条规则匹配时后定义者胜出——黑名单 deny 必须放在
  写白名单之后（测试钉住顺序）。
- 按真实路径匹配：/tmp 是 /private/tmp 的 symlink，subpath 必须给
  resolve 后的路径（调用方责任）。
- fork/exec 全继承：curl x | sh 整链都在围栏内，子进程逃不出去。

049 补：读侧默认全放，只有 .env 一族 deny file-read*——凭证是唯一「读了
就等于泄漏」的资源，其余读全放以免打断正常流程（分层：home 凭证走应用层
确认，见 terminal.needs_confirm）。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from agent.paths import MEMORY_WRITE_FENCE

ENV_SWITCH = "CORTEX_SANDBOX"   # =off 强制关闭（其余值/缺省 = auto）

# 与 files.py 黑名单交叉同源的目录项（root 相对）——tests/test_sandbox.py
# 断言 files.py 的每一项在这里都有对应 deny，漂移即红。
_BLACKLIST_DIRS = (
    "data/memory", "data/audit", "data/vector_db",
    "servers/sandbox", ".venv", "data/worktrees",
)


def detect_backend() -> str | None:
    """探测可用沙箱后端；无 → None（诚实降级）。

    不做模块级缓存：which 是毫秒级，run_command 频率低；缓存会让
    CORTEX_SANDBOX=off 在同进程内不生效。
    """
    if os.environ.get(ENV_SWITCH) == "off":
        return None
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    return None


def build_seatbelt_profile(root: Path) -> str:
    """生成 seatbelt profile 文本。root 必须已 resolve（symlink 锚点会让
    围栏整圈失效——调用方责任，terminal 的 ctx.workspace_root 已 resolve）。

    语义：放基础操作 + 全机读（除 .env 一族，049 补的读黑名单）+ 写白名单
    四处（root/TMPDIR//private/tmp//private/var/folders），黑名单后置 deny。
    网络放行是 048 裁定（网络维度另案，本轮只做写隔离）。
    """
    r = str(root)
    rx_root = re.escape(r)
    tmpdir = str(Path(tempfile.gettempdir()).resolve())
    rules = [
        "(version 1)",
        "(deny default)",
        # shell/python 能启动的最小集 + 网络放行（048：网络另案）
        "(allow process-exec process-fork signal mach-lookup sysctl-read)",
        "(allow ipc-posix-sem ipc-posix-shm network*)",
        "(allow file-read*)",
        # ── 写白名单（先 allow）──
        # /dev/null：git 等工具启动即打开它，(deny default) 连设备写都挡
        '(allow file-write* (literal "/dev/null"))',
        f'(allow file-write* (subpath "{r}"))',
        f'(allow file-write* (subpath "{tmpdir}"))',
        '(allow file-write* (subpath "/private/tmp"))',
        '(allow file-write* (subpath "/private/var/folders"))',
        # ── 写黑名单（后 deny 胜出；与 files.py 测试交叉同源）──
        # .env：根级 .env* 前缀 + 任意深度 .env 文件/目录（同 files.py 语义）
        f'(deny file-write* (regex "^{rx_root}/\\.env"))',
        '(deny file-write* (regex "/\\.env$"))',
        '(deny file-write* (regex "/\\.env/"))',
        # .git 丙案（048）：可写保日常 commit 闭环，只围死两个毒化入口——
        # hooks（跨边界延时执行：沙箱内写毒 hook，宿主日后全权限跑 git 触发）
        # 与 config（alias.*/core.sshCommand 等「解释执行任意命令」全在这）。
        # 全机任意深度（比 root 相对更简更安全）。
        '(deny file-write* (regex "/\\.git/hooks/"))',
        '(deny file-write* (regex "/\\.git/config$"))',
        # ── 读黑名单（049）：凭证是唯一「读了就等于泄漏」的资源 ──
        # 只围 .env 一族，与写侧同形状（root 级 .env* 前缀 + 任意深度
        # .env 文件/目录）。home 凭证（~/.ssh/id_rsa 等）不在这里——deny
        # read 会打断沙箱内 git 的 SSH 认证，摩擦不可预估，那层交给
        # terminal.needs_confirm 的应用层确认（049 ADR 拍板 2）。
        # 实测语义：cat → EPERM；open() → PermissionError（但解释器本身
        # 跑完）；load_dotenv() → 静默返回 False 不抛（本项目 13 处全在
        # 主进程、零处沙箱内直接 open，故零摩擦）。
        f'(deny file-read* (regex "^{rx_root}/\\.env"))',
        '(deny file-read* (regex "/\\.env$"))',
        '(deny file-read* (regex "/\\.env/"))',
    ]
    # 目录黑名单（root 相对；子 agent root=副本时这些路径不存在，规则无害
    # 空转——零特判）。052 记忆围栏同形状追加：subpath 对普通文件
    # （graph.json）也生效，本机实测写/追加/rm 全 EPERM 而 cat 照常放行。
    rules += [
        f'(deny file-write* (subpath "{r}/{d}"))'
        for d in (*_BLACKLIST_DIRS, *MEMORY_WRITE_FENCE)
    ]
    return "\n".join(rules)


def wrap_command(command: str, *, root: Path) -> tuple[list[str] | None, str]:
    """把 shell 命令包进沙箱。返回 (argv, backend)；无后端 (None, "off")
    = 降级原样跑（调用方 shell=True 走现状路径）。

    argv 形态：sandbox-exec -p <profile> /bin/sh -c <command>——
    shell 语义（管道/重定向/展开）由围栏内的 sh 承担，与现状 shell=True
    等价，只是多了进程级写围栏。
    """
    if detect_backend() == "seatbelt":
        return (
            ["sandbox-exec", "-p", build_seatbelt_profile(root), "/bin/sh", "-c", command],
            "seatbelt",
        )
    return None, "off"
