"""S6a worktree 原语：子 agent 的文件系统沙箱（git worktree 封装）。

设计拍板（S6a 草案）：
- 创建：`git worktree add <dir> -b agent/<name>`——分支名可追溯（谁派的任务）
- 生命周期：spawn 结束（合回或丢弃）即清理；启动时扫残留回收
  （进程被杀留下半截 worktree 的防线——「先杀进程再删文件」血案同款）
- 合回：worktree 内 add -A + commit → 主仓库 merge --no-edit。
  主工作区脏且撞文件时 git 拒绝——错误串返回（反馈环惯例，模型/用户可自纠）
- 丢弃：worktree remove --force + 分支删除，零残留

P0 绑定化（Spec 1 工作区绑定 + Spec 2 产物保留与安全合入）：
- git 一律锚定显式仓库根（repo_root / merge_into）——产物住绑定的用户仓库，
  Facta 主仓不再参与任何提交/分支；目录仍集中住 data/worktrees（清理面单一）
- 分支名真值源 = 调用方传入的 worktree_branch（Task 字段）；目录名反推
  `agent/<wt.name>` 只剩无绑定旧任务的兼容路径
- 产物检查升级四段（已提交未合入/已暂存/未暂存/未跟踪）：「已提交但工作区
  干净」不得判为无产物（P0-2 根因——曾被当空沙箱 branch -D 丢提交）
- 合入四态化（merged/empty/conflict/failed）：前置核对 + 冲突 abort 保主树
  干净，冲突/失败都保留 worktree 与分支可抢救（P0-3 根因——曾撞冲突即
  remove worktree、主树留 MERGE_HEAD 半合并态）

零新依赖：subprocess 调 git 命令行（GitPython 不进项目——CLI 面够窄，
每次一条命令，没有对象图需求；MCP stdio 子进程先例同款判断）。

错误协议：所有函数返回 str 或 (status, detail)（成功=结果描述，失败=错误串）
——与工具层「错误也返回字符串」同一纪律，调用方（spawn/收口）直接拼进结论；
四态元组是内部结构化契约，不外泄为新错误协议。
"""

from __future__ import annotations

import subprocess
import threading
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from facta.paths import NOTES_DIR, WORKSPACE_ROOT, WORKTREES_DIR
from facta.tools.context import ToolContext, WorkspaceBinding
from facta.tools.files import register_file_tools
from facta.tools.registry import ToolRegistry
from facta.tools.terminal import register_terminal_tools

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
    # 只去尾部空白，不去头部：status --porcelain 首列（暂存区状态）是语义位，
    # strip 会把首行「 M file」啃成「M file」——未暂存被误判成已暂存
    output = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
    return proc.returncode, output


@dataclass(frozen=True)
class ChangeSet:
    """产物四段检查结果（Spec 2.1）：任一段非空即「有产物」。

    分段存文本而非路径列表——消费方两类：确认缝/接口直接展示（summary 拼
    中文标注串），收口判定只要真假（__bool__ = any，旧调用
    `if worktree_changes(wt):` 语义保持）。error 非空 = 检查本身失败，
    此时各段内容不可信，调用方按「有产物」保守保留（宁多留不误删——
    P0-2 丢产物根因的同款保守）。
    """
    committed: str = ""       # 已提交未合入段摘要（log --oneline + diff --stat 风格）
    staged: str = ""
    unstaged: str = ""
    untracked: str = ""
    baseline_inferred: bool = False   # merge-base 回落推断标注（旧任务）
    error: str = ""           # 检查失败；调用方按「有产物」保守保留

    def any(self) -> bool:
        """有产物判定：error 非空 或 任一段非空。"""
        return bool(self.error or self.committed or self.staged or self.unstaged or self.untracked)

    def summary(self) -> str:
        """兼容旧字符串展示：分段加中文标注；error 非空返回错误串。"""
        if self.error:
            return self.error
        parts = []
        if self.baseline_inferred:
            parts.append("（基线为推断值：merge-base 回落）")
        if self.committed:
            parts.append(f"已提交未合入：\n{self.committed}")
        if self.staged:
            parts.append(f"已暂存：\n{self.staged}")
        if self.unstaged:
            parts.append(f"未暂存：\n{self.unstaged}")
        if self.untracked:
            parts.append(f"未跟踪：\n{self.untracked}")
        return "\n".join(parts)

    __bool__ = any   # 关键：旧调用 `if worktree_changes(wt):` 语义保持


