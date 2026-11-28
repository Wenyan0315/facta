"""P0 任务工作区绑定（S1-1/S1-3/S1-5）：绑定三元组 + 任务沙箱接线 + 恢复路径。

覆盖面取 TestClient（create_app）路径：B1-B3 走 POST /api/tasks 全链
（resolve_binding → TaskStore.create → 落盘/响应），B12 走存量 JSON 读取 +
API 视图缺省值；B4-B6 走真 worker 全链（沙箱分支/提交落绑定仓库、双仓并行、
沙箱 registry 继承绑定）；B9-B11、B13 走恢复入口/绑定失效/目标前进/元数据
冲突（S1-5）。用例在真 git 仓库上跑（tmp init + commit）——协议面就是命令行。
"""

import json
import re
import shutil
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
from facta.orchestrator.task import TASK_BLOCKED, TASK_DONE, TASK_FAILED, Task, TaskStore
from facta.server import app as app_mod
from facta.server.app import create_app
from facta.tools import worktree as wt_mod
from facta.tools.context import WorkspaceBinding
from facta.tools.registry import ToolRegistry
from facta.tools.worktree import cleanup_stale_worktrees, create_worktree


def _git(repo: Path, *args: str) -> str:
    """真 git 命令（测试地基）：成功回 stdout，check=True 炸掉即用例失败。"""
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """真 git 仓库（初始提交落 main）——B1-B3 的地基。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def _task_ctx(llm) -> AppContext:
    """最小 AppContext（test_task_feed 同款形状）：registry 恒裸，沙箱重锚自带五件。"""
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


def _client(monkeypatch, sched_stub=None) -> TestClient:
    """创建入口不起 worker（sched_stub 可注入捕获器，B10 触发调度用）。"""
    monkeypatch.setattr(
        "facta.server.app._maybe_start_queued",
        sched_stub if sched_stub is not None else (lambda *a, **k: None),
    )
    return TestClient(create_app(_task_ctx(ScriptedLLM([]))))


def _live_client(llm=None) -> TestClient:
    """真调度的全链 client（worker 真跑）：B4-B6/B9-B11 用。"""
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


def _wait_task(client: TestClient, tid: str, timeout: float = 15.0) -> str:
    """轮询任务终态（TaskStore.on_save 即刻落盘，run 界终态再取一次兜底）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/tasks/{tid}").json()["status"]
        if status in (TASK_DONE, TASK_FAILED):
            return status
        time.sleep(0.05)
    return ""


def _make_repo(path: Path, base: str = "base.py") -> Path:
    """现做即弃的真 git 仓（fixture git_repo 的复用版，B5/E2 双仓用）。"""
    path.mkdir(parents=True, exist_ok=True)
    (path / base).write_text("base\n", encoding="utf-8")
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "-A")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return path


# ---- B1：repo_root 非 Git / 不存在 → 创建失败且原因明确（拒建任务）----


def test_b1_create_rejects_bad_repo_root(tmp_path, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/api/tasks", json={"goal": "g", "repo_root": str(tmp_path / "missing")})
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]

    plain = tmp_path / "plain"
    plain.mkdir()
    resp = client.post("/api/tasks", json={"goal": "g", "repo_root": str(plain)})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "不是 Git 仓库" in detail
    assert "fatal" in detail   # 含 git 原始错误信息，不是空泛拒绝

    # 拒建任务：失败不落成能跑的任务再事后补救
    assert client.get("/api/tasks").json() == []


# ---- B2：三元组记录正确；baseline 创建时钉死，不跟随分支头 ----


