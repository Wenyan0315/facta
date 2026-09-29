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

import pytest

from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.store import Session
from facta.orchestrator.agent import Agent
from facta.orchestrator.loop import run_turn
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

    patch 目标是 agent.tools.worktree（from-import 后名字绑定在消费模块，
    只 patch paths 不生效——S6a 开发实踩：测试的 worktree 建到了真项目里，
    垃圾 commit 混进 main）。files/terminal 的锚点走 ctx 注入不受影响。
    """
    from facta.tools import worktree as wt_mod

    monkeypatch.setattr(wt_mod, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(wt_mod, "WORKTREES_DIR", tmp_path / "data" / "worktrees")
    (tmp_path / "base.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                   "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


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
    assert "base.py" in changes and "new.py" in changes

    out = commit_and_merge_back(wt, "test: 改动")
    assert "已合回" in out
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
    # 空改动守卫：无改动时 commit_and_merge_back 不 commit 不 merge，直接清理
    wt, err = create_worktree()
    assert err == ""
    out = commit_and_merge_back(wt, "空")
    assert "无改动" in out
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
