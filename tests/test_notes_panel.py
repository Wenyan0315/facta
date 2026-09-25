"""知识语料面板验收（042）：围栏四道 + 原子写 + hash 乐观锁 + 陈旧回报。

不变量：
- 围栏只有一份（tools/notes.resolve_note_path），工具层与面板端点共用；
  四道逐个钉：越界 / 非 .md / 子目录 / 存在性（写要不存在、面板要已存在）
- 保存走原子写：不留 .tmp，磁盘内容与提交一致
- base_hash 不匹配 → 409 且磁盘一字不动（挡「IDE 与面板同时改一篇」的盲覆盖）
- 面板 PUT 对不存在的笔记 404，绝不顺手创建（新建会绕过 write_note 的查重闸门）
- 陈旧标记按指纹现算，不猜：图谱只在该篇曾进过图时才算陈旧
"""

import pytest

from agent.knowledge.sync import file_hash
from agent.tools.notes import resolve_note_path

# ---------- 围栏本体：纯函数层（避开 HTTP 客户端的路径规范化） ----------

def test_fence_rejects_escape(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    for bad in ["../evil.md", "../../.env.md", "sub/../../x.md"]:
        with pytest.raises(ValueError, match="越界"):
            resolve_note_path(notes, bad)


def test_fence_rejects_non_markdown(tmp_path):
    with pytest.raises(ValueError, match=r"\.md"):
        resolve_note_path(tmp_path, "note.txt")


def test_fence_rejects_subdirectory(tmp_path):
    # resolve 后仍在 notes/ 内（所以第一道拦不住），靠第三道「不支持子目录」
    with pytest.raises(ValueError, match="子目录"):
        resolve_note_path(tmp_path, "sub/note.md")


def test_fence_accepts_plain_name(tmp_path):
    got = resolve_note_path(tmp_path, "a.md")
    assert got == (tmp_path / "a.md").resolve()


# ---------- 端点层 ----------

def _client(monkeypatch, tmp_path, *, kb=None):
    """最小 AppContext + NOTES_DIR 指向临时目录（与 test_learned 同款隔离）。"""
    from fastapi.testclient import TestClient

    from agent.core.llm import ScriptedLLM
    from agent.core.types import Message
    from agent.memory.store import SessionStore
    from agent.orchestrator.agent import Agent
    from agent.orchestrator.assemble import AppContext
    from agent.server.app import create_app
    from agent.tools.registry import ToolRegistry

    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    monkeypatch.setattr("agent.server.app.NOTES_DIR", notes_dir)

    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="ok")]),
        internal_llm=ScriptedLLM([]),
        kb=kb,
        store=SessionStore(tmp_path / "sessions"),
        build_agent=lambda session: Agent(
            name="test", system_prompt="测试人设", registry=ToolRegistry()
        ),
        todos=None,
    )
    return TestClient(create_app(ctx)), ctx, notes_dir


@pytest.fixture
def client_env(monkeypatch, tmp_path):
    return _client(monkeypatch, tmp_path)


def _note(notes_dir, name="a.md", text="原文\n"):
    path = notes_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def test_list_only_markdown_top_level(client_env):
    client, _, notes_dir = client_env
    _note(notes_dir, "b.md")
    _note(notes_dir, "a.md")
    (notes_dir / "draft.txt").write_text("不是笔记", encoding="utf-8")
    (notes_dir / "sub").mkdir()
    (notes_dir / "sub" / "c.md").write_text("子目录不进列表", encoding="utf-8")
    # 原子写的临时文件也不该冒出来（.tmp 不匹配 *.md）
    (notes_dir / "a.md.tmp").write_text("半截", encoding="utf-8")

    listed = client.get("/api/notes").json()
    assert [it["name"] for it in listed] == ["a.md", "b.md"]
    assert all(it["size"] > 0 for it in listed)


def test_list_missing_dir_is_empty(client_env):
    # 第一次跑还没建 data/notes/ 是正常状态，不是 500
    client, _, notes_dir = client_env
    notes_dir.rmdir()
    assert client.get("/api/notes").json() == []


def test_read_returns_content_and_hash(client_env):
    client, _, notes_dir = client_env
    _note(notes_dir, text="正文\n")
    body = client.get("/api/notes/a.md").json()
    assert body["content"] == "正文\n"
    assert body["hash"] == file_hash("正文\n")


def test_read_missing_is_404(client_env):
    client, _, _ = client_env
    assert client.get("/api/notes/nope.md").status_code == 404


def test_read_rejects_bad_name(client_env):
    client, _, _ = client_env
    assert client.get("/api/notes/evil.txt").status_code == 400


def test_save_roundtrip_is_atomic(client_env):
    client, _, notes_dir = client_env
    path = _note(notes_dir, text="原文\n")

    resp = client.put(
        "/api/notes/a.md", json={"content": "改过的\n", "base_hash": file_hash("原文\n")}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hash"] == file_hash("改过的\n")
    assert path.read_text(encoding="utf-8") == "改过的\n"
    # 原子写不留尾巴：.tmp 已 rename 走，目录里只剩那一篇
    assert list(notes_dir.iterdir()) == [path]


def test_save_stale_hash_is_409_and_disk_untouched(client_env):
    client, _, notes_dir = client_env
    path = _note(notes_dir, text="磁盘现值\n")

    resp = client.put(
        "/api/notes/a.md", json={"content": "盲覆盖\n", "base_hash": file_hash("陈旧的载入值\n")}
    )
    assert resp.status_code == 409
    assert path.read_text(encoding="utf-8") == "磁盘现值\n"


def test_save_missing_note_is_404_not_created(client_env):
    client, _, notes_dir = client_env
    resp = client.put("/api/notes/new.md", json={"content": "x", "base_hash": ""})
    assert resp.status_code == 404
    assert not (notes_dir / "new.md").exists()   # 不顺手创建（绕过查重闸门）


def test_save_reports_graph_staleness(client_env):
    client, ctx, notes_dir = client_env
    path = _note(notes_dir, text="原文\n")
    # kb 未启用（无 embedder）→ 向量库无从陈旧；图谱按指纹判：曾进过图且指纹变了才算
    ctx.graph.note_hashes["a.md"] = file_hash("更早的内容\n")

    stale = client.put(
        "/api/notes/a.md", json={"content": "改过的\n", "base_hash": file_hash("原文\n")}
    ).json()["stale"]
    assert stale == {"kb": False, "graph": True}

    # 再存一次且图谱指纹恰好等于新内容 → 不算陈旧；从没进过图的笔记也不算
    ctx.graph.note_hashes["a.md"] = file_hash("改过的\n第二行\n")
    stale = client.put(
        "/api/notes/a.md",
        json={"content": "改过的\n第二行\n", "base_hash": file_hash("改过的\n")},
    ).json()["stale"]
    assert stale == {"kb": False, "graph": False}
    assert path.read_text(encoding="utf-8") == "改过的\n第二行\n"


def test_sync_without_kb_is_503(client_env):
    client, _, _ = client_env
    assert client.post("/api/notes/sync").status_code == 503