def test_b2_binding_recorded_and_baseline_pinned(git_repo, monkeypatch):
    client = _client(monkeypatch)
    sub = git_repo / "sub"
    sub.mkdir()
    baseline_sha = _git(git_repo, "rev-parse", "HEAD")

    # repo_root 传仓库子目录：归一化为 rev-parse --show-toplevel
    resp = client.post(
        "/api/tasks",
        json={"goal": "g", "repo_root": str(sub), "target_branch": "main"},
    )
    assert resp.status_code == 202
    tid = resp.json()["task_id"]

    d = client.get(f"/api/tasks/{tid}").json()
    assert d["repo_root"] == _git(git_repo, "rev-parse", "--show-toplevel")
    assert d["target_branch"] == "main"
    assert d["baseline"] == baseline_sha

    # 目标分支加提交：baseline 钉死在创建时，不跟分支头
    (git_repo / "b.txt").write_text("2\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "second")
    assert _git(git_repo, "rev-parse", "HEAD") != baseline_sha
    assert client.get(f"/api/tasks/{tid}").json()["baseline"] == baseline_sha


def test_b2_no_repo_root_means_default_workspace(tmp_path, monkeypatch):
    """规则 4：未选项目 → (None, "")，三元组留空 = 默认工作区（现状语义不变）。"""
    client = _client(monkeypatch)
    tid = client.post("/api/tasks", json={"goal": "g"}).json()["task_id"]

    d = client.get(f"/api/tasks/{tid}").json()
    assert d["repo_root"] == ""
    assert d["target_branch"] == ""
    assert d["baseline"] == ""


# ---- B3：target_branch 解析（缺省=当前检出分支；显式不存在 → 拒建）----


def test_b3_target_branch_resolution(git_repo, monkeypatch):
    client = _client(monkeypatch)
    _git(git_repo, "checkout", "-q", "-b", "topic")

    # 缺省：取绑定仓库当前检出分支
    tid = client.post("/api/tasks", json={"goal": "g", "repo_root": str(git_repo)}).json()["task_id"]
    d = client.get(f"/api/tasks/{tid}").json()
    assert d["target_branch"] == "topic"
    assert d["baseline"] == _git(git_repo, "rev-parse", "HEAD")

    # 显式不存在的分支 → 创建失败，原因指明缺失分支
    resp = client.post(
        "/api/tasks",
        json={"goal": "g", "repo_root": str(git_repo), "target_branch": "nope"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "nope" in detail and "不存在" in detail


# ---- B12：存量任务兼容（旧 JSON 无新字段）----


def test_b12_legacy_task_json_loads_with_defaults(tmp_path, monkeypatch):
    # 旧版 asdict 落盘形状：无 target_branch/baseline/worktree_branch/candidate/
    # verifications/merge_status/merge_commit/disposition 任何一键
    tid = uuid.uuid4().hex
    legacy = {
        "task_id": tid,
        "goal": "存量任务",
        "session_id": "20260101-000000",
        "repo_root": "/old/repo",
        "status": "done",
        "run_ids": ["r1"],
        "billing": {"runs": {"r1": {"llm_cost": 0.1}}},
        "continuations": 2,
        "started_at": 1.0,
        "cost_base": 0.0,
        "created_at": 1.0,
        "updated_at": 2.0,
        "cancel_requested": False,
        "fuse_fired": False,
        "blocked_reason": "",
        "worktree": "",
        "skill": "",
        "authorization": "readonly",
        "authorized_write": False,
        "next_text": "",
    }
    # conftest 隔离闸已把 TASKS_DIR 指进本用例 tmp_path
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / f"{tid}.json").write_text(json.dumps(legacy), encoding="utf-8")

    client = _client(monkeypatch)
    d = client.get(f"/api/tasks/{tid}").json()
    # 读取正常、响应缺省值不崩
    assert d["goal"] == "存量任务"
    assert d["status"] == "done"
    # 不补造绑定：三元组与收口字段全是缺省值，repo_root 原样保留
    assert d["repo_root"] == "/old/repo"
    assert d["target_branch"] == "" and d["baseline"] == "" and d["worktree_branch"] == ""
    assert d["candidate"] == "" and d["verifications"] == []
    assert d["merge_status"] == "none" and d["merge_commit"] == "" and d["disposition"] is None


# ---- B4：任务沙箱分支/提交进绑定仓库，Facta 主仓零污染 ----


def test_b4_sandbox_lands_in_bound_repo(git_repo, wt_git_repo):
    client = _live_client(_write_llm())
    facta_head = _git(wt_git_repo, "rev-parse", "HEAD")
    facta_branches = _git(wt_git_repo, "branch", "--list")

    tid = client.post(
        "/api/tasks", json={"goal": "干活", "repo_root": str(git_repo)}
    ).json()["task_id"]
    assert _wait_task(client, tid) == TASK_DONE

    view = client.get(f"/api/tasks/{tid}").json()
    branch = view["worktree_branch"]
    assert re.fullmatch(r"agent/[0-9a-f]{8}", branch)   # 显式生成存字段（S1-3）
    assert view["exec_mode"] == "sandbox"
    wt = Path(view["worktree"])

    # 分支与提交都进绑定仓库 B 的 refs
    assert branch in _git(git_repo, "branch", "--list", "agent/*")
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "feat")
    assert "feat" in _git(git_repo, "log", "--oneline", branch)

    # Facta 主仓零污染：无新分支无新提交
    assert _git(wt_git_repo, "rev-parse", "HEAD") == facta_head
    assert _git(wt_git_repo, "branch", "--list") == facta_branches

    # 收口合入落点 = 绑定 target_branch（默认仓头分支）
    resp = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"})
    assert resp.json()["status"] == "merged"
    assert (git_repo / "feat.py").exists()
    assert "feat" in _git(git_repo, "log", "--oneline", "main")


# ---- B5：双仓并行互不串（父侧：沙箱/分支/写入/合入落点各自正确）----


def test_b5_two_repos_parallel_no_cross(git_repo, tmp_path, monkeypatch):
    repo_b = _make_repo(tmp_path / "repo-b", "b_base.py")
    # 一 app 一任务根（部署形态如此）：双 app 共享任务根会互抢调度
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks-a")
    client_a = _live_client(_write_llm("only_a.py"))
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks-b")
    client_b = _live_client(_write_llm("only_b.py"))
    results: dict[str, object] = {}

    def scenario(key: str, client: TestClient, repo: Path, other: Path, filename: str) -> None:
        try:
            tid = client.post(
                "/api/tasks", json={"goal": "干活", "repo_root": str(repo)}
            ).json()["task_id"]
            status = _wait_task(client, tid)
            view = client.get(f"/api/tasks/{tid}").json()
            branch = view["worktree_branch"]
            own = branch in _git(repo, "branch", "--list", "agent/*")
            cross = branch in _git(other, "branch", "--list", "agent/*")
            resp = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"})
            results[key] = (status, own, cross, resp.json(), filename)
        except Exception as exc:  # noqa: BLE001 —— 线程里的断言失败要带出来
            results[key] = exc

    t1 = threading.Thread(target=scenario, args=("a", client_a, git_repo, repo_b, "only_a.py"))
    t2 = threading.Thread(target=scenario, args=("b", client_b, repo_b, git_repo, "only_b.py"))
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)

    for key in ("a", "b"):
        assert not isinstance(results[key], Exception), f"{key} 侧异常：{results[key]}"
    for key, repo, other, mine in (
        ("a", git_repo, repo_b, "only_a.py"),
        ("b", repo_b, git_repo, "only_b.py"),
    ):
        status, own, cross, settle, filename = results[key]
        assert status == TASK_DONE
        assert settle["status"] == "merged"
        assert own and not cross                       # 分支只在绑定仓，不串仓
        assert (repo / mine).exists()                  # 写入/合入落点正确
        assert not (repo / ("only_b.py" if key == "a" else "only_a.py")).exists()
        assert not (other / filename).exists()