def create_worktree(
    repo_root: Path | None = None,
    branch: str | None = None,
    baseline: str = "",
) -> tuple[Path, str]:
    """创建 worktree：data/worktrees/<uuid>，分支从 baseline（缺省 repo 当前 HEAD）切。

    repo_root 缺省 WORKSPACE_ROOT（无绑定旧调用兼容）；绑定路径必须显式传
    用户仓库根——分支/提交进用户仓库，Facta 主仓不参与（Spec 1 §2.3）。
    branch 缺省生成 agent/<uuid8>：绑定路径调用方应显式传并存 Task.worktree_branch
    （分支名真值源是字段，废除目录名反推）；生成值与目录名同源只服务
    无绑定旧路径的兼容反推。baseline 非空 → 从钉死基线切（预览可复现）。

    返回 (目录, 错误串)。错误串非空 = 创建失败（调用方走反馈环）。
    """
    repo = WORKSPACE_ROOT if repo_root is None else repo_root
    name = uuid.uuid4().hex[:8]
    wt_dir = WORKTREES_DIR / name
    if branch is None:
        branch = f"agent/{name}"
    args = ["worktree", "add", str(wt_dir), "-b", branch]
    if baseline:
        args.append(baseline)
    with WORKTREE_LOCK:
        code, out = _git(*args, cwd=repo)
    if code != 0:
        return wt_dir, f"worktree 创建失败：{out}"
    return wt_dir, ""


def registry_in_worktree(
    registry: ToolRegistry,
    wt: Path,
    binding: WorkspaceBinding | None = None,
) -> ToolRegistry:
    """worktree 内干活的子 registry：file/terminal 五件重锚 wt，其余原样共享。

    重锚=重新注册（闭包锚 wt 目录）；共享=搬运同一 Tool 对象（闭包锚主资源
    ——知识库/待办/时钟共享是正确语义）。审计同源（registry.audit 透传），
    S3「单一必经点」与 S5a「确认缝收口不分叉」都保持。file/terminal 五件
    只读 ctx.workspace_root（files/terminal 的注册体），重锚不需要其余资源。
    binding 透传（P0）：沙箱里的工具继承父任务的工作区绑定（仓库/目标分支/
    基线），子 agent 的 git 落点与父任务同一绑定——父子不串仓（Spec 1 §2.4）。
    子 agent 沙箱（spawn）与任务沙箱（线五①调度接线）共用这一份。
    """
    # scope_check 一并继承（057）：范围声明锚的是主会话的 plan 棋盘，沙箱里的
    # agent 受主计划约束是正确语义——不继承就成洞（主 agent 声明窄范围，
    # 再把渗出步骤派进沙箱，沙箱里想用什么用什么）
    sub = ToolRegistry(audit=registry.audit, scope_check=registry.scope_check)
    # notes_dir 是 ToolContext 的必填位，但五个重锚工具只读 workspace_root
    # （files/terminal 的注册体）——给缺省值即可，无第二个真值源
    wt_ctx = ToolContext(notes_dir=NOTES_DIR, workspace_root=wt, binding=binding)
    register_file_tools(sub, wt_ctx)
    register_terminal_tools(sub, wt_ctx)
    for name in registry.names():
        if name not in {"read_file", "search_code", "list_dir", "write_file", "run_command"}:
            tool = registry.get(name)
            if tool is not None:
                sub.register(tool)
    return sub


