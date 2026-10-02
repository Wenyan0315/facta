"""run_command 进程级沙箱（048）：确认弹窗不再是唯一防线。

形态（048 裁定·甲案）：平台派发抽象层——detect_backend() 探测本机可用
后端，wrap_command() 把 shell 命令包进沙箱 argv；无后端环境诚实降级
（原样跑 + 审计打标 off），不假装有围栏。macOS 用系统自带 sandbox-exec
（seatbelt）；Linux 用 bubblewrap（072 甲案落地，探测制：which 命中还要
空跑成功——Ubuntu 23.10+ AppArmor 限制 userns，binary 在不代表能跑，
探针实锤 ubuntu-latest FAIL / ubuntu-22.04 OK）；Windows 后端挂触发信号。

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
import subprocess
import sys
import tempfile
from pathlib import Path

from facta.paths import MEMORY_WRITE_FENCE

ENV_SWITCH = "FACTA_SANDBOX"   # =off 强制关闭（其余值/缺省 = auto）

# 与 files.py 黑名单交叉同源的目录项（root 相对）——tests/test_sandbox.py
# 断言 files.py 的每一项在这里都有对应 deny，漂移即红。
_BLACKLIST_DIRS = (
    "data/memory", "data/audit", "data/vector_db",
    "servers/sandbox", ".venv", "data/worktrees",
)


def detect_backend() -> str | None:
    """探测可用沙箱后端；无 → None（诚实降级）。

    不做模块级缓存：which 是毫秒级，run_command 频率低；缓存会让
    FACTA_SANDBOX=off 在同进程内不生效。
    """
    if os.environ.get(ENV_SWITCH) == "off":
        return None
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if sys.platform.startswith("linux") and _bwrap_available():
        return "bwrap"
    return None


def _bwrap_available() -> bool:
    """bwrap 探测制（072 甲案）：which 命中 + 空跑成功才算在——Ubuntu 23.10+
    用 AppArmor 限制非特权 userns 创建（kernel.apparmor_restrict_unprivileged_userns=1），
    binary 装了也跑不起（探针 run 36954014738：ubuntu-latest FAIL、
    ubuntu-22.04 OK）。试跑失败诚实降级 off，不假装有围栏——048 同款裁定。

    不做缓存：which + 空跑合计几十毫秒，run_command 频率低；缓存会让
    FACTA_SANDBOX=off 在同进程内不生效。
    """
    if not shutil.which("bwrap"):
        return False
    try:
        proc = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-pid",
             "--die-with-parent", "/bin/true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


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


def build_bwrap_argv(command: str, *, root: Path) -> list[str]:
    """生成 bwrap argv（072 甲案）。root 必须已 resolve——与 seatbelt 同款
    锚点纪律：bwrap 按真实路径挂 bind，symlink 锚点会让围栏整圈对不上。

    语义逐条对齐 seatbelt profile（探针 run 36954014738 实测成立）：
    - --ro-bind / / ≈ (allow file-read*) + 写默认拒：全机可读、处处只读
    - 写白名单 = root + /tmp（seatbelt 的 root/TMPDIR//private/tmp 对应物；
      Linux 侧 TMPDIR 缺省即 /tmp）
    - --dev /dev：/dev/null 一族可写——git 等工具启动即开 /dev/null，
      ro-bind / 会连设备写都掐死（seatbelt 的 (allow (literal "/dev/null")) 同款）
    - 黑名单后 bind 覆盖白名单：bwrap 按序应用 bind、后者盖前者——与
      seatbelt「同 operation 后定义者胜出」同构（测试钉住顺序）
    - 网络不放 --unshare-net：与 048「网络维度另案」同款裁定

    与 seatbelt 的三个已知语义差（ADR 072 补注，均为 bwrap 无 regex 所致）：
    - 遮蔽清单是 wrap 时枚举不是模式匹配——wrap 之后新建的 .env 不在围栏内
    - .env 目录用 --tmpfs 遮蔽：读空、写「成功」但随进程消失（文件则用
      --ro-bind /dev/null，读空写 EROFS，与 seatbelt 完全同语义）
    - 报错文案是 Read-only file system（EROFS）不是 Operation not permitted
      （EPERM）——模型自我纠正的提示词要认两种（terminal 描述已对齐）
    """
    r = str(root)
    argv = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--tmpfs", "/dev/shm",   # multiprocessing/posix shm（seatbelt 的 ipc-posix-shm 对应物）
        "--bind", r, r,
        "--bind", "/tmp", "/tmp",
    ]
    # 写黑名单：ro-bind 自身盖回只读（可读不可写 = seatbelt deny file-write*）。
    # 存在才 bind——seatbelt 对不存在路径「规则无害空转」，bwrap 对不存在的
    # source 直接报错，判在是显式税。
    for rel in (*_BLACKLIST_DIRS, *MEMORY_WRITE_FENCE):
        p = root / rel
        if p.exists():
            argv += ["--ro-bind", str(p), str(p)]
    # .git 丙案同款：只围死 hooks 与 config 两个毒化入口，保日常 commit 闭环
    for p in (root / ".git" / "hooks", root / ".git" / "config"):
        if p.exists():
            argv += ["--ro-bind", str(p), str(p)]
    # 读+写双遮（049 对齐）：root 级 .env* 前缀 + 任意深度 .env 文件/目录
    masked: set[Path] = set()
    for p in sorted(root.glob(".env*")) + sorted(root.rglob(".env")):
        if p in masked or not p.exists():
            continue
        masked.add(p)
        if p.is_dir():
            argv += ["--tmpfs", str(p)]
        else:
            argv += ["--ro-bind", "/dev/null", str(p)]
    argv += ["--unshare-pid", "--die-with-parent", "/bin/sh", "-c", command]
    return argv


def wrap_command(command: str, *, root: Path) -> tuple[list[str] | None, str]:
    """把 shell 命令包进沙箱。返回 (argv, backend)；无后端 (None, "off")
    = 降级原样跑（调用方 shell=True 走现状路径）。

    argv 形态：<backend argv> /bin/sh -c <command>——
    shell 语义（管道/重定向/展开）由围栏内的 sh 承担，与现状 shell=True
    等价，只是多了进程级写围栏。
    """
    backend = detect_backend()
    if backend == "seatbelt":
        return (
            ["sandbox-exec", "-p", build_seatbelt_profile(root), "/bin/sh", "-c", command],
            "seatbelt",
        )
    if backend == "bwrap":
        return build_bwrap_argv(command, root=root), "bwrap"
    return None, "off"
