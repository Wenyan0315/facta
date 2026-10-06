"""全项目路径常量的唯一真值源（P1-3 补强，2026-09-10 code review）。

P1-3 先把 "data/notes" 收口到 __main__，但 evals 与 demo 是离线脚本、
不走 ToolContext，各自又带一份字面量副本——architecture.md 记录的不变量
「__main__ 与 evals 共用同一 loader → 评估与线上永远同一份语料」
就悬在「几处副本恰好没漂移」上。本模块让所有消费者 import 同一个常量。

规则：跨模块共享的路径常量住这里；只被一个模块用的路径（如 MEMORY_PATH
只归 __main__）留在消费者本地，不提前搬家。

S8a 边界①收口（2026-09-25）：data/ 全族锚到 WORKSPACE_ROOT（`__file__` 上两级），
不再依赖「从仓库根启动」——VS Code 壳 / 常驻服务拉子进程时 cwd 不可控，相对路径
会因找不到 `data/notes` 直接崩。env 覆盖 FACTA_DATA_DIR 后续已补（见下 WORKSPACE_ROOT
注释）；ADR 090（R03）把装配层残余字面量与写入围栏也收到同一推导上。
"""
from __future__ import annotations

import os
from pathlib import Path

# S4 文件工具的 workspace 围栏根：项目仓库根（src/facta/paths.py 上两级）。
# 用 __file__ 锚定而非 Path.cwd()——无论从哪个目录启动，agent 只碰得到本项目的
# 文件（S4a 裁定：workspace=项目根）
#
# ADR 071 触发信号兑现（评审 § 产品边界「工作目录固定仓库根，wheel 安装语义需专项验收」）：
# 「数据目录与代码目录分离」的真消费者 = 开源分发后用户 `pip install facta` 而非 `pip
# install -e .`，包装在 site-packages/facta 下，`__file__.parents[2]` 跳到 site-packages
# 之外，data/ 写不进散落且首次启动找不到 notes 语料 → 升级补 FACTA_DATA_DIR 覆盖。
# 边界仍锚硬保证：`paths.MEMORY_WRITE_FENCE` 写的相对路径是「data/notes/learned/graph.json」，
# FACTA_DATA_DIR 设了就把它替换为绝对根，开发态（pip install -e）零变化。
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
# DATA_ROOT 走 _data_root() 函数：env 优先，缺省回 WORKSPACE_ROOT/data。
# 模块加载期一次性结算（其他模块 `from facta.paths import DATA_ROOT` 拿到的仍是 Path 对象，
# 行为与原常量一致）。
DATA_ROOT = (
    Path(os.environ["FACTA_DATA_DIR"]).resolve()
    if os.environ.get("FACTA_DATA_DIR")
    else WORKSPACE_ROOT / "data"
)

NOTES_DIR = DATA_ROOT / "notes"   # 知识库笔记目录（语料资产，进 git）
LEARNED_DIR = DATA_ROOT / "learned"   # M6.4 记忆固化目录（项目级记忆资产，进 git；用户级记忆另行住仓库外）
SESSIONS_DIR = DATA_ROOT / "memory" / "sessions"   # S8a 会话仓库：所有会话同住这里，身份=文件名（`%Y%m%d-%H%M%S.json`）；S1~S7 时期它是「归档仓库」，老归档文件名本就是合法 id，原地即完成迁移
# P0-3（038）Run checkpoint 账本目录：每会话一个 append-only JSONL（`{sid}.jsonl`），
# 记「点了什么菜（intent）/ 回了什么结果（result）」。与 session.json 的分工——
# 底片真值源是 session.json，账本只存底片表达不了的东西：执行前意图（区分
# 「没跑过」vs「跑了但结果丢了」）与结果全文（healing 时原样回注，不截断）。
# 运行时数据，不进 git（同 audit/）。
CHECKPOINT_DIR = DATA_ROOT / "checkpoints"

# M6.5 用户级记忆默认位置：仓库外单文件（~ 锚定，与仓库内 DATA_ROOT 族不同列）。
# 跨项目共享、不进任何 git——M6.4 红线「位置没建好前不开桶」的
# 兑现：用户级信息（偏好/习惯/行程）从「一律不记」变「分流到这里」。
_USER_MEMORY_DEFAULT = Path.home() / ".facta" / "user.md"
# 开源改名前旧目录（personal-agent → facta）：仅一次性迁移用，不留双读逻辑——
# 两个真值源就是漂移温床（本模块开篇立论）。迁移后旧目录不再被任何代码引用。
_LEGACY_USER_DIR = Path.home() / ".personal-agent"


