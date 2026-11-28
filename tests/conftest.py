"""测试隔离总闸：data/tasks/（含 M5 的 runs/ Run 明细落盘）一律进 tmp_path。

此前只有部分用例文件各自 monkeypatch `facta.orchestrator.task.TASKS_DIR`，
漏patch的用例会把任务快照/Run 元快照写进仓库真目录（RunLog 让每个 Run
创建都落盘，泄漏面从「偶尔写任务」扩大到「每跑必写」）。集中在这里：
autouse + tmp_path，一把钥匙全测生效；个别用例自己的 patch 照旧可覆盖
（同为 tmp 路径，殊途同归）。
"""

import subprocess

import pytest


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks")


@pytest.fixture(scope="session")
def wt_git_repo(tmp_path_factory):
    """session 级真 git 仓库（基线一份提交）——worktree 隔离闸的地基。

    线五①起 chat 任务默认开沙箱（真 git worktree）：不隔离的话测试会往
    真项目仓库建分支（test_worktree.py 头注记的血案同款）。git init 每个
    测试跑一次太重，session 造一次、分支名 uuid 不碰撞，顺序执行无竞态。
    """
    repo = tmp_path_factory.mktemp("wt-repo")
    (repo / "base.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


@pytest.fixture(autouse=True)
def _isolate_worktree_roots(wt_git_repo, monkeypatch):
    """worktree 模块的两个根名指进 session 仓库（函数体解析名字，patch 运行时生效）。

    test_worktree.py 的 git_repo fixture 同名属性二次 patch（指它自己的
    tmp_path）——后设先生效，teardown 逆序还原，双层共存无碍。
    """
    from facta.tools import worktree as wt_mod

    monkeypatch.setattr(wt_mod, "WORKSPACE_ROOT", wt_git_repo)
    monkeypatch.setattr(wt_mod, "WORKTREES_DIR", wt_git_repo / "data" / "worktrees")
