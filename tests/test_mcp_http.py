"""MCP-r 验收：HTTP 客户端 × 自建 HTTP 演示服务器（纯本地，秒级往返）。

不变量：
- 会话头回传：initialize 响应抓回 mcp-session-id，后续每个请求带回
  （演示服务器对缺头的请求一律 401——忘了回传，这里当场炸）
- SSE 分帧响应解析成结果（sse_echo），普通 JSON 响应照常（echo_server）
- 业务错误同 stdio 版语义：isError → 抛 McpCallError / registry 转错误字符串
- 健康度：连接级失败置死（is_alive False）→ register_mcp_tools 的
  死菜摘牌计数器**原样复用**（远程服务器持续宕机同样被摘牌）
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from facta.tools.mcp_client import McpCallError, McpError, register_mcp_tools
from facta.tools.mcp_http import HttpMcpClient
from facta.tools.registry import ToolRegistry

SERVER = Path(__file__).resolve().parents[1] / "servers" / "http_demo_server.py"


def _load_server_module():
    spec = importlib.util.spec_from_file_location("http_demo_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["http_demo_server"] = module
    spec.loader.exec_module(module)
    return module


_demo = _load_server_module()


@pytest.fixture
def url():
    server, thread = _demo.start(0)
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    _demo.stop(server, thread)


@pytest.fixture
def client(url):
    c = HttpMcpClient(url, timeout=10)
    yield c
    c.close()


def test_handshake_and_list_tools(client):
    # 能完成握手即证明会话头回传正确（演示服务器对缺头请求 401）
    names = {t["name"] for t in client.list_tools()}
    assert {"echo_server", "sse_echo", "fail"} <= names


def test_call_tool_roundtrip_json(client):
    result = client.call_tool("echo_server", {"text": "你好"})
    assert result == json.dumps({"text": "你好"}, ensure_ascii=False)


def test_sse_framed_response_is_parsed(client):
    """SSE 分帧响应（text/event-stream）与普通 JSON 响应对调用方无差别。"""
    result = client.call_tool("sse_echo", {"text": "流式帧"})
    assert result == json.dumps({"text": "流式帧"}, ensure_ascii=False)


def test_business_error_raises_mcp_call_error(client):
    with pytest.raises(McpCallError):
        client.call_tool("fail", {})


def test_registry_serves_http_tools_with_prefix(client):
    registry = ToolRegistry()
    register_mcp_tools(registry, client, prefix="ctx7__")
    assert "ctx7__echo_server" in registry.names()
    assert registry.execute("ctx7__echo_server", json.dumps({"text": "hi"})) == json.dumps({"text": "hi"})


def test_connection_failure_flags_unhealthy():
    """连接级失败置死健康度：成功时 is_alive True，撞死后 False。"""
    server, thread = _demo.start(0)
    port = server.server_address[1]
    client = HttpMcpClient(f"http://127.0.0.1:{port}", timeout=10)
    try:
        assert client.is_alive()
        _demo.stop(server, thread)   # 真死：socket 都关掉，后续请求「拒连」而非白等
        with pytest.raises(McpError):
            client.list_tools()
        assert not client.is_alive()
    finally:
        client.close()


def test_dead_remote_server_eviction_reuses_stdio_logic():
    """远程服务器宕机 = 摘牌计数器的第二战场：同一套「死菜摘牌」原样生效。"""
    server, thread = _demo.start(0)
    port = server.server_address[1]
    client = HttpMcpClient(f"http://127.0.0.1:{port}", timeout=10)
    registry = ToolRegistry()
    register_mcp_tools(registry, client, prefix="mcp__")
    try:
        _demo.stop(server, thread)   # 服务器真死：后续请求「拒连」，秒级失败

        # 第 1 招：健康度还挂着 True（上次是成功的），call 撞死 → 置死 + 计一次账
        first = registry.execute("mcp__echo_server", json.dumps({"text": "x"}))
        assert "离线" in first
        assert "mcp__echo_server" in registry.names()

        # 第 2 招：is_alive 已 False → 直接计账到阈值 → 摘牌
        second = registry.execute("mcp__echo_server", json.dumps({"text": "x"}))
        assert "从菜单移除" in second
        assert "mcp__echo_server" not in registry.names()

        # 第 3 招：死菜已不在菜单，回到常规「无此工具」反馈
        third = registry.execute("mcp__echo_server", json.dumps({"text": "x"}))
        assert third.startswith("错误")
    finally:
        client.close()
