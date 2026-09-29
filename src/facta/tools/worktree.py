"""S6a worktree 原语：子 agent 的文件系统沙箱（git worktree 封装）。

设计拍板（S6a 草案）：
- 创建：`git worktree add <dir> -b agent/<name>`——分支名可追溯（谁派的任务）
- 生命周期：spawn 结束（合回或丢弃）即清理；启动时扫残留回收
  （进程被杀留下半截 worktree 的防线——「先杀进程再删文件」血案同款）
- 合回：worktree 内 add -A + commit → 主仓库 merge --no-edit。
  主工作区脏且撞文件时 git 拒绝——错误串返回（反馈环惯例，模型/用户可自纠）
- 丢弃：worktree remove --force + 分支删除，零残留

零新依赖：subprocess 调 git 命令行（GitPython 不进项目——CLI 面够窄，
每次一条命令，没有对象图需求；MCP stdio 子进程先例同款判断）。

错误协议：所有函数返回 str（成功=结果/空改动描述，失败=错误串）——
与工具层「错误也返回字符串」同一纪律，调用方（spawn）直接拼进结论。
"""

from __future__ import annotations

import subprocess
import threading
import uuid
from pathlib import Path

from facta.paths import WORKSPACE_ROOT, WORKTREES_DIR

_TIMEOUT = 30   # git 命令超时（本地操作，给足但不无限等）

# 主仓库 git 元数据的进程级互斥（S8a 多会话并发）：worktree add/remove、
# merge、branch 写的都是同一个 .git（index.lock / refs / worktrees 元数据）。
# 两个会话的 spawn 同时合回，第二个必定撞 index.lock 而失败——改动虽还在
# agent/<name> 分支上可手工抢救，用户看到的却是「子 agent 白跑了」。
# 锁只圈碰主仓库的那几条命令：worktree 内的 add/commit 走私有 index，
# 保持并发（那才是 worktree 隔离的意义所在）。
WORKTREE_LOCK = threading.Lock()


def _git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    """跑一条 git 命令，返回 (exit_code, 合并输出)。

    cwd 默认 None 而非 WORKSPACE_ROOT：默认参数在模块加载时固化 Path 对象，
    测试 monkeypatch 模块名字对已固化的默认值无效（S6a 开发实踩——测试的
    worktree 建到了真项目里）。函数体内解析名字，patch 运行时生效。
    """
    if cwd is None:
        cwd = WORKSPACE_ROOT
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, f"git 命令执行失败：{e}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output


def create_worktree() -> tuple[Path, str]:
    """创建 worktree：data/worktrees/<uuid>，分支 agent/<uuid>（从当前 HEAD 切）。

    返回 (目录, 错误串)。错误串非空 = 创建失败（调用方走反馈环）。
    """
    name = uuid.uuid4().hex[:8]
    wt_dir = WORKTREES_DIR / name
    branch = f"agent/{name}"
    with WORKTREE_LOCK:
        code, out = _git("worktree", "add", str(wt_dir), "-b", branch)
    if code != 0:
        return wt_dir, f"worktree 创建失败：{out}"
    return wt_dir, ""


def worktree_changes(wt: Path) -> str:
    """改动摘要（给确认缝展示）：status --short + diff 统计。空串 = 无改动。"""
    code_s, status = _git("status", "--short", cwd=wt)
    if code_s != 0:
        return f"（改动检查失败：{status}）"
    if not status:
        return ""
    # diff --stat 给文件级摘要（行数变动），比全文 diff 适合弹窗展示
    _, diffstat = _git("diff", "HEAD", "--stat", cwd=wt)
    return f"{status}\n{diffstat}".strip()


def commit_and_merge_back(wt: Path, message: str) -> str:
    """合回：worktree 内 add -A + commit → 主仓库 merge → 清理 worktree。

    空改动守卫：无改动时不 commit 不 merge（返回「无改动」）——
    与 VectorStore 删除守卫同一哲学：空操作前置拦截。
    """
    changes = worktree_changes(wt)
    if not changes:
        discard_worktree(wt)
        return "worktree 无改动，已清理"

    branch = f"agent/{wt.name}"
    code_c, out_c = _git("add", "-A", cwd=wt)
    if code_c != 0:
        return f"staging 失败：{out_c}"
    # commit 身份显式带（CI runner 无全局 git 配置会 commit 失败——
    # 本地全绿 CI 红的坑）；且语义正确：spawn 的 commit 本就是 agent 干的，
    # 归属标注清楚（不覆盖用户主分支上自己的 commit 身份）
    code_c, out_c = _git(
        "-c", "user.name=cortex-agent", "-c", "user.email=agent@cortex.local",
        "commit", "-m", message, cwd=wt,
    )
    if code_c != 0:
        return f"commit 失败：{out_c}（改动仍在 worktree：{wt}）"

    # 主仓库段（merge/remove/branch）持锁：与另一会话的 spawn 合回互斥。
    # worktree 内的 add/commit 已在上面跑完（私有 index，不抢主仓库锁）。
    with WORKTREE_LOCK:
        code_m, out_m = _git("merge", "--no-edit", branch)
        # 无论 merge 成败都清 worktree（成功=已进主分支；失败=错误串带回，
        # 子 agent 改动在 agent/<name> 分支上仍可手工抢救——分支先不删）
        code_r, out_r = _git("worktree", "remove", str(wt))
        if code_m != 0:
            return f"merge 失败：{out_m}（子 agent 改动保留在分支 {branch}，可手工处理；worktree 目录：{'已清理' if code_r == 0 else out_r}）"
        _git("branch", "-d", branch)   # 合回成功才删分支
    return f"已合回主分支：\n{changes}"


def discard_worktree(wt: Path) -> str:
    """丢弃：worktree remove --force + 删分支，零残留。"""
    branch = f"agent/{wt.name}"
    with WORKTREE_LOCK:
        code, out = _git("worktree", "remove", "--force", str(wt))
        _git("branch", "-D", branch)   # -D：未合并的分支也删（丢弃语义）
    return "" if code == 0 else f"worktree 清理失败：{out}"


def cleanup_stale_worktrees() -> int:
    """启动回收：data/worktrees/ 下残留目录全部移除（进程被杀防线）。

    返回清理数。半截目录（git 元数据已坏，worktree remove 失败）直接
    rmtree 兜底 + branch -D——启动路径的清理不因单个坏目录中断。
    """
    if not WORKTREES_DIR.exists():
        return 0
    count = 0
    import shutil
    for wt in WORKTREES_DIR.iterdir():
        if not wt.is_dir():
            continue
        code, _ = _git("worktree", "remove", "--force", str(wt))
        if code != 0:
            shutil.rmtree(wt, ignore_errors=True)
        _git("branch", "-D", f"agent/{wt.name}")
        count += 1
    return count
