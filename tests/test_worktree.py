"""S6a worktree 隔离验收：沙箱生命周期 + spawn 端到端 + 确认缝合回。

不变量：
- 隔离：子 agent 的文件改动只发生在 worktree，主工作区（及 git 状态）零污染
- 确认缝：有改动必经裁决——批准合回（merge 进主分支）、拒绝整棵丢弃（零残留）
- 无改动：沙箱自动清理，不占目录
- 审计同源：子 registry 与主 registry 落同一审计流

测试在真 git 仓库上跑（tmp_path init + commit）——worktree 原语是 git
封装，mock git 无意义（协议面就是命令行）。
"""

import json
import subprocess
import tempfile
import time
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
from facta.orchestrator.loop import run_turn
from facta.orchestrator.task import TASK_DONE, TASK_FAILED
from facta.server.app import create_app
from facta.tools.context import ToolContext
from facta.tools.files import register_file_tools
from facta.tools.registry import ToolRegistry
from facta.tools.spawn import register_spawn_tools
from facta.tools.terminal import register_terminal_tools
from facta.tools.worktree import (
    cleanup_stale_worktrees,
    commit_and_merge_back,
    create_worktree,
    discard_worktree,
    worktree_changes,
)


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    """真 git 仓库 + 一份基线提交；把 worktree 模块的根指过来。

    仓库与 WORKTREES_DIR 分家（tmp_path/repo vs tmp_path/worktrees）：
    P0 合入前置要求目标工作区干净，沙箱目录住进仓库会顶着 untracked 噪声
    把一切合入挡在前置门外——生产拓扑同款（沙箱住 Facta data/，用户仓库
    另在别处）。patch 目标是 facta.tools.worktree（from-import 后名字绑定
    在消费模块，只 patch paths 不生效——S6a 开发实踩：测试的 worktree 建到
    了真项目里，垃圾 commit 混进 main）。files/terminal 的锚点走 ctx 注入不受影响。
    """
    from facta.tools import worktree as wt_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(wt_mod, "WORKSPACE_ROOT", repo)
    monkeypatch.setattr(wt_mod, "WORKTREES_DIR", tmp_path / "worktrees")
    (repo / "base.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                   "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _git_status(repo) -> str:
    proc = subprocess.run(["git", "status", "--short"], cwd=repo,
                          capture_output=True, text=True, check=True)
    return proc.stdout


def _main_setup(repo, sub_llm: ScriptedLLM, main_script: list[Message]):
    """主链装配：registry（file 四件+terminal+spawn）+ ctx（锚主根+子链）。

    ctx.llm 必须在 register_spawn_tools 之前设好——spawn 是条件注册
    （llm 缺席不上菜单），注册后才改 ctx.llm 闭包已捕获 None。
    """
    registry = ToolRegistry()
    ctx = ToolContext(notes_dir=repo / "notes", workspace_root=repo, llm=sub_llm)
    register_file_tools(registry, ctx)
    register_terminal_tools(registry, ctx)
    register_spawn_tools(registry, ctx)
    main_llm = ScriptedLLM(main_script)
    return registry, ctx, main_llm


def _call(name: str, args: dict) -> dict:
    return {"id": f"call_{name}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


# ---------- 原语 ----------


def test_create_changes_merge_back(git_repo):
    # 全生命周期：创建 → 改文件 → changes 可见 → 合回 → 主分支有改动
    wt, err = create_worktree()
    assert err == ""
    assert wt.exists()

    (wt / "base.py").write_text("x = 2\n", encoding="utf-8")
    (wt / "new.py").write_text("y = 1\n", encoding="utf-8")

    changes = worktree_changes(wt)
    assert "base.py" in changes.summary() and "new.py" in changes.summary()

    status, _detail = commit_and_merge_back(wt, "test: 改动")
    assert status == "merged"
    assert (git_repo / "base.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (git_repo / "new.py").exists()
    assert not wt.exists()                      # worktree 已清理
    assert _git_status(git_repo) == ""          # 主工作区干净（改动已 commit+merge）


def test_create_discard_zero_residue(git_repo):
    # 丢弃：改了文件也整棵丢弃，主分支/工作区零影响
    wt, err = create_worktree()
    assert err == ""
    (wt / "base.py").write_text("x = 999\n", encoding="utf-8")

    discard_worktree(wt)
    assert not wt.exists()
    assert (git_repo / "base.py").read_text(encoding="utf-8") == "x = 1\n"
    assert _git_status(git_repo) == ""


def test_merge_back_with_no_changes(git_repo):
    # 空改动守卫：四段无产物时 commit_and_merge_back 不 commit 不 merge，
    # 返回 empty 态直接清理
    wt, err = create_worktree()
    assert err == ""
    status, _detail = commit_and_merge_back(wt, "空")
    assert status == "empty"
    assert not wt.exists()


def test_cleanup_stale_removes_residue(git_repo):
    # 启动回收：残留 worktree（进程被杀场景）全清
    wt, err = create_worktree()
    assert err == ""
    assert wt.exists()
    # 不走正常 discard——模拟残留（目录+分支都在）

    count = cleanup_stale_worktrees()
    assert count == 1
    assert not wt.exists()


# ---------- spawn worktree 端到端 ----------


def test_spawn_worktree_isolation_and_merge(git_repo):
    # 端到端：子 agent 在 worktree 改文件 → 主工作区全程零污染 →
    # 确认缝批准 → 合回主分支
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("write_file", {"path": "feat.py", "content": "print('feat')\n"}),
        ]),
        Message(role="assistant", content="结论：已创建 feat.py"),
    ])
    registry, ctx, main_llm = _main_setup(git_repo, sub_llm, [
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "建 feat.py", "worktree": True}),
        ]),
        Message(role="assistant", content="子任务完成"),
    ])

    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(name="main", system_prompt="主 sys", registry=registry)
    confirms = []
    run_turn(session, "建文件", agent=agent, llm=main_llm,
             on_confirm=lambda name, args: (confirms.append(name), True)[1])

    # 合回裁决确实过了确认缝（merge_worktree）
    assert "merge_worktree" in confirms
    # 主分支拿到改动
    assert (git_repo / "feat.py").exists()
    # 沙箱清干净（读 patch 后的 WORKTREES_DIR；目录可能整个不存在=更干净）
    from facta.tools.worktree import WORKTREES_DIR
    assert not WORKTREES_DIR.exists() or not any(WORKTREES_DIR.iterdir())
    # 主底片只有 spawn 结论一条 tool 消息（噪声隔离保持）
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "feat.py" in tool_msgs[0].content or "已创建" in tool_msgs[0].content


