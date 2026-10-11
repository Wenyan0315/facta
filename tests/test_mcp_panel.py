"""MCP 配置面板验收（106）：读写 API + 乐观锁 + 写侧校验不变式 + 密钥护栏。

不变量：
- GET：managed/effective path 对照 + env_override + active 运行时名单；
  headers 只报有无（has_headers），值不出现在任何响应里
- 写侧三道：清单损坏拒写（400）/ base_hash 整文件锁（409，挡手编与面板
  盲覆盖）/ 新内容先过 load_server_specs 校验再原子落盘——磁盘永远留不下
  会让下次启动「MCP 工具静默全消失」的坏清单
- POST 撞名 409；PUT/DELETE 不存在 404；PUT 保留原条目 headers（密钥不经 UI）
"""

import json

import pytest

from facta.knowledge.sync import file_hash


def _client(monkeypatch, tmp_path, *, mcp_active=None):
    """最小 AppContext + MANAGED_MCP_PATH 指向临时文件（与 test_notes_panel 同款隔离）。"""
    from fastapi.testclient import TestClient

    from facta.core.llm import ScriptedLLM
    from facta.core.types import Message
    from facta.memory.store import SessionStore
    from facta.orchestrator.agent import Agent
    from facta.orchestrator.assemble import AppContext
    from facta.server.app import create_app
    from facta.tools.registry import ToolRegistry

    manifest = tmp_path / "mcp_servers.json"
    monkeypatch.setattr("facta.server.app.MANAGED_MCP_PATH", manifest)

    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="ok")]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(tmp_path / "sessions"),
        build_agent=lambda session: Agent(
            name="test", system_prompt="测试人设", registry=ToolRegistry()
        ),
        todos=None,
        mcp_active=list(mcp_active or []),
    )
    return TestClient(create_app(ctx)), manifest


@pytest.fixture
def client_env(monkeypatch, tmp_path):
    return _client(monkeypatch, tmp_path)


def _write_manifest(manifest, servers):
    manifest.write_text(json.dumps({"servers": servers}, ensure_ascii=False, indent=2), encoding="utf-8")


def _body(base_hash, **overrides):
    payload = {"command": ["{python}", "servers/x.py"], "prefix": "", "timeout": 30.0,
               "enabled": True, "base_hash": base_hash}
    payload.update(overrides)
    return payload


# ---------- GET：对照信息与密钥护栏 ----------

def test_get_returns_manifest_and_hides_headers(client_env):
    client, manifest = client_env
    _write_manifest(manifest, [
        {"name": "notes", "command": ["{python}", "servers/notes_server.py"], "prefix": "mcp__"},
        {"name": "ctx7", "url": "https://mcp.context7.com/mcp", "enabled": False},
        {"name": "secret", "url": "https://x.example/mcp", "headers": {"Authorization": "Bearer sk-test"}},
    ])
    data = client.get("/api/mcp").json()
    assert [s["name"] for s in data["servers"]] == ["notes", "ctx7", "secret"]
    # headers 只报有无，值不进响应（密钥护栏）
    assert data["servers"][2]["has_headers"] is True
    assert "headers" not in data["servers"][2]
    assert "sk-test" not in json.dumps(data)
    assert data["env_override"] is False
    assert data["managed_path"] == data["effective_path"] == str(manifest)


def test_get_active_list_from_ctx(monkeypatch, tmp_path):
    """运行时名单来自 AppContext（assemble 拉起时记录），文件与运行时可以不一致。"""
    client, manifest = _client(monkeypatch, tmp_path, mcp_active=["notes"])
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}, {"name": "memory", "command": ["b"], "enabled": False}])
    data = client.get("/api/mcp").json()
    assert data["active"] == ["notes"]
    # 文件里有 memory（enabled:false），运行时名单没有——这正是「对照」要呈现的差异
    assert [s["name"] for s in data["servers"]] == ["notes", "memory"]


def test_get_env_override_reports_effective_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SERVERS", "/personal/mcp.json")
    client, manifest = _client(monkeypatch, tmp_path)
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}])
    data = client.get("/api/mcp").json()
    assert data["env_override"] is True
    assert data["effective_path"] == "/personal/mcp.json"
    assert data["managed_path"] == str(manifest)   # 面板恒管仓库文件


def test_get_missing_file_is_empty(client_env):
    client, _ = client_env
    data = client.get("/api/mcp").json()
    assert data["servers"] == []
    assert data["parse_error"] is None
    assert data["hash"] == file_hash("")


