"""MCP-config 验收：清单驱动的服务器装配（数据驱动替写死装配）。

不变量：
- 清单校验：command/url 二选一、缺 name、坏 JSON 都在装载期炸出来而非运行期
- 文件不存在 → 空清单不崩（第一次跑很正常）
- 装配 = 衣服工厂：命令型穿 stdio、URL 型穿 HTTP，prefix 缺省 f"{name}__"
- 单台失败（起不来/撞名）只警告不阻断，其余服务器照常进菜单
- enabled=false 只挂名不拉
"""

import json
from pathlib import Path

import pytest

from facta.tools.mcp_config import assemble_servers, load_server_specs
from facta.tools.registry import ToolRegistry

DEMO = Path(__file__).resolve().parents[1] / "servers" / "http_demo_server.py"


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_file_returns_empty(tmp_path):
    assert load_server_specs(tmp_path / "nope.json") == []


def test_malformed_json_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_server_specs(bad)


def test_command_and_url_are_mutually_exclusive(tmp_path):
    path = _write(tmp_path, {"servers": [{"name": "x", "command": ["a"], "url": "http://x"}]})
    with pytest.raises(ValueError, match="二选一"):
        load_server_specs(path)
    path = _write(tmp_path, {"servers": [{"name": "x"}]})
    with pytest.raises(ValueError, match="必须给一个"):
        load_server_specs(path)


def test_default_prefix_is_name_based(tmp_path):
    path = _write(tmp_path, {"servers": [{"name": "foo", "command": ["python", "x.py"]}]})
    specs = load_server_specs(path)
    assert specs[0].prefix == "foo__"


def test_assemble_url_server_into_registry(tmp_path):
    """URL 型服务器装配：以自带 HTTP 演示服务器为接入目标（子进程真实往返）。"""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("http_demo_server", DEMO)
    module = importlib.util.module_from_spec(spec)
    sys.modules["http_demo_server"] = module
    spec.loader.exec_module(module)
    server, thread = module.start(0)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        path = _write(tmp_path, {"servers": [{"name": "demo", "url": url}]})
        registry = ToolRegistry()
        clients = assemble_servers(registry, load_server_specs(path))
        try:
            assert "demo__echo_server" in registry.names()
            assert registry.execute("demo__echo_server", json.dumps({"text": "hi"})) == json.dumps({"text": "hi"})
        finally:
            for c in clients:
                c.close()
    finally:
        module.stop(server, thread)


def test_dead_server_does_not_block_others(tmp_path, caplog):
    """一台起不来（不存在的命令）→ 警告并跳过，装配循环不崩。"""
    import logging
    path = _write(tmp_path, {"servers": [{"name": "ghost", "command": ["/definitely/not/a/binary"]}]})
    registry = ToolRegistry()
    with caplog.at_level(logging.WARNING):
        clients = assemble_servers(registry, load_server_specs(path))
    assert clients == []
    assert "ghost" not in registry.names()
    assert "失败" in caplog.text


def test_python_placeholder_resolves_to_current_interpreter(tmp_path):
    """占位符 {python} → sys.executable：命令型服务器用配置驱动也能真连。"""
    notes = Path(__file__).resolve().parents[1] / "servers" / "notes_server.py"
    path = _write(tmp_path, {"servers": [{"name": "notes", "command": ["{python}", str(notes)]}]})
    registry = ToolRegistry()
    clients = assemble_servers(registry, load_server_specs(path))
    try:
        assert "notes__get_server_time" in registry.names()
        assert registry.execute("notes__get_server_time", "{}")
    finally:
        for c in clients:
            c.close()


def test_name_collision_idempotent_and_skipped(tmp_path):
    """同一 URL 同一前缀登记两次 → 第二次撞名被拒（拒绝而非静默覆盖）。"""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("http_demo_server", DEMO)
    module = importlib.util.module_from_spec(spec)
    sys.modules["http_demo_server"] = module
    spec.loader.exec_module(module)
    server, thread = module.start(0)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        path = _write(tmp_path, {"servers": [{"name": "a", "url": url, "prefix": "same__"}, {"name": "b", "url": url, "prefix": "same__"}]})
        registry = ToolRegistry()
        clients = assemble_servers(registry, load_server_specs(path))
        try:
            assert len(clients) == 1   # 第一个进了，第二个撞前缀被拒
            names = registry.names()
            assert len([n for n in names if n.startswith("same__")]) == 3   # 三个工具只登记一遍
        finally:
            for c in clients:
                c.close()
    finally:
        module.stop(server, thread)