def test_spawn_worktree_reject_discards(git_repo):
    # 拒绝：改动整棵丢弃，主分支零影响
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("write_file", {"path": "pwn.py", "content": "x"}),
        ]),
        Message(role="assistant", content="结论：建了 pwn.py"),
    ])
    registry, ctx, main_llm = _main_setup(git_repo, sub_llm, [
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "建 pwn.py", "worktree": True}),
        ]),
        Message(role="assistant", content="知道了"),
    ])

    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(name="main", system_prompt="主 sys", registry=registry)
    run_turn(session, "建文件", agent=agent, llm=main_llm,
             on_confirm=lambda name, args: False)   # 拒绝

    assert not (git_repo / "pwn.py").exists()
    assert _git_status(git_repo) == ""
    from facta.tools.worktree import WORKTREES_DIR
    assert not WORKTREES_DIR.exists() or not any(WORKTREES_DIR.iterdir())


def test_spawn_worktree_no_changes_cleans_up(git_repo):
    # 无改动：子 agent 只读不写 → 沙箱自动清理，结论附「无文件改动」
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="结论：看了，没改"),
    ])
    registry, ctx, main_llm = _main_setup(git_repo, sub_llm, [
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "看一眼", "worktree": True}),
        ]),
        Message(role="assistant", content="好"),
    ])

    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(name="main", system_prompt="主 sys", registry=registry)
    run_turn(session, "看", agent=agent, llm=main_llm,
             on_confirm=lambda name, args: True)

    from facta.tools.worktree import WORKTREES_DIR
    assert not WORKTREES_DIR.exists() or not any(WORKTREES_DIR.iterdir())
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert "无文件改动" in tool_msgs[0].content