# ---- B6（任务沙箱级）：沙箱 registry 继承任务工作区绑定（父子不串仓）----


def test_b6_sandbox_registry_inherits_binding(git_repo, monkeypatch):
    real = app_mod.registry_in_worktree
    seen: dict = {}

    def spy(registry, wt, binding=None):
        seen["binding"] = binding
        return real(registry, wt, binding=binding)

    monkeypatch.setattr("facta.server.app.registry_in_worktree", spy)
    baseline = _git(git_repo, "rev-parse", "HEAD")
    toplevel = _git(git_repo, "rev-parse", "--show-toplevel")

    client = _live_client(_write_llm())
    tid = client.post(
        "/api/tasks", json={"goal": "干活", "repo_root": str(git_repo)}
    ).json()["task_id"]
    assert _wait_task(client, tid) == TASK_DONE

    # 沙箱里的工具继承任务绑定三元组（子 agent git 落点与任务同仓）
    assert seen["binding"] == WorkspaceBinding(
        repo_root=Path(toplevel), target_branch="main", baseline=baseline
    )
    # 落点证据：写进绑定仓库的沙箱工作树，git 元数据归属绑定仓库
    view = client.get(f"/api/tasks/{tid}").json()
    wt = Path(view["worktree"])
    assert (wt / "feat.py").exists()
    common = Path(_git(wt, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (wt / common).resolve()
    assert common.parent == Path(toplevel).resolve()


# ---- B9：删目录留分支 → 呈现恢复三选项，重建挂回后原产物可找回 ----


def test_b9_recovery_options_reattach(git_repo):
    client = _live_client(_write_llm())
    tid = client.post(
        "/api/tasks", json={"goal": "干活", "repo_root": str(git_repo)}
    ).json()["task_id"]
    assert _wait_task(client, tid) == TASK_DONE
    view = client.get(f"/api/tasks/{tid}").json()
    wt = Path(view["worktree"])
    branch = view["worktree_branch"]
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "feat")
    shutil.rmtree(wt)   # 手工删目录，分支还在

    d = client.get(f"/api/tasks/{tid}/worktree").json()
    assert d["state"] == "missing_worktree"
    assert d["recovery"] == ["reattach", "merge_direct", "discard"]
    assert branch in d["changes"]                      # 如实指路「产物在分支 X」
    assert d["candidate"]                              # 候选 = 分支头快照

    # 恢复选项① 重建挂回：原产物可找回（分支名真值源=字段，不反推）
    r = client.post(f"/api/tasks/{tid}/worktree", json={"action": "reattach"})
    assert r.status_code == 200
    assert r.json()["status"] == "reattached" and r.json()["cleared"] is False
    wt2 = Path(client.get(f"/api/tasks/{tid}").json()["worktree"])
    assert wt2.is_dir()
    assert (wt2 / "feat.py").exists()                  # 原产物找回来了
    assert _git(wt2, "rev-parse", "--abbrev-ref", "HEAD") == branch

    # 恢复后可正常收口合入，落绑定 target_branch
    r2 = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"})
    assert r2.json()["status"] == "merged" and r2.json()["cleared"] is True
    assert (git_repo / "feat.py").exists()


def test_b9_recovery_options_merge_direct_and_discard(git_repo):
    # merge_direct：目录缺失时直接合入分支，零残留
    client = _live_client(_write_llm())
    tid = client.post(
        "/api/tasks", json={"goal": "干活", "repo_root": str(git_repo)}
    ).json()["task_id"]
    assert _wait_task(client, tid) == TASK_DONE
    view = client.get(f"/api/tasks/{tid}").json()
    wt = Path(view["worktree"])
    branch = view["worktree_branch"]
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "feat")
    shutil.rmtree(wt)

    d = client.get(f"/api/tasks/{tid}/worktree").json()
    r = client.post(
        f"/api/tasks/{tid}/worktree",
        json={"action": "merge_direct", "candidate": d["candidate"]},
    )
    assert r.json()["status"] == "merged" and r.json()["cleared"] is True
    assert (git_repo / "feat.py").exists()
    assert branch not in _git(git_repo, "branch", "--list", "agent/*")   # 零残留

    # discard：丢弃同样零残留（显式裁决）
    client2 = _live_client(_write_llm("throw.py"))
    tid2 = client2.post(
        "/api/tasks", json={"goal": "干活", "repo_root": str(git_repo)}
    ).json()["task_id"]
    assert _wait_task(client2, tid2) == TASK_DONE
    view2 = client2.get(f"/api/tasks/{tid2}").json()
    wt2 = Path(view2["worktree"])
    branch2 = view2["worktree_branch"]
    _git(wt2, "add", "-A")
    _git(wt2, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "throw")
    shutil.rmtree(wt2)
    r2 = client2.post(f"/api/tasks/{tid2}/worktree", json={"action": "discard"})
    assert r2.json()["status"] == "discarded" and r2.json()["cleared"] is True
    assert branch2 not in _git(git_repo, "branch", "--list", "agent/*")
    assert not (git_repo / "throw.py").exists()