def worktree_changes(wt: Path, baseline: str = "", target_branch: str = "") -> ChangeSet:
    """产物四段检查（Spec 2.1）：已提交未合入 / 已暂存 / 未暂存 / 未跟踪。

    baseline 给定 → ①段 = `log baseline..HEAD` + `diff baseline...HEAD --stat`；
    baseline 空、target_branch 给定 → merge-base 推断基线（旧任务回落，
    baseline_inferred 标注「基线为推断值」）；都空 → ①段恒空（无绑定旧调用
    语义保持：只看工作区改动）。②③④段由 status --porcelain 解析。

    「已提交但工作区干净」不得判为无产物（P0-2 根因：这样的产物曾被判空 →
    收口走 discard → branch -D 丢提交）。检查失败进 error，调用方保守保留。
    """
    code_s, status = _git("status", "--porcelain", cwd=wt)
    if code_s != 0:
        return ChangeSet(error=f"（改动检查失败：{status}）")
    staged_lines: list[str] = []
    unstaged_lines: list[str] = []
    untracked_lines: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked_lines.append(line)
            continue
        x, y = line[:1], line[1:2]   # porcelain：XY 两列状态（暂存区/工作区）
        if x not in (" ", "?"):
            staged_lines.append(line)
        if y not in (" ", "?"):
            unstaged_lines.append(line)

    committed = ""
    inferred = False
    if not baseline and target_branch:
        code_b, out_b = _git("merge-base", target_branch, "HEAD", cwd=wt)
        if code_b != 0:
            return ChangeSet(error=f"（基线推断失败：{out_b}）")
        baseline = out_b.strip()
        inferred = True
    if baseline:
        code_l, log = _git("log", "--oneline", f"{baseline}..HEAD", cwd=wt)
        if code_l != 0:
            return ChangeSet(error=f"（已提交段检查失败：{log}）")
        code_d, diffstat = _git("diff", f"{baseline}...HEAD", "--stat", cwd=wt)
        if code_d != 0:
            return ChangeSet(error=f"（已提交段检查失败：{diffstat}）")
        committed = f"{log}\n{diffstat}".strip()
    return ChangeSet(
        committed=committed,
        staged="\n".join(staged_lines),
        unstaged="\n".join(unstaged_lines),
        untracked="\n".join(untracked_lines),
        baseline_inferred=inferred,
    )


def _merge_precheck(dest: Path, target_branch: str, branch: str) -> tuple[str, str]:
    """合入前置核对（Spec 2.3）：返回 (错误串, 目标当前分支)。

    错误串非空 = 不满足前置 → 调用方拒绝合入，目标零改动、候选保留。
    核对顺序：目标分支匹配 → 无 MERGE_HEAD（合并中）→ 工作区干净——
    合并中是更具体的现场状态，先报它（失败处理表两场景分列）。
    """
    code_b, cur = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)
    if code_b != 0:
        return f"无法确定合入目标分支：{cur}（目标零改动，产物保留在分支 {branch}）", ""
    cur = cur.strip()
    if target_branch and cur != target_branch:
        return (
            f"目标分支不匹配：合入目标当前检出 {cur}，绑定 target_branch={target_branch}"
            f"（目标零改动，产物保留在分支 {branch}）"
        ), cur
    code_mh, _ = _git("rev-parse", "--verify", "-q", "MERGE_HEAD", cwd=dest)
    if code_mh == 0:
        return (
            f"目标处于合并中（存在 MERGE_HEAD），请先完成或中止合并"
            f"（目标零改动，产物保留在分支 {branch}）"
        ), cur
    code_st, dest_status = _git("status", "--porcelain", cwd=dest)
    if code_st != 0:
        return f"目标状态检查失败：{dest_status}（目标零改动，产物保留在分支 {branch}）", cur
    if dest_status:
        return (
            f"目标工作区不干净，合入前置不满足（目标零改动，产物保留在分支 {branch}）：\n{dest_status}"
        ), cur
    return "", cur


def _merge_failed(dest: Path, wt: Path, branch: str, out_m: str) -> tuple[str, str]:
    """merge 失败分岔（Spec 2.3）：冲突 abort 保主树，非冲突如实报错。

    有 MERGE_HEAD = 冲突 → 抓冲突文件清单 + merge --abort 恢复目标干净；
    无 = 非冲突失败（index.lock/超时等）。两条路都保留 worktree 与分支——
    候选可抢救/重试，绝不趁乱删（P0-3 根因的反面）。
    """
    code_mh, _ = _git("rev-parse", "--verify", "-q", "MERGE_HEAD", cwd=dest)
    if code_mh == 0:
        _, conflicts = _git("diff", "--name-only", "--diff-filter=U", cwd=dest)
        code_ab, out_ab = _git("merge", "--abort", cwd=dest)
        aborted = "" if code_ab == 0 else f"（merge --abort 也失败：{out_ab}）"
        return "conflict", (
            f"merge 冲突，已中止并恢复目标{aborted}；冲突文件：\n{conflicts}\n"
            f"候选保留在分支 {branch}（worktree：{wt}），可重试合入或手工处理"
        )
    return "failed", (
        f"merge 失败：{out_m}（目标未合入，候选保留在分支 {branch}，worktree：{wt}）"
    )