def test_spawn_without_worktree_unchanged(git_repo):
    # 回归：worktree=False（默认）走 S5c 原路径——共享主 registry，改动直落主工作区
    sub_llm = ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("write_file", {"path": "direct.py", "content": "x"}),
        ]),
        Message(role="assistant", content="结论：直改"),
    ])
    registry, ctx, main_llm = _main_setup(git_repo, sub_llm, [
        Message(role="assistant", content="", tool_calls=[
            _call("spawn_subagent", {"task": "直改"}),
        ]),
        Message(role="assistant", content="好"),
    ])

    session = Session()
    session.messages.append(Message(role="system", content="sys"))
    agent = Agent(name="main", system_prompt="主 sys", registry=registry)
    run_turn(session, "改", agent=agent, llm=main_llm,
             on_confirm=lambda name, args: True)

    # 直改落主工作区（无沙箱）
    assert (git_repo / "direct.py").exists()


# ---------- 任务沙箱（线五①）：chat 任务默认隔离 + 显式收口 ----------


def _task_ctx(llm):
    """最小 AppContext（test_task_feed._make_ctx 同款形状，本文件自带一份）。

    registry 恒裸：沙箱重锚（registry_in_worktree）自带 file/terminal 五件
    ——这正是要验的接线（写进沙箱而不是主树）。
    """
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


def _run_task(llm):
    app = create_app(_task_ctx(llm))
    client = TestClient(app)
    tid = client.post("/api/tasks", json={"goal": "干活"}).json()["task_id"]
    status = ""
    for _ in range(200):   # worker 是后台线程，轮询等终态
        status = client.get(f"/api/tasks/{tid}").json()["status"]
        if status in (TASK_DONE, TASK_FAILED):
            break
        time.sleep(0.05)
    return client, tid, status


def _write_llm():
    return ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            _call("write_file", {"path": "feat.py", "content": "print('feat')\n"}),
        ]),
        Message(role="assistant", content="完成：已建 feat.py"),
    ])


def test_task_sandbox_isolation_and_explicit_merge(git_repo):
    # 全链（线五①）：chat 任务默认进沙箱 → 主树全程零污染 →
    # 显式 merge 收口，改动才进工作分支
    client, tid, status = _run_task(_write_llm())
    assert status == TASK_DONE
    view = client.get(f"/api/tasks/{tid}").json()
    assert view["worktree"]                        # 沙箱保留待收口
    assert not (git_repo / "feat.py").exists()     # 主树零污染
    changes = client.get(f"/api/tasks/{tid}/worktree").json()["changes"]
    assert "feat.py" in changes

    resp = client.post(f"/api/tasks/{tid}/worktree", json={"action": "merge"})
    assert resp.status_code == 200 and resp.json()["cleared"]
    assert (git_repo / "feat.py").exists()          # 显式合并后才进主树
    assert client.get(f"/api/tasks/{tid}").json()["worktree"] == ""
    assert client.get(f"/api/tasks/{tid}/worktree").status_code == 404


def test_task_sandbox_discard_keeps_main_clean(git_repo):
    # 收口的另一半：丢弃整棵沙箱，主树/主分支零影响
    client, tid, status = _run_task(_write_llm())
    assert status == TASK_DONE
    resp = client.post(f"/api/tasks/{tid}/worktree", json={"action": "discard"})
    assert resp.status_code == 200 and resp.json()["cleared"]
    assert not (git_repo / "feat.py").exists()
    assert _git_status(git_repo) == ""          # 主树零泄漏（沙箱/tasks 都住仓库外）
    assert client.get(f"/api/tasks/{tid}").json()["worktree"] == ""


def test_task_sandbox_empty_autoclean(git_repo):
    # 空改动守卫（spawn 同款）：任务没动文件 → 沙箱不留、字段清空
    client, tid, status = _run_task(ScriptedLLM([Message(role="assistant", content="没事发生")]))
    assert status == TASK_DONE
    for _ in range(100):   # 收口在 worker 的 finally 里，落定晚于 DONE 翻转
        if not client.get(f"/api/tasks/{tid}").json()["worktree"]:
            break
        time.sleep(0.05)
    assert client.get(f"/api/tasks/{tid}").json()["worktree"] == ""
    from facta.tools.worktree import WORKTREES_DIR
    assert not WORKTREES_DIR.exists() or not any(WORKTREES_DIR.iterdir())


def test_cleanup_keeps_claimed_worktrees(git_repo):
    # 线五①：启动回收认领任务沙箱（keep 名单）——有主的不许被当残留清掉
    wt, err = create_worktree()
    assert err == ""
    assert cleanup_stale_worktrees(keep={wt.name}) == 0
    assert wt.exists()
    discard_worktree(wt)