# ---- B10：绑定失效（target_branch 消失 / 仓库转非 Git）→ blocked，不启动执行 ----


def test_b10_binding_invalid_blocks_and_reports(git_repo, monkeypatch):
    real_sched = app_mod._maybe_start_queued
    box: dict = {}

    def capture(*args, **kwargs):
        box["args"] = args

    client = _client(monkeypatch, sched_stub=capture)
    tid = client.post(
        "/api/tasks", json={"goal": "干活", "repo_root": str(git_repo)}
    ).json()["task_id"]

    # 靶一：target_branch 被删
    _git(git_repo, "checkout", "-q", "-b", "other")
    _git(git_repo, "branch", "-D", "main")
    real_sched(*box["args"])   # 手工触发调度（打桩环境下唯一放行的 worker 入口）

    deadline = time.time() + 10
    while time.time() < deadline:
        d = client.get(f"/api/tasks/{tid}").json()
        if d["status"] == TASK_BLOCKED:
            break
        time.sleep(0.05)
    d = client.get(f"/api/tasks/{tid}").json()
    assert d["status"] == TASK_BLOCKED
    assert "绑定失效" in d["blocked_reason"] and "main" in d["blocked_reason"]
    assert d["worktree"] == "" and d["run_ids"] == []   # 不硬跑，不虚构

    # 靶二：仓库转非 Git
    repo2 = _make_repo(git_repo.parent / "repo2", "b.py")
    tid2 = client.post(
        "/api/tasks", json={"goal": "干活", "repo_root": str(repo2)}
    ).json()["task_id"]
    shutil.move(str(repo2 / ".git"), str(repo2 / ".git.bak"))
    real_sched(*box["args"])
    deadline = time.time() + 10
    while time.time() < deadline:
        d2 = client.get(f"/api/tasks/{tid2}").json()
        if d2["status"] == TASK_BLOCKED:
            break
        time.sleep(0.05)
    d2 = client.get(f"/api/tasks/{tid2}").json()
    assert d2["status"] == TASK_BLOCKED
    assert "绑定失效" in d2["blocked_reason"] and "Git" in d2["blocked_reason"]
    assert d2["run_ids"] == []