def _migrate_legacy_user_dir() -> None:
    """旧目录一次性搬到新目录（品牌改名 personal-agent → facta）。

    只在「新目录不存在 + 旧目录存在」时整目录 move；两边都在属于异常状态，
    不猜不合并，留给人处理（记忆不可逆，045/064 同款保守）。
    """
    if _USER_MEMORY_DEFAULT.parent.exists() or not _LEGACY_USER_DIR.exists():
        return
    import shutil
    shutil.move(str(_LEGACY_USER_DIR), str(_USER_MEMORY_DEFAULT.parent))


def user_memory_path() -> Path:
    """用户级记忆位置（默认值唯一真值源在此；FACTA_USER_MEMORY 环境变量
    可覆写——测试 tmp_path 隔离的注入点，S6a WORKSPACE_ROOT 注入化同款）。

    函数而非常量：消费方三处（assemble / CLI 退出复盘 / Web 归档），
    覆写逻辑跟着真值源走，不散三份（P1「evals 各带副本漂移」的老病）。
    """
    if "FACTA_USER_MEMORY" not in os.environ:
        _migrate_legacy_user_dir()
    return Path(os.environ.get("FACTA_USER_MEMORY", str(_USER_MEMORY_DEFAULT)))

# S6a worktree 沙箱根：子 agent 的文件改动隔离区（运行时数据，gitignore 排除）。
# 放仓库内 data/ 下（与 memory/audit 同区）；files.py 黑名单挡住——否则
# search_code 的 rglob 会扫进 worktree 造成同文件双重命中
WORKTREES_DIR = DATA_ROOT / "worktrees"

# S7a 知识图谱落盘位置：notes 的结构化投影——文本可读、可审查、可 diff，
# 知识资产进 git（与 vector_db 二进制缓存相反的判断）
GRAPH_PATH = DATA_ROOT / "graph.json"

def _fence_entry(p: Path) -> str:
    """围栏条目（ADR 090）：在 WORKSPACE_ROOT 内保持相对 posix（files.py 的
    rel 比对与 sandbox 的 `root / d` 拼接两种消费姿势都吃得下）；根外给绝对
    路径——files.py 臂够不着 workspace 外是无害空转，sandbox 臂靠 pathlib
    「绝对右操作数返回其本身」恰好拿到真路径（tmp 族数据根补上写白名单缺口）。
    """
    try:
        return p.relative_to(WORKSPACE_ROOT).as_posix()
    except ValueError:
        return str(p)


# 052 记忆写围栏：只作用于「写」的清单（root 相对 posix；数据根迁出则绝对，
# 见 _fence_entry）。与 files.py 的 BLACKLIST_DIRS 分列——那份清单被
# _resolve_in_workspace 用于读写两条路径，而 notes 是必须可读的语料资产
# （r1/i3/i4 的 verify 基准就是这 15 篇），塞进去等于当场产品回归。消费方两处
# 同源 import：files.py（write_file 写侧拒）与 sandbox.py（deny file-write*）。
# 记忆落盘唯一入口＝工具进程（write_note / sync_graph / 记忆固化，都是进程内写，
# 不经这两层）。ADR 090 起由常量推导，默认布局输出与旧字面量逐字节一致。
MEMORY_WRITE_FENCE = tuple(_fence_entry(p) for p in (NOTES_DIR, LEARNED_DIR, GRAPH_PATH))

# 敏感目录黑名单（files.py 读+写双挡 / sandbox.py 写侧 deny 的唯一真值源，
# ADR 090 合一——此前两模块各抄一份字面量，漂移即静默裸奔）。数据条目随
# DATA_ROOT 走；servers/sandbox 与 .venv 是 workspace 静态项。
BLACKLIST_DIRS = tuple(
    _fence_entry(p)
    for p in (
        DATA_ROOT / "memory",
        DATA_ROOT / "audit",
        DATA_ROOT / "vector_db",
        WORKSPACE_ROOT / "servers" / "sandbox",
        WORKSPACE_ROOT / ".venv",
        WORKTREES_DIR,
    )
)