def commit_and_merge_back(
    wt: Path,
    message: str,
    *,
    merge_into: Path | None = None,
    branch: str | None = None,
    target_branch: str = "",
    baseline: str = "",
) -> tuple[str, str]:
    """合入：四段检查 → 按需 commit → 合入前置核对 → merge → 成功才清理。

    返回 (status, detail)，status ∈ {"merged","empty","conflict","failed"}；
    detail 是给人/模型读的中文串（「错误也返回字符串」反馈环惯例不变）。

    产物保守原则（P0-2 根因）：四段任一非空或检查失败（error）都按有产物
    走完整流程，绝不先行丢弃；只有「确认零产物」才 discard 收口（empty）。
    冲突/失败都保留 worktree 与分支（候选可抢救/重试）——旧实现撞冲突即
    remove worktree、主树留 MERGE_HEAD 半合并态，正是 P0-3 根因。

    branch / target_branch / baseline 都以调用方绑定字段为真值源
    （Task.worktree_branch / target_branch / baseline）；branch 缺省
    agent/<wt.name> 反推仅限无绑定旧任务路径。
    """
    if branch is None:
        branch = f"agent/{wt.name}"   # 兼容反推：仅限无绑定旧任务路径
    dest = WORKSPACE_ROOT if merge_into is None else merge_into

    # ① 四段产物检查：确认零产物（且检查无错）才允许丢弃
    changes = worktree_changes(wt, baseline, target_branch)
    if not changes.any():
        err = discard_worktree(wt, repo_root=dest, branch=branch)
        detail = "worktree 无产物，已清理"
        if err:
            detail += f"（清理失败：{err}）"
        return "empty", detail

    # ② 按需 commit（Spec 2.2）：仅工作区有未提交改动才 add+commit——
    # 已提交产物直接参与合入，不补空 commit（nothing-to-commit 防御）
    code_s, porcelain = _git("status", "--porcelain", cwd=wt)
    if code_s != 0:
        return "failed", f"改动检查失败：{porcelain}（产物保留在分支 {branch}，worktree：{wt}）"
    if porcelain:
        code_a, out_a = _git("add", "-A", cwd=wt)
        if code_a != 0:
            return "failed", f"staging 失败：{out_a}（产物保留在 worktree：{wt}）"
        # commit 身份显式带（CI runner 无全局 git 配置会 commit 失败——
        # 本地全绿 CI 红的坑）；且语义正确：spawn 的 commit 本就是 agent 干的，
        # 归属标注清楚（不覆盖用户主分支上自己的 commit 身份）
        code_c, out_c = _git(
            "-c", "user.name=cortex-agent", "-c", "user.email=agent@cortex.local",
            "commit", "-m", message, cwd=wt,
        )
        if code_c != 0:
            return "failed", f"commit 失败：{out_c}（产物仍在 worktree：{wt}）"

    # ③~⑥ 合入段持锁：与另一会话的合回/回收互斥（主仓库 refs/index 竞写）。
    # worktree 内的 add/commit 已在上面跑完（私有 index，不抢主仓库锁）。
    with WORKTREE_LOCK:
        # ③ 合入前置检查（Spec 2.3）：不满足 = failed，目标零改动、候选保留
        precheck_err, cur = _merge_precheck(dest, target_branch, branch)
        if precheck_err:
            return "failed", precheck_err

        # ④ 合入（Spec 2 分歧二：维持 git merge 默认——ff 可行则 ff，否则 merge commit）
        code_m, out_m = _git("merge", "--no-edit", branch, cwd=dest)
        if code_m != 0:
            # ⑤ 失败分岔：conflict（abort 恢复目标）或 failed，都保留候选
            return _merge_failed(dest, wt, branch, out_m)

        # ⑥ 成功才清理（Spec 2.7）：清理失败不影响已合入产物，如实标注可单独重试
        code_r, sha = _git("rev-parse", "HEAD", cwd=dest)
        commit = sha.strip() if code_r == 0 else "(未知)"
        code_wr, out_wr = _git("worktree", "remove", str(wt), cwd=dest)
        code_bd, out_bd = _git("branch", "-d", branch, cwd=dest)
        notes = []
        if code_wr != 0:
            notes.append(f"worktree 清理失败：{out_wr}（可重试，产物已合入不受影响）")
        if code_bd != 0:
            notes.append(f"分支 {branch} 清理失败：{out_bd}（可重试，产物已合入不受影响）")
        detail = f"已合入 {cur}：commit {commit}"
        if notes:
            detail += "；" + "；".join(notes)
        return "merged", detail