# ---- B11：目标已前进（target_branch HEAD ≠ baseline）如实呈现 ----


def test_b11_target_moved_presented(git_repo):
    baseline = _git(git_repo, "rev-parse", "HEAD")
    client = _live_client(_write_llm())
    tid = client.post(
        "/api/tasks", json={"goal": "干活", "repo_root": str(git_repo)}
    ).json()["task_id"]
    assert _wait_task(client, tid) == TASK_DONE

    assert client.get(f"/api/tasks/{tid}").json()["target_moved"] is False
    assert client.get(f"/api/tasks/{tid}/worktree").json()["target_moved"] is False

    (git_repo / "b.txt").write_text("2\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "advance")

    d = client.get(f"/api/tasks/{tid}").json()
    assert d["target_moved"] is True
    assert d["baseline"] == baseline                    # baseline 钉死不跟
    assert client.get(f"/api/tasks/{tid}/worktree").json()["target_moved"] is True


# ---- B13：记录与 Git 元数据冲突 → 保留现场、呈现冲突、不自动清理 ----


def test_b13_metadata_conflict_preserved(git_repo, tmp_path):
    baseline = _git(git_repo, "rev-parse", "HEAD")
    wt, err = create_worktree(repo_root=git_repo, branch="agent/b13face", baseline=baseline)
    assert err == "" and wt.is_dir()
    _git(wt, "checkout", "-q", "-b", "other-branch")   # 制造冲突：实际检出 ≠ 记录

    task = Task(
        task_id=uuid.uuid4().hex,
        goal="冲突现场",
        status=TASK_DONE,
        repo_root=_git(git_repo, "rev-parse", "--show-toplevel"),
        target_branch="main",
        baseline=baseline,
        worktree=str(wt),
        worktree_branch="agent/b13face",
    )
    store = TaskStore()
    store.save(task)

    client = TestClient(create_app(_task_ctx(ScriptedLLM([]))))
    d = client.get(f"/api/tasks/{task.task_id}/worktree").json()
    assert d["state"] == "metadata_conflict"
    assert "agent/b13face" in d["metadata_conflict"] and "other-branch" in d["metadata_conflict"]
    assert "保留现场" in d["changes"]

    # 收口不自动清理（四段全空也不动——冲突现场绝不误删）
    app_mod._task_sandbox_closeout(store, store.get(task.task_id), wt)
    assert wt.exists() and store.get(task.task_id).worktree == str(wt)

    # 合入被拒（人工裁决前不动现场）；keep 集合圈进保留现场
    r = client.post(f"/api/tasks/{task.task_id}/worktree", json={"action": "merge"})
    assert r.status_code == 409 and "元数据冲突" in r.json()["detail"]
    keep = {Path(t.worktree).name for t in TaskStore().list() if t.worktree}   # assemble 口径
    assert wt.name in keep
    stale = wt_mod.WORKTREES_DIR / "stale-leaf"
    stale.mkdir(parents=True, exist_ok=True)
    cleanup_stale_worktrees(keep=keep)
    assert wt.exists() and not stale.exists()

    # 显式裁决 discard 才零残留：目录 + 两个分支全清
    r2 = client.post(f"/api/tasks/{task.task_id}/worktree", json={"action": "discard"})
    assert r2.json()["status"] == "discarded" and r2.json()["cleared"] is True
    assert not wt.exists()
    assert _git(git_repo, "branch", "--list", "agent/*") == ""
    assert _git(git_repo, "branch", "--list", "other-branch") == ""