def test_get_parse_error_is_reported_not_500(client_env):
    client, manifest = client_env
    manifest.write_text("{broken", encoding="utf-8")
    data = client.get("/api/mcp").json()
    assert data["parse_error"] and "JSON" in data["parse_error"]
    assert data["servers"] == []


# ---------- 写侧：乐观锁 + 校验 + 原子写 ----------

def test_post_adds_server(client_env):
    client, manifest = client_env
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}])
    base = client.get("/api/mcp").json()["hash"]
    resp = client.post("/api/mcp/servers/memory", json=_body(base, command=["{python}", "servers/memory_server.py"]))
    assert resp.status_code == 201
    saved = json.loads(manifest.read_text(encoding="utf-8"))["servers"]
    assert [s["name"] for s in saved] == ["notes", "memory"]
    # 新 hash 可直接用于下一次写（省一次 GET 的链式语义）
    assert resp.json()["hash"] == client.get("/api/mcp").json()["hash"]


def test_post_collision_409(client_env):
    client, manifest = client_env
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}])
    base = client.get("/api/mcp").json()["hash"]
    resp = client.post("/api/mcp/servers/notes", json=_body(base))
    assert resp.status_code == 409
    assert len(json.loads(manifest.read_text(encoding="utf-8"))["servers"]) == 1


def test_put_toggles_enabled_and_keeps_headers(client_env):
    client, manifest = client_env
    _write_manifest(manifest, [
        {"name": "secret", "url": "https://x/mcp", "headers": {"Authorization": "Bearer sk-test"}, "enabled": False},
    ])
    base = client.get("/api/mcp").json()["hash"]
    resp = client.put("/api/mcp/servers/secret", json=_body(base, command=None, url="https://x/mcp", enabled=True))
    assert resp.status_code == 200
    entry = json.loads(manifest.read_text(encoding="utf-8"))["servers"][0]
    assert entry["enabled"] is True
    # 密钥护栏的写侧半边：headers 不经请求体，服务端原样保留
    assert entry["headers"] == {"Authorization": "Bearer sk-test"}


def test_put_missing_404(client_env):
    client, manifest = client_env
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}])
    resp = client.put("/api/mcp/servers/ghost", json=_body("whatever"))
    assert resp.status_code == 404


def test_delete_removes_entry(client_env):
    client, manifest = client_env
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}, {"name": "ctx7", "url": "https://x"}])
    base = client.get("/api/mcp").json()["hash"]
    resp = client.delete(f"/api/mcp/servers/ctx7?base_hash={base}")
    assert resp.status_code == 200
    assert [s["name"] for s in json.loads(manifest.read_text(encoding="utf-8"))["servers"]] == ["notes"]


def test_delete_missing_404(client_env):
    client, manifest = client_env
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}])
    resp = client.delete("/api/mcp/servers/ghost?base_hash=x")
    assert resp.status_code == 404


def test_stale_base_hash_409_disk_untouched(client_env):
    """手编清单后旧页面再提交 → 409 且手改内容一字不动（042 notes 同款事故场景）。"""
    client, manifest = client_env
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}])
    stale = client.get("/api/mcp").json()["hash"]
    _write_manifest(manifest, [{"name": "notes", "command": ["hand-edited"]}])
    resp = client.put("/api/mcp/servers/notes", json=_body(stale))
    assert resp.status_code == 409
    assert json.loads(manifest.read_text(encoding="utf-8"))["servers"][0]["command"] == ["hand-edited"]


def test_write_rejects_command_and_url_both(client_env):
    """写侧不变式：校验失败的请求整体拒绝，磁盘不动、不留 .tmp。"""
    client, manifest = client_env
    _write_manifest(manifest, [{"name": "notes", "command": ["a"]}])
    base = client.get("/api/mcp").json()["hash"]
    before = manifest.read_text(encoding="utf-8")
    resp = client.put("/api/mcp/servers/notes", json=_body(base, command=["a"], url="https://x"))
    assert resp.status_code == 400
    assert "二选一" in resp.json()["detail"]
    assert manifest.read_text(encoding="utf-8") == before
    assert not (manifest.parent / (manifest.name + ".tmp")).exists()


def test_write_on_corrupt_manifest_400(client_env):
    client, manifest = client_env
    manifest.write_text("{broken", encoding="utf-8")
    resp = client.post("/api/mcp/servers/new", json=_body("any"))
    assert resp.status_code == 400
    assert manifest.read_text(encoding="utf-8") == "{broken"   # 坏文件只能手修
