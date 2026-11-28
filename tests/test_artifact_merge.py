"""P0 Spec 2 产物保留与安全合入：worktree 原语层验收（M1-M7 + 原语层补充）。

不变量：
- 产物识别覆盖四段（已提交未合入/已暂存/未暂存/未跟踪）——「已提交但工作区
  干净」不得判为无产物（P0-2 根因回归：曾被当空沙箱 branch -D 丢提交）
- 合入四态 merged/empty/conflict/failed；冲突 abort 保主树干净，冲突/失败都
  保留 worktree 与候选分支可抢救（P0-3 根因回归：曾撞冲突即 remove worktree、
  主树留 MERGE_HEAD 半合并态）
- 合入前置：目标分支不匹配 / 工作区脏 / 合并中 → 拒绝且目标零改动
- 按需 commit：已提交产物不补空提交（nothing-to-commit 防御）

M8-M12（settle 幂等/验证关联/清理重试/重启恢复）落 app 层归后续拆单；
M13 在此只写原语层的 merge-base 回落部分。测试在真 git 仓库上跑（tmp
init + commit）——worktree 原语是 git 封装，mock git 无意义（协议面就是
命令行）。
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facta.core.llm import ScriptedLLM
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.memory.store import Session, SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext, ensure_persona
from facta.orchestrator.task import TASK_DONE, TASK_FAILED
from facta.paths import WORKSPACE_ROOT
from facta.server.app import create_app
from facta.tools.registry import ToolRegistry
from facta.tools.worktree import (
    commit_and_merge_back,
    create_worktree,
    worktree_changes,
)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """真 git 仓库（tmp_path/repo）+ 一份基线提交；把 worktree 模块的根指过来。

    仓库与 WORKTREES_DIR 分家（tmp_path/worktrees，沙箱住仓库外）：合入
    前置要求目标工作区干净，沙箱目录住进仓库会顶着 untracked 噪声把一切
    合入挡在前置门外——生产拓扑同款（沙箱住 Facta data/，用户仓库另在别处）。
    patch 目标是 facta.tools.worktree（from-import 后名字绑定在消费模块，
    只 patch paths 不生效——S6a 实踩纪律）。
    """
    from facta.tools import worktree as wt_mod

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.setattr(wt_mod, "WORKSPACE_ROOT", repo_dir)
    monkeypatch.setattr(wt_mod, "WORKTREES_DIR", tmp_path / "worktrees")
    (repo_dir / "base.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo_dir,
        check=True,
    )
    return repo_dir


def _run(cwd: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _out(cwd: Path, *args: str) -> str:
    code, out = _run(cwd, *args)
    assert code == 0, f"git {' '.join(args)} 失败：{out}"
    return out


def _commit_in(wt: Path, message: str) -> None:
    _out(wt, "add", "-A")
    _out(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)


# ---------- M1-M4：四段产物识别与收口合入 ----------


def test_m1_committed_only_detected_and_merged(repo):
    # M1 已提交且工作区干净：①段识别出产物，收口不丢提交，合入后产物在目标分支
    baseline = _out(repo, "rev-parse", "HEAD")
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m1", baseline=baseline)
    assert err == ""
    (wt / "feat.py").write_text("y = 1\n", encoding="utf-8")
    _commit_in(wt, "feat: 已提交产物")
    assert _out(wt, "status", "--porcelain") == ""   # 前提：工作区干净，产物全在①段

    cs = worktree_changes(wt, baseline, target)
    assert cs.any() and bool(cs)                     # ①段识别出产物（__bool__ = any）
    assert not cs.error
    assert not cs.staged and not cs.unstaged and not cs.untracked
    assert "feat.py" in cs.committed
    assert "已提交未合入" in cs.summary()
    assert not cs.baseline_inferred                  # 绑定 baseline 给定，非推断

    status, detail = commit_and_merge_back(
        wt, "merge-back", merge_into=repo, branch="agent/m1",
        target_branch=target, baseline=baseline,
    )
    assert status == "merged"
    assert (repo / "feat.py").read_text(encoding="utf-8") == "y = 1\n"
    # 收口不丢提交：候选提交真实落在目标分支历史里
    assert "feat: 已提交产物" in _out(repo, "log", "--oneline")
    assert not wt.exists()                           # 成功才清理
    assert _out(repo, "branch", "--list", "agent/m1") == ""


def test_m2_staged_detected_and_merged(repo):
    # M2 已暂存：②段识别非空，收口合入含该改动
    baseline = _out(repo, "rev-parse", "HEAD")
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m2", baseline=baseline)
    assert err == ""
    (wt / "base.py").write_text("x = 2\n", encoding="utf-8")
    _out(wt, "add", "base.py")                       # 只暂存不提交

    cs = worktree_changes(wt, baseline, target)
    assert "base.py" in cs.staged
    assert not cs.committed and not cs.unstaged and not cs.untracked
    assert "已暂存" in cs.summary()

    status, _ = commit_and_merge_back(
        wt, "merge-back", merge_into=repo, branch="agent/m2",
        target_branch=target, baseline=baseline,
    )
    assert status == "merged"
    assert (repo / "base.py").read_text(encoding="utf-8") == "x = 2\n"


def test_m3_unstaged_detected_and_merged(repo):
    # M3 未暂存：③段识别非空，收口合入含该改动
    baseline = _out(repo, "rev-parse", "HEAD")
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m3", baseline=baseline)
    assert err == ""
    (wt / "base.py").write_text("x = 3\n", encoding="utf-8")   # 不 add

    cs = worktree_changes(wt, baseline, target)
    assert "base.py" in cs.unstaged
    assert not cs.committed and not cs.staged and not cs.untracked
    assert "未暂存" in cs.summary()

    status, _ = commit_and_merge_back(
        wt, "merge-back", merge_into=repo, branch="agent/m3",
        target_branch=target, baseline=baseline,
    )
    assert status == "merged"
    assert (repo / "base.py").read_text(encoding="utf-8") == "x = 3\n"


def test_m4_untracked_detected_and_merged(repo):
    # M4 未跟踪：④段识别非空，收口合入含该文件
    baseline = _out(repo, "rev-parse", "HEAD")
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m4", baseline=baseline)
    assert err == ""
    (wt / "new.py").write_text("y = 1\n", encoding="utf-8")   # 未跟踪

    cs = worktree_changes(wt, baseline, target)
    assert "new.py" in cs.untracked
    assert not cs.committed and not cs.staged and not cs.unstaged
    assert "未跟踪" in cs.summary()

    status, _ = commit_and_merge_back(
        wt, "merge-back", merge_into=repo, branch="agent/m4",
        target_branch=target, baseline=baseline,
    )
    assert status == "merged"
    assert (repo / "new.py").read_text(encoding="utf-8") == "y = 1\n"


# ---------- M5-M7：合入四态与前置拒绝 ----------


def test_m5_merge_conflict_keeps_candidate(repo):
    # M5 merge 冲突：conflict 态 + 冲突文件清单，主树无 MERGE_HEAD 且干净，
    # worktree 与候选分支保留可重试
    baseline = _out(repo, "rev-parse", "HEAD")
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m5", baseline=baseline)
    assert err == ""
    (wt / "base.py").write_text("x = 2\n", encoding="utf-8")
    _commit_in(wt, "feat: wt 改 base")
    # 目标分支前进且改同一文件 → merge 必冲突
    (repo / "base.py").write_text("x = 3\n", encoding="utf-8")
    _commit_in(repo, "main: 目标改 base")

    status, detail = commit_and_merge_back(
        wt, "merge-back", merge_into=repo, branch="agent/m5",
        target_branch=target, baseline=baseline,
    )
    assert status == "conflict"
    assert "base.py" in detail                        # 冲突文件清单
    # 主树无 MERGE_HEAD 且干净（abort 恢复），目标内容零变化
    code, _ = _run(repo, "rev-parse", "--verify", "-q", "MERGE_HEAD")
    assert code != 0
    assert _out(repo, "status", "--porcelain") == ""
    assert (repo / "base.py").read_text(encoding="utf-8") == "x = 3\n"
    # worktree 与候选分支保留，候选未丢
    assert wt.exists()
    assert _out(repo, "branch", "--list", "agent/m5") != ""
    assert "feat: wt 改 base" in _out(wt, "log", "--oneline")
    # 可重试：目标回到基线后同一候选直接合入成功
    _out(repo, "reset", "--hard", baseline)
    status2, _ = commit_and_merge_back(
        wt, "merge-back 重试", merge_into=repo, branch="agent/m5",
        target_branch=target, baseline=baseline,
    )
    assert status2 == "merged"
    assert (repo / "base.py").read_text(encoding="utf-8") == "x = 2\n"


def test_m6_dirty_target_rejected(repo):
    # M6 目标工作区脏：前置拒绝（明确报错列出脏文件），目标状态零变化，候选保留
    baseline = _out(repo, "rev-parse", "HEAD")
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m6", baseline=baseline)
    assert err == ""
    (wt / "feat.py").write_text("y = 1\n", encoding="utf-8")
    _commit_in(wt, "feat: 候选")
    (repo / "base.py").write_text("x = 999\n", encoding="utf-8")   # 目标脏（未提交）

    status, detail = commit_and_merge_back(
        wt, "merge-back", merge_into=repo, branch="agent/m6",
        target_branch=target, baseline=baseline,
    )
    assert status == "failed"
    assert "base.py" in detail                        # 脏文件清单
    # 目标状态零变化：脏改动原样、候选没进目标
    assert (repo / "base.py").read_text(encoding="utf-8") == "x = 999\n"
    assert _out(repo, "status", "--porcelain") != ""
    assert not (repo / "feat.py").exists()
    # 候选保留
    assert wt.exists()
    assert _out(repo, "branch", "--list", "agent/m6") != ""


def test_m7_merging_target_rejected(repo):
    # M7 目标合并中（有 MERGE_HEAD）：前置拒绝，目标半合并态零变化，候选保留
    baseline = _out(repo, "rev-parse", "HEAD")
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m7", baseline=baseline)
    assert err == ""
    (wt / "feat.py").write_text("y = 1\n", encoding="utf-8")
    _commit_in(wt, "feat: 候选")
    # 目标进入「合并中」：--no-commit 合入候选（无冲突、停在提交前），
    # MERGE_HEAD 挂起——真实半合并态，不是伪造元数据
    _run(repo, "merge", "--no-commit", "--no-ff", "agent/m7")
    code, _ = _run(repo, "rev-parse", "--verify", "-q", "MERGE_HEAD")
    assert code == 0                                   # 前提：目标确实合并中
    before = _out(repo, "status", "--porcelain")

    status, detail = commit_and_merge_back(
        wt, "merge-back", merge_into=repo, branch="agent/m7",
        target_branch=target, baseline=baseline,
    )
    assert status == "failed"
    assert "合并中" in detail
    # 目标状态零变化：半合并态原样留给用户裁决
    code, _ = _run(repo, "rev-parse", "--verify", "-q", "MERGE_HEAD")
    assert code == 0
    assert _out(repo, "status", "--porcelain") == before
    # 候选保留
    assert wt.exists()
    assert _out(repo, "branch", "--list", "agent/m7") != ""
    _run(repo, "merge", "--abort")


# ---------- 原语层补充：M13 回落 / empty 态 / 按需 commit ----------


def test_m13_baseline_inferred_fallback(repo):
    # M13 原语层部分：无 baseline 旧任务回落 merge-base，标注「基线为推断值」
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m13")   # 无基线：从当前 HEAD 切
    assert err == ""
    (wt / "feat.py").write_text("y = 2\n", encoding="utf-8")
    _commit_in(wt, "feat: 旧任务产物")

    cs = worktree_changes(wt, "", target)   # baseline 空 + target_branch 给定 → 回落
    assert cs.baseline_inferred
    assert "feat.py" in cs.committed
    assert "基线为推断值" in cs.summary()
    # 旧调用语义保持：baseline/target 都空 → ①段恒空
    legacy = worktree_changes(wt)
    assert not legacy.committed and not legacy.baseline_inferred


def test_empty_sandbox_discarded(repo):
    # 真·空沙箱：四段全空 → empty 态 discard，不留目录不留分支
    wt, err = create_worktree(repo_root=repo, branch="agent/empty")
    assert err == ""
    assert not worktree_changes(wt)          # 空沙箱 falsy（旧 if not changes 语义）
    status, _ = commit_and_merge_back(wt, "空", merge_into=repo, branch="agent/empty")
    assert status == "empty"
    assert not wt.exists()
    assert _out(repo, "branch", "--list", "agent/empty") == ""
    assert _out(repo, "status", "--porcelain") == ""


def test_no_empty_commit_for_committed_artifacts(repo):
    # 按需 commit：仅已提交产物时收口不补空提交（nothing-to-commit 防御）
    baseline = _out(repo, "rev-parse", "HEAD")
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/noec", baseline=baseline)
    assert err == ""
    (wt / "feat.py").write_text("y = 1\n", encoding="utf-8")
    _commit_in(wt, "feat: 只带已提交产物")

    status, _ = commit_and_merge_back(
        wt, "merge-back", merge_into=repo, branch="agent/noec",
        target_branch=target, baseline=baseline,
    )
    assert status == "merged"
    log = _out(repo, "log", "--oneline")
    assert "feat: 只带已提交产物" in log
    assert "merge-back" not in log                      # 收口消息没落成空提交
    assert _out(repo, "rev-list", "--count", f"{baseline}..HEAD") == "1"   # ff 即达


# ---------- app 层辅助（M8-M13、E1-E3）----------


def _task_ctx(llm) -> AppContext:
    """最小 AppContext（test_worktree._task_ctx 同款）：registry 恒裸，沙箱重锚自带五件。"""
    tools = ToolRegistry()

    def build_agent(session: Session, repo_root=None) -> Agent:
        agent = Agent(name="test", system_prompt="测试人设", registry=tools)
        ensure_persona(session, agent)
        return agent

    return AppContext(
        provider="mock",
        ledger=UsageLedger(),
        embedder=None,
        llm=llm,
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(Path(tempfile.mkdtemp()) / "sessions"),
        build_agent=build_agent,
        todos=TodoStore(Path(tempfile.mkdtemp()) / "todos.json"),
    )


def _live_client(llm=None) -> TestClient:
    return TestClient(create_app(_task_ctx(llm or ScriptedLLM([]))))


def _call(name: str, args: dict) -> dict:
    return {"id": uuid.uuid4().hex[:8], "name": name, "arguments": json.dumps(args)}


def _write_llm(path: str = "feat.py", content: str = "print('feat')\n") -> ScriptedLLM:
    return ScriptedLLM([
        Message(
            role="assistant",
            content="",
            tool_calls=[_call("write_file", {"path": path, "content": content})],
        ),
        Message(role="assistant", content="完成"),
    ])


def _wait_task(client: TestClient, tid: str, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/tasks/{tid}").json()["status"]
        if status in (TASK_DONE, TASK_FAILED):
            return status
        time.sleep(0.05)
    return ""


def _start_task(llm, repo: Path, *, goal: str = "干活"):
    client = _live_client(llm)
    tid = client.post(
        "/api/tasks", json={"goal": goal, "repo_root": str(repo)}
    ).json()["task_id"]
    return client, tid, _wait_task(client, tid)


def _make_repo(path: Path, base: str = "base.py") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / base).write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=path,
        check=True,
    )
    return path


# ---------- M8-M12：settle 幂等 / 候选预览 / 验证如实 / 清理重试 / 重启重放 ----------


def test_m8_repeat_merge_applies_once(repo):
    client, tid, status = _start_task(_write_llm(), repo)
    assert status == TASK_DONE
    d1 = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"}).json()
    assert d1["status"] == "merged" and d1["cleared"] is True
    count = _out(repo, "rev-list", "--count", "HEAD")
    assert (repo / "feat.py").exists()

    # 重复点击/重发：返回原处置结果，不重复执行
    d2 = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"}).json()
    assert d2["status"] == "merged" and d2["result"] == d1["result"]
    assert d2["detail"] == d1["detail"] and d2["cleared"] is True
    assert _out(repo, "rev-list", "--count", "HEAD") == count   # 目标分支只变一次
    assert client.get(f"/api/tasks/{tid}").json()["merge_status"] == "merged"


def test_m9_candidate_changed_preview_stale(repo):
    llm = ScriptedLLM([
        Message(
            role="assistant", content="",
            tool_calls=[_call("run_command", {"command": "echo ok"})],
        ),
        Message(
            role="assistant", content="",
            tool_calls=[_call("write_file", {"path": "x.py", "content": "1\n"})],
        ),
        Message(role="assistant", content="完成"),
    ])
    client, tid, status = _start_task(llm, repo)
    assert status == TASK_DONE
    d = client.get(f"/api/tasks/{tid}/worktree").json()
    assert d["verifications"] and d["verifications"][-1]["result"] == "passed"

    # 预览① 请求带过期候选 → 409 要求重新核验
    r = client.post(
        f"/api/tasks/{tid}/worktree", json={"action": "merge", "candidate": "stale-stale"}
    )
    assert r.status_code == 409 and "重新核验" in r.json()["detail"]
    # 预览② 验证后改动发生变化（最近验证候选 ≠ 当前候选）→ 同样 409
    r2 = client.post(
        f"/api/tasks/{tid}/worktree", json={"action": "merge", "candidate": d["candidate"]}
    )
    assert r2.status_code == 409 and "重新核验" in r2.json()["detail"]
    assert client.get(f"/api/tasks/{tid}").json()["merge_status"] == "none"
    assert "feat" not in _out(repo, "log", "--oneline")   # 未合入不动目标分支


def test_m10_verification_honest_failed_not_run(repo, monkeypatch):
    monkeypatch.setattr("facta.tools.terminal.TIMEOUT_SECONDS", 1)   # 跑不完的检查→not_run
    llm = ScriptedLLM([
        Message(
            role="assistant", content="",
            tool_calls=[_call("run_command", {"command": "grep zzz base.py"})],
        ),
        Message(
            role="assistant", content="",
            tool_calls=[_call("run_command", {"command": "tail -f base.py"})],
        ),
        Message(
            role="assistant", content="",
            tool_calls=[_call("write_file", {"path": "keep.py", "content": "1\n"})],
        ),
        Message(role="assistant", content="完成"),
    ])
    client, tid, status = _start_task(llm, repo)
    assert status == TASK_DONE
    d = client.get(f"/api/tasks/{tid}/worktree").json()
    assert [v["result"] for v in d["verifications"]] == ["failed", "not_run"]   # 不装绿
    assert [v["name"] for v in d["verifications"]] == ["grep zzz base.py", "tail -f base.py"]
    assert all(v["candidate"] for v in d["verifications"])   # 与候选版本绑定
    assert client.get(f"/api/tasks/{tid}").json()["verifications"] == d["verifications"]


def test_m11_cleanup_failure_separate_retry(repo, monkeypatch):
    from facta.tools import worktree as wt_mod

    client, tid, status = _start_task(_write_llm(), repo)
    assert status == TASK_DONE
    orig = wt_mod._git

    def flaky(*args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            return 1, "simulated cleanup failure"
        return orig(*args, **kwargs)

    monkeypatch.setattr(wt_mod, "_git", flaky)
    d1 = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"}).json()
    assert d1["status"] == "merged" and d1["cleared"] is False
    assert "清理失败" in d1["detail"]
    assert client.get(f"/api/tasks/{tid}").json()["merge_status"] == "merged"
    assert (repo / "feat.py").exists()                      # 产物已合入不受影响
    count = _out(repo, "rev-list", "--count", "HEAD")

    monkeypatch.setattr(wt_mod, "_git", orig)               # 清理可再次成功
    d2 = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"}).json()
    assert d2["status"] == "merged" and d2["cleared"] is True
    assert "清理重试成功" in d2["detail"]
    assert _out(repo, "rev-list", "--count", "HEAD") == count   # 仅重试清理，不重复 merge
    assert client.get(f"/api/tasks/{tid}").json()["worktree"] == ""


def test_m12_disposition_replay_only_applies_once(repo):
    client, tid, status = _start_task(_write_llm(), repo)
    assert status == TASK_DONE
    d1 = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"}).json()
    assert d1["status"] == "merged"
    count = _out(repo, "rev-list", "--count", "HEAD")
    disp_id = client.get(f"/api/tasks/{tid}").json()["disposition"]["id"]

    for _ in range(2):   # 重复点击/重复请求：同结果同处置，目标分支不动
        d2 = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"}).json()
        assert d2["status"] == "merged" and d2["result"] == d1["result"]
    assert _out(repo, "rev-list", "--count", "HEAD") == count
    assert client.get(f"/api/tasks/{tid}").json()["disposition"]["id"] == disp_id

    # merge/discard 互斥先到先得
    r = client.post(f"/api/tasks/{tid}/worktree", json={"action": "discard"})
    assert r.status_code == 409


def test_m13_app_legacy_baseline_inferred(repo, tmp_path):
    # app 层：无 baseline 旧任务读取兼容，产物判定回落 merge-base 并标注推断值
    target = _out(repo, "branch", "--show-current")
    wt, err = create_worktree(repo_root=repo, branch="agent/m13app")
    assert err == ""
    (wt / "feat.py").write_text("print('feat')\n", encoding="utf-8")
    _commit_in(wt, "feat: 旧任务产物")
    tid = uuid.uuid4().hex
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / f"{tid}.json").write_text(
        json.dumps({
            "task_id": tid,
            "goal": "旧任务",
            "status": "done",
            "worktree": str(wt),
            "worktree_branch": "agent/m13app",
            "repo_root": str(repo),
            "target_branch": target,
            "baseline": "",
        }),
        encoding="utf-8",
    )
    client = TestClient(create_app(_task_ctx(ScriptedLLM([]))))
    d = client.get(f"/api/tasks/{tid}/worktree").json()
    assert d["baseline_inferred"] is True
    assert "基线为推断值" in d["changes"]
    assert "feat.py" in d["segments"]["committed"]


# ---------- E1-E3：主验收场景全链 ----------


def test_e1_full_chain_fix_verify_merge(repo, monkeypatch):
    # 临时仓造失败测试（.gitignore 排除测试缓存：候选指纹不被噪声带偏）
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "app.py").write_text("VALUE = 1\n# TODO: should be 2\n", encoding="utf-8")
    (repo / "test_check.py").write_text(
        "from app import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    _commit_in(repo, "init: 带一个失败测试")
    # 白名单口径：pytest 二进制须解析到系统/项目 venv 目录——补 PATH 后无需确认闸门
    monkeypatch.setenv("PATH", f"{WORKSPACE_ROOT / '.venv' / 'bin'}:{os.environ['PATH']}")

    llm = ScriptedLLM([
        Message(
            role="assistant", content="",
            tool_calls=[_call("run_command", {"command": "pytest test_check.py"})],
        ),
        Message(
            role="assistant", content="",
            tool_calls=[_call("write_file", {"path": "app.py", "content": "VALUE = 2\n"})],
        ),
        Message(
            role="assistant", content="",
            tool_calls=[_call("run_command", {"command": "pytest test_check.py"})],
        ),
        Message(role="assistant", content="修复完成"),
    ])
    client, tid, status = _start_task(llm, repo, goal="修复失败测试")
    assert status == TASK_DONE

    # 任务卡核验：diff + 测试结果（如实：先 failed 后 passed）
    d = client.get(f"/api/tasks/{tid}/worktree").json()
    assert "app.py" in d["changes"]
    assert [v["result"] for v in d["verifications"]] == ["failed", "passed"]
    assert all(v["name"] == "pytest test_check.py" for v in d["verifications"])

    # 批准合入（候选标识原样带回）
    r = client.post(
        f"/api/tasks/{tid}/worktree", json={"action": "merge", "candidate": d["candidate"]}
    )
    assert r.json()["status"] == "merged" and r.json()["cleared"] is True

    # 目标分支含修复
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    target = _out(repo, "branch", "--show-current")
    assert "VALUE = 2" in _out(repo, "show", f"{target}:app.py")


def test_e2_two_repos_parallel_full_chain(tmp_path, monkeypatch):
    repo_a = _make_repo(tmp_path / "repo-a", "a_base.py")
    repo_b = _make_repo(tmp_path / "repo-b", "b_base.py")
    # 一 app 一任务根（部署形态如此）：双 app 共享任务根会互抢调度
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks-a")
    client_a = _live_client(_write_llm("only_a.py"))
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks-b")
    client_b = _live_client(_write_llm("only_b.py"))
    results: dict[str, object] = {}

    def scenario(key: str, client: TestClient, repo: Path, other: Path, mine: str) -> None:
        try:
            tid = client.post(
                "/api/tasks", json={"goal": "干活", "repo_root": str(repo)}
            ).json()["task_id"]
            status = _wait_task(client, tid)
            branch = client.get(f"/api/tasks/{tid}").json()["worktree_branch"]
            resp = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"})
            results[key] = (status, branch, resp.json(), mine, repo, other)
        except Exception as exc:  # noqa: BLE001 —— 线程里的失败要带出来
            results[key] = exc

    t1 = threading.Thread(target=scenario, args=("a", client_a, repo_a, repo_b, "only_a.py"))
    t2 = threading.Thread(target=scenario, args=("b", client_b, repo_b, repo_a, "only_b.py"))
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)

    for key in ("a", "b"):
        assert not isinstance(results[key], Exception), f"{key} 侧异常：{results[key]}"
    for key, mine, other_name in (("a", "only_a.py", "only_b.py"), ("b", "only_b.py", "only_a.py")):
        status, branch, settle, filename, repo, other = results[key]
        assert status == TASK_DONE and settle["status"] == "merged"
        assert (repo / mine).exists()                  # 写入/合入落点正确
        assert not (repo / other_name).exists()        # 父子写入不越界（文件证据）
        assert not (other / mine).exists()
        assert branch not in _out(repo, "branch", "--list")   # 合入后分支零残留


def test_e3_restart_replays_disposition(repo):
    client, tid, status = _start_task(_write_llm(), repo)
    assert status == TASK_DONE
    d1 = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"}).json()
    assert d1["status"] == "merged"
    count = _out(repo, "rev-list", "--count", "HEAD")

    # 重启：同任务根重建 app（处置已落盘），重放 settle 返回原处置、不重复合入
    client2 = _live_client(ScriptedLLM([]))
    d2 = client2.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"}).json()
    assert d2["status"] == "merged" and d2["result"] == d1["result"]
    assert d2["detail"] == d1["detail"] and d2["cleared"] is True
    assert _out(repo, "rev-list", "--count", "HEAD") == count

    view = client2.get(f"/api/tasks/{tid}").json()
    assert view["disposition"]["id"] == client.get(f"/api/tasks/{tid}").json()["disposition"]["id"]
    assert view["merge_status"] == "merged" and view["merge_commit"]

