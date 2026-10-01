"""GitHub 非交互 worker —— 让 facta 以 bot 身份处理 issue / 审查 PR。

触发入口是 GitHub Actions（见 .github/workflows/agent.yml），本模块只做三件事：
  1. 从环境变量读任务参数（BOT_TASK / ISSUE_NUMBER / PR_NUMBER / …）
  2. 用内核公开接口（assemble / run_turn）跑一轮 ReAct —— 与 __main__、server worker 同层
  3. 把 agent 的改动收成 git 提交 / PR，或把审查意见发成 PR review

设计约定：
  - 不碰内核：三条缝（on_text 流式 / on_event 语义事件 / on_confirm 确认）全部复用
  - 高危操作默认拒绝（on_confirm 恒 False）——bot 没有「批准」的概念，只有人批准
  - git 分支/提交/PR 由本文件执行，不信模型自报 —— 模型只负责改文件和跑测试
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time

from facta.github_bot.prompts import render_task
from facta.memory.store import Session
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import assemble, settle_session
from facta.orchestrator.loop import RunResult, run_turn

# ---------------------------------------------------------------- 环境契约
# BOT_TASK: "fix-issue" | "review-pr"
# ISSUE_NUMBER / PR_NUMBER: 二选一
# FACTA_PROVIDER: 模型档，默认 deepseek-flash（与 __main__.py 默认一致）
# BOT_TOKEN: 细粒度 PAT（推分支/开 PR 会触发 CI；GITHUB_TOKEN 的 push 不触发工作流，
#            见 docs/github-bot.md「为什么需要 PAT」）。缺省退回 GITHUB_TOKEN。
# BOT_DRY_RUN=1: 只跑 agent + 打印结果，不做任何 git/网络写操作（本地调试）

DRY_RUN = os.environ.get("BOT_DRY_RUN") == "1"
PROGRESS_INTERVAL = 15.0  # 进度评论节流秒数
TRACE_TAIL = 4000         # 收尾评论里带上的模型输出/事件尾巴长度

# 记忆子系统的数据目录（data/graph.json、data/memory/sessions/、data/learned/）：
# agent 运行本身就会产生副作用写（会话落盘、图谱同步、固化），与任务无关。
# 状态判断和提交都必须排除它们——否则 git add -A 会把副作用扫进 PR（issue #2 实录）。
GIT_EXCLUDES = [":(exclude)data/"]

# 提交闸门（issue #15 ②）：agent 干活时会自己造临时文件（补丁脚本、探针脚本……），
# 它自述「用完即删」但可能没删（PR #14 的 patch_013.py 实录）。git add -A 会把它们
# 一并扫进 PR。改为白名单提交：只有预期路径进 commit，其余列进 PR 正文供人工核对。
COMMIT_ALLOW_PREFIXES = ("src/", "tests/", "docs/", "evals/")
COMMIT_ALLOW_FILES = frozenset({"pyproject.toml", "README.md"})


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise SystemExit(f"[facta-bot] 缺少环境变量 {name}")
    return v


REPO = env("GITHUB_REPOSITORY", "local/dry-run")
TOKEN = env("BOT_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
TASK = env("BOT_TASK", "fix-issue")
ISSUE = os.environ.get("ISSUE_NUMBER", "")
PR = os.environ.get("PR_NUMBER", "")
PROVIDER = os.environ.get("FACTA_PROVIDER", "deepseek-flash")
# 工具轮数预算：内核默认 5 是「人还在场、下轮继续」的交互价；bot 要单发完成
# 「勘查→改动→跑测试」全程，默认放宽到 15（issue #2 首轮就烧光预算没写成）。
# 失控兜底仍在：原地踏步熔断 + workflow 30 分钟超时。
BOT_MAX_ROUNDS = int(os.environ.get("BOT_MAX_ROUNDS", "15"))


# ---------------------------------------------------------------- 小工具
def _run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, input=input_text, check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} 失败: {p.stderr.strip()[:500]}")
    return p.stdout


def gh(*args: str, input_text: str | None = None, check: bool = True) -> str:
    full = ["gh", *args]
    envs = dict(os.environ)
    if TOKEN:
        envs["GH_TOKEN"] = TOKEN
    p = subprocess.run(full, capture_output=True, text=True, input=input_text, env=envs, check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} 失败: {p.stderr.strip()[:500]}")
    return p.stdout


def gh_json(api_path: str) -> dict:
    return json.loads(gh("api", f"repos/{REPO}/{api_path}"))



# ---------------------------------------------------------------- 进度评论
class Progress:
    """一条 issue/PR 评论的流式更新（复用 streaming 缝：on_text/on_event 都喂进来）。"""

    def __init__(self, number: str) -> None:
        self.number = number
        self.comment_id: str | None = None
        self.last_flush = 0.0
        self.lines: list[str] = []      # 事件流水（保留最近 30 条）
        self.reply_parts: list[str] = []  # 模型流式输出全文

    @property
    def reply(self) -> str:
        return "".join(self.reply_parts)

    def post(self, body: str) -> None:
        if DRY_RUN or not self.number:
            print(f"\n[dry-run 评论]\n{body}\n")
            return
        if self.comment_id is None:
            out = gh("api", f"repos/{REPO}/issues/{self.number}/comments",
                     "-f", f"body={body}")
            self.comment_id = json.loads(out)["id"]
        else:
            gh("api", "--method", "PATCH",
               f"repos/{REPO}/issues/comments/{self.comment_id}", "-f", f"body={body}")
        self.last_flush = time.monotonic()

    def event(self, text: str) -> None:
        self.lines.append(text)
        self.lines = self.lines[-30:]
        if time.monotonic() - self.last_flush > PROGRESS_INTERVAL:
            self.flush()

    def on_text(self, chunk: str) -> None:
        self.reply_parts.append(chunk)

    def on_event(self, event_type: str, data: dict) -> None:
        if event_type == "tool_started":
            self.event(f"🔧 调用工具 `{data.get('name')}`")
        elif event_type == "plan.created":
            self.event("📋 已建立计划")
        elif event_type == "max_rounds":
            self.event("⚠️ 达到轮数上限，强制收尾")
        elif event_type == "stuck":
            self.event(f"⚠️ 检测到重复调用，已停止（{data.get('tools')}）")
        elif event_type == "error":
            self.event(f"❌ 模型不可用：{data.get('message')}")

    def flush(self) -> None:
        tail = "\n".join(self.lines)
        body = (
            f"🤖 facta 正在处理（task={TASK}）\n\n"
            f"**进展**\n{tail or '（思考中…）'}\n\n"
            f"<details><summary>模型输出（尾部）</summary>\n\n```\n"
            f"{self.reply[-TRACE_TAIL:]}\n```\n</details>"
        )
        self.post(body)


# ---------------------------------------------------------------- 主流程
def _session_with_persona(ctx, persona: str | None) -> tuple[Session, Agent]:
    """新建一段空会话并种人设法——与 __main__ 同款姿势，换的是可选人设。"""
    sid = ctx.store.create(Session())
    session = ctx.store.load(sid)
    agent = ctx.build_agent(session)
    # Agent 是 frozen dataclass（「frozen=配置不是状态」）：放宽预算走 replace 换新实例，
    # 内核默认值不动（spawn.py 注预算也是构造期注入，同款姿势）
    agent = dataclasses.replace(agent, max_tool_rounds=BOT_MAX_ROUNDS)
    if persona:
        agent.system_prompt = persona  # 外设层覆盖人设：不动工厂默认
    from facta.core.types import Message  # 延迟导入：与 cli.py 同款的底片播种
    if not session.messages:
        session.messages.append(Message(role="system", content=agent.system_prompt))
    return session, agent


def _auto_deny(name: str, args: dict) -> bool:
    print(f"[facta-bot] 高危操作已自动拒绝: {name} {args}")
    return False  # bot 不批准任何事；被拒后 run_turn 会把拒绝回灌给模型


def _verify_ci_trio() -> tuple[str, str, str, list[str]]:
    """CI 同口径复验（issue #15 ①）：提示词要求的三门由 worker 兜底复跑。

    返回 (ruff 输出, mypy 输出, pytest 输出, 未通过的科目列表)——任何一门红了
    都如实写进 PR 正文：「bot 认为验证过了」必须等于「CI 认为通过」。
    """
    lint_out = _run(["ruff", "check", "src/", "evals/", "tests/"], check=False)
    type_out = _run(["mypy"], check=False)
    test_out = _run(["python", "-m", "pytest", "tests/", "-x", "--timeout", "120"],
                    check=False)
    bad = []
    if "All checks passed" not in lint_out:  # ruff 全绿会打印这行，不能按空输出判
        bad.append("ruff")
    if "error:" in type_out:
        bad.append("mypy")
    if "failed" in test_out or "error" in test_out:
        bad.append("pytest")
    return lint_out, type_out, test_out, bad


def _stage_whitelist() -> tuple[list[str], list[str]]:
    """提交闸门（issue #15 ②）：白名单路径才进 commit。

    agent 干活时会自造临时文件（补丁脚本、探针脚本……），它自述「用完即删」但
    可能没删（PR #14 的 patch_013.py 实录）。返回 (待提交, 被拦截) 两组路径，
    拦截组不进 commit、列进 PR 正文供人工核对。
    """
    staged, blocked = [], []
    status = _run(["git", "status", "--porcelain", "--", ".", *GIT_EXCLUDES])
    for line in status.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')  # 兼容 rename 的 old -> new
        if path.startswith(COMMIT_ALLOW_PREFIXES) or path in COMMIT_ALLOW_FILES:
            staged.append(path)
        else:
            blocked.append(path)
    return staged, blocked


def run_fix_issue(progress: Progress) -> int:
    issue = gh_json(f"issues/{ISSUE}")
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    comments = gh_json(f"issues/{ISSUE}/comments")
    comment_text = "\n\n".join(
        f"@{c['user']['login']}: {c['body']}" for c in comments[-10:]
    )

    branch = f"bot/issue-{ISSUE}"
    if not DRY_RUN:
        _run(["git", "config", "user.name", "facta-bot"])
        _run(["git", "config", "user.email", "facta-bot@users.noreply.github.com"])
        _run(["git", "checkout", "-b", branch])
    print(f"[facta-bot] 分支 {branch}（dry_run={DRY_RUN}）")

    ctx = assemble(PROVIDER)
    session, agent = _session_with_persona(ctx, None)
    prompt = render_task("fix-issue", {
        "number": ISSUE, "title": title, "body": body, "comments": comment_text,
    })

    progress.post(f"🤖 facta 开工：issue #{ISSUE}「{title}」")
    try:
        result, reply = run_turn(
            session, prompt,
            agent=agent, llm=ctx.llm, summarizer=ctx.internal_llm,
            on_text=progress.on_text,
            on_event=progress.on_event,
            on_confirm=_auto_deny,
        )
    finally:
        print(ctx.ledger.bill())  # M7.5 账单纪律：bot 跑也要报账
        sid = ctx.store.latest()
        if sid:
            print(settle_session(session, sid, ctx.store, ctx.internal_llm, flush=True))

    if result is not RunResult.COMPLETED or not reply:
        progress.post(f"❌ 本轮没有完成（result={result}），未产生提交。\n\n"
                      f"<details><summary>输出</summary>\n\n```\n{progress.reply[-TRACE_TAIL:]}\n```\n</details>")
        return 1

    if DRY_RUN:
        print("[facta-bot] dry-run：跳过提交/PR")
        print(progress.reply[-TRACE_TAIL:])
        return 0

    if not _run(["git", "status", "--porcelain", "--", ".", *GIT_EXCLUDES]).strip():
        progress.post("✅ 调查完毕，但 agent 判断无需修改代码，未产生提交。\n\n" + progress.reply[-2000:])
        return 0

    lint_out, type_out, test_out, verify_bad = _verify_ci_trio()
    if verify_bad:
        progress.post(f"⚠️ agent 已改完但复验未全绿（{', '.join(verify_bad)}），"
                      f"先把现状推上来供人工接手。\n\n"
                      f"```\n{(lint_out + type_out + test_out)[-2000:]}\n```")

    staged, blocked = _stage_whitelist()
    if not staged:
        progress.post("✅ 调查完毕，但改动均在提交白名单之外，未产生提交。\n\n"
                      + progress.reply[-2000:])
        return 0
    _run(["git", "add", "--", *staged])
    _run(["git", "commit", "-m", f"bot: fix issue #{ISSUE} - {title[:60]}"])
    _run(["git", "push", "-u", "origin", branch])
    blocked_note = ""
    if blocked:
        blocked_note = ("\n\n## ⚠️ 提交闸门拦截的文件（未进本 PR，请人工核对后清理）\n\n"
                        + "\n".join(f"- `{p}`" for p in blocked))
    pr_url = gh("pr", "create", "--title", f"bot: fix #{ISSUE} {title[:60]}",
                "--body-file", "-", input_text=(
                    f"Fixes #{ISSUE}\n\n## agent 调查与修改说明\n\n{progress.reply[-TRACE_TAIL:]}\n\n"
                    f"## 复验结果（worker 兜底复跑，与 CI 同口径）\n\n"
                    f"### ruff\n```\n{lint_out[-1000:] or 'clean'}\n```\n"
                    f"### mypy\n```\n{type_out[-1000:]}\n```\n"
                    f"### pytest\n```\n{test_out[-2000:]}\n```\n"
                    f"{blocked_note}\n\n"
                    f"> 由 facta-bot 生成，合并前请人工 review。"
                )).strip()
    progress.post(f"✅ PR 已创建：{pr_url}")
    for client in ctx.mcp_clients:
        client.close()
    return 0


def run_review_pr(progress: Progress) -> int:
    pr = gh_json(f"pulls/{PR}")
    title = pr.get("title", "")
    body = pr.get("body", "") or ""
    diff = gh("pr", "diff", PR)

    if not DRY_RUN:
        _run(["git", "config", "user.name", "facta-bot"])
        _run(["git", "config", "user.email", "facta-bot@users.noreply.github.com"])
        gh("pr", "checkout", PR)

    ctx = assemble(PROVIDER)
    session, agent = _session_with_persona(ctx, None)
    prompt = render_task("review-pr", {
        "number": PR, "title": title, "body": body,
        "diff": diff[-30000:],  # 超长 diff 截尾：prompt 预算防线
    })

    progress.post(f"🤖 facta 开始审查 PR #{PR}「{title}」")
    try:
        result, reply = run_turn(
            session, prompt,
            agent=agent, llm=ctx.llm, summarizer=ctx.internal_llm,
            on_text=progress.on_text,
            on_event=progress.on_event,
            on_confirm=_auto_deny,
        )
    finally:
        print(ctx.ledger.bill())
        sid = ctx.store.latest()
        if sid:
            print(settle_session(session, sid, ctx.store, ctx.internal_llm, flush=True))

    review_body = (reply.content if reply is not None else "") or progress.reply[-TRACE_TAIL:]
    if DRY_RUN:
        print(review_body)
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(review_body)
        path = f.name
    gh("pr", "review", PR, "--comment", "-F", path)
    progress.post(f"✅ 审查意见已发到 PR #{PR}")
    for client in ctx.mcp_clients:
        client.close()
    return 0


def main() -> int:
    number = ISSUE or PR
    progress = Progress(number)
    if TASK not in ("fix-issue", "review-pr"):
        raise SystemExit(f"[facta-bot] 未知 BOT_TASK: {TASK}")
    try:
        if TASK == "fix-issue":
            return run_fix_issue(progress)
        return run_review_pr(progress)
    except Exception as e:  # noqa: BLE001 —— bot 层要兜住所有异常并回显到 issue
        with contextlib.suppress(Exception):
            progress.post(f"❌ facta-bot 运行失败：`{e}`")
        raise


if __name__ == "__main__":
    sys.exit(main())