def discard_worktree(wt: Path, repo_root: Path | None = None, branch: str | None = None) -> str:
    """丢弃：worktree remove --force + 删分支（-D），零残留。

    repo_root 缺省 WORKSPACE_ROOT；branch 缺省 agent/<wt.name> 反推——仅限
    无绑定旧任务路径，绑定路径必须显式传 Task.worktree_branch。返回错误串或空。
    """
    repo = WORKSPACE_ROOT if repo_root is None else repo_root
    if branch is None:
        branch = f"agent/{wt.name}"
    with WORKTREE_LOCK:
        code, out = _git("worktree", "remove", "--force", str(wt), cwd=repo)
        _git("branch", "-D", branch, cwd=repo)   # -D：未合并的分支也删（丢弃语义）
    return "" if code == 0 else f"worktree 清理失败：{out}"


def _worktree_repo_root(wt: Path) -> Path | None:
    """解析 worktree 的归属仓库根（多仓感知）：共享 git 目录的父目录。

    linked worktree 的 .git 是指回 <repo>/.git/worktrees/<name> 的文件，
    rev-parse --git-common-dir 给出共享的 <repo>/.git——父目录即仓库根，
    worktree remove / branch -D 要在那里跑（wt 目录删完后自己就没了，
    而残留沙箱可能属于任一绑定仓库，不能默认碰 Facta 主仓）。
    解析不出（元数据已坏）返回 None，调用方走 rmtree 兜底。
    """
    code, out = _git("rev-parse", "--git-common-dir", cwd=wt)
    if code != 0 or not out.strip():
        return None
    common = Path(out.strip())
    if not common.is_absolute():
        common = (wt / common).resolve()
    return common.parent


def cleanup_stale_worktrees(keep: Collection[str] = ()) -> int:
    """启动回收：data/worktrees/ 下残留目录全部移除（进程被杀防线）。

    keep：要保留的目录名（线五①任务沙箱——长住生命周期归任务收口管理，
    启动回收不许碰：非终态任务续跑要复用，终态任务待显式合并/丢弃）。
    spawn 的短命沙箱没人认领，照旧全清。

    多仓感知（P0）：归属仓库按 _worktree_repo_root 解析，分支名按
    rev-parse --abbrev-ref HEAD 取（不再从目录名反推——绑定路径分支是
    任意 worktree_branch），branch -D 在归属仓库上执行。返回清理数。
    半截目录（git 元数据已坏，worktree remove 失败）直接 rmtree 兜底——
    启动路径的清理不因单个坏目录中断。
    """
    if not WORKTREES_DIR.exists():
        return 0
    count = 0
    import shutil
    for wt in WORKTREES_DIR.iterdir():
        if not wt.is_dir() or wt.name in keep:
            continue
        repo = _worktree_repo_root(wt)
        branch = ""
        if repo is not None:
            code_b, out_b = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt)
            if code_b == 0 and out_b.strip() and out_b.strip() != "HEAD":
                branch = out_b.strip()
        code, _ = _git("worktree", "remove", "--force", str(wt), cwd=repo or wt)
        if code != 0:
            shutil.rmtree(wt, ignore_errors=True)
        if repo is not None and branch:
            _git("branch", "-D", branch, cwd=repo)
        count += 1
    return count
