"""ADR 116 §3 验收：项目登记表（ProjectsManifest）+ 归属判定（resolve_learned_dir）
+ settle 归属接线。

钉子：
- 归属判定三分支（登记命中→项目目录 / 本仓→全局 / 外部未登记→None）；
- chat 与任务轮读同一份全局（None ≡ WORKSPACE_ROOT，双入口不漂移）；
- 本仓禁止登记（双读入口后门）；
- migrate = 复制三桶不删源（045/064 记忆不可逆保守）；
- settle 的 consolidate 与 maintain_memory **同目录**（评审问题 4，078 整理
  曾漏换目录——只动全局）。
"""

from pathlib import Path

import pytest

from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.consolidate import CATEGORIES
from facta.memory.store import Session, SessionStore
from facta.orchestrator import assemble as asm
from facta.paths import WORKSPACE_ROOT
from facta.projects import ProjectsManifest, resolve_learned_dir


@pytest.fixture(autouse=True)
def _isolate_disk_state(tmp_path, monkeypatch):
    monkeypatch.setattr("facta.projects.PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))


# ---- 登记表 CRUD ----


def test_register_derives_learned_and_roundtrips(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = ProjectsManifest()

    entry = manifest.register("alpha", str(repo))

    assert entry["memory_mode"] == "centralized"
    learned = Path(entry["learned_path"])
    assert learned == manifest.root / "alpha" / "learned"
    assert learned.is_dir()   # 登记即建目录
    assert manifest.get("alpha") == entry
    assert [e["name"] for e in manifest.list()] == ["alpha"]
    # 按 repo_root 反查（写法不同也认：~ 与相对路径统一 expanduser+resolve）
    assert manifest.by_repo_root(repo) == entry
    assert manifest.by_repo_root(repo.resolve()) == entry
    assert manifest.by_repo_root(tmp_path / "other") is None


def test_register_rejects_own_workspace(tmp_path):
    # 评审问题 6：本仓恒走全局，禁止登记——双读入口会漂移
    with pytest.raises(ValueError, match="禁止登记"):
        ProjectsManifest().register("own", str(WORKSPACE_ROOT))


def test_register_rejects_bad_inputs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = ProjectsManifest()

    for bad in ("", ".", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError, match="非法项目名"):
            manifest.register(bad, str(repo))
    with pytest.raises(ValueError, match="不存在"):
        manifest.register("p", str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="memory_mode"):
        manifest.register("p", str(repo), "bogus")
    with pytest.raises(ValueError, match="确认"):
        manifest.register("p", str(repo), "in-repo")   # 就地式须显式确认（114）
    manifest.register("p", str(repo))
    with pytest.raises(ValueError, match="已登记"):
        manifest.register("p", str(tmp_path))   # 名字唯一
    with pytest.raises(ValueError, match="已登记"):
        manifest.register("q", str(repo))   # 仓唯一


def test_update_switch_mode_and_migrate_copies_without_deleting(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = ProjectsManifest()
    entry = manifest.register("alpha", str(repo))
    old = Path(entry["learned_path"])
    for cat in CATEGORIES:
        (old / f"{cat}.md").write_text(f"2026-11-01 [{cat}] 记忆行\n", encoding="utf-8")

    with pytest.raises(ValueError, match="确认"):
        manifest.update("alpha", "in-repo")
    with pytest.raises(ValueError, match="未登记"):
        manifest.update("nope", "centralized")

    updated = manifest.update("alpha", "in-repo", migrate=True, confirm_repo_write=True)

    assert updated["memory_mode"] == "in-repo"
    new = Path(updated["learned_path"])
    assert new == repo.resolve() / ".facta" / "learned"
    for cat in CATEGORIES:
        assert (new / f"{cat}.md").read_text(encoding="utf-8") == (old / f"{cat}.md").read_text(encoding="utf-8")
        assert (old / f"{cat}.md").is_file()   # 复制不删源（记忆不可逆保守）


# ---- 归属判定 ----


def test_resolve_learned_dir_three_branches(tmp_path):
    own = tmp_path / "own-learned"
    repo = tmp_path / "repo"
    repo.mkdir()
    entry = ProjectsManifest().register("alpha", str(repo))

    # ① 登记命中 → 该项目目录
    assert resolve_learned_dir(repo, own_dir=own) == Path(entry["learned_path"])
    # ② 本仓 → 全局（chat 与无归属任务轮同一入口：None ≡ WORKSPACE_ROOT）
    assert resolve_learned_dir(None, own_dir=own) == own
    assert resolve_learned_dir(WORKSPACE_ROOT, own_dir=own) == own
    # ③ 外部未登记仓 → None（注入不给，固化回落全局）
    assert resolve_learned_dir(tmp_path / "stranger", own_dir=own) is None


# ---- settle 归属接线（评审问题 4）----


def test_settle_consolidate_and_maintain_memory_share_learned_dir(tmp_path, monkeypatch):
    """consolidate 与 maintain_memory（078 sleep-time 整理）必须同目录：项目
    settle 后整理只动项目目录——078 整理曾写死全局漏换目录（评审问题 4）。
    """
    recorded: dict = {}
    monkeypatch.setattr(asm, "CONSOLIDATE_THRESHOLD", 1)

    def fake_consolidate(session, llm, learned, **kw):
        recorded["consolidate"] = learned
        return ("报告", True)

    def fake_maintain(learned, user_path):
        recorded["maintain"] = learned
        return ""

    monkeypatch.setattr(asm, "consolidate", fake_consolidate)
    monkeypatch.setattr(asm, "maintain_memory", fake_maintain)

    store = SessionStore(tmp_path / "sessions")
    session = Session(title="已设标题")
    session.messages.append(Message(role="user", content="hi"))
    session.messages.append(Message(role="assistant", content="好"))
    sid = store.create(Session())
    store.save(sid, session)   # 调用方契约：保底落盘先于 settle

    # 显式归属（任务轮注入的项目目录）：两处都吃 learned_dir
    learned = tmp_path / "proj-learned"
    asm.settle_session(session, sid, store, ScriptedLLM([]), learned_dir=learned)
    assert recorded["consolidate"] == recorded["maintain"] == learned

    # 缺省（chat）：两处都回落本仓全局（既有调用方零改动的对照面）
    recorded.clear()
    session.consolidated_upto = 0   # 上一轮已推进游标，重置才再触发固化
    monkeypatch.setattr(asm, "LEARNED_DIR", tmp_path / "global-learned")
    asm.settle_session(session, sid, store, ScriptedLLM([]))
    assert recorded["consolidate"] == recorded["maintain"] == tmp_path / "global-learned"
