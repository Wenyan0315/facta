"""MCP-a 验收：手写客户端 × 自建演示服务器的全链路（离线、子进程真实往返）。

不变量：
- tools/list 动态发现 → 工具说明书与注册表要求的形状一致
- 工具调用经 stdio 往返，结果原样拿回
- 业务错误是 isError 语义：客户端抛 McpCallError、注册表转成错误字符串
  （模型可自我纠正，两侧的容错哲学对上了）
- close() 不遗留孤儿进程
"""

import json
import sys
from pathlib import Path

import pytest

from agent.tools.mcp_client import McpCallError, McpClient, register_mcp_tools
from agent.tools.registry import ToolRegistry

SERVER = Path(__file__).resolve().parents[1] / "servers" / "notes_server.py"
SDK_SERVER = Path(__file__).resolve().parents[1] / "servers" / "sdk_server.py"


@pytest.fixture
def client():
    c = McpClient([sys.executable, str(SERVER)])
    yield c
    c.close()


def test_list_tools_discovers_three_tools(client):
    tools = client.list_tools()
    names = {t["name"] for t in tools}
    assert {"read_local_file", "write_local_file", "get_server_time"} <= names
    # 说明书形状 = 注册表想要的（name/description/inputSchema 一个不能少）
    assert all(
        "inputSchema" in t and "description" in t and "name" in t for t in tools
    )


def test_call_time_tool(client):
    result = client.call_tool("get_server_time", {})
    assert result  # 非空时间串


def test_file_roundtrip_through_mcp(client, tmp_path):
    target = tmp_path / "note.md"
    ack = client.call_tool(
        "write_local_file", {"path": str(target), "content": "MCP 语料"}
    )
    assert "已写入" in ack
    text = client.call_tool("read_local_file", {"path": str(target)})
    assert text == "MCP 语料"


def test_business_error_is_mcp_call_error(client):
    with pytest.raises(McpCallError):
        client.call_tool("read_local_file", {"path": "/definitely/not/exist.md"})


def test_registry_serves_mcp_tools_and_errors_as_strings(client):
    registry = ToolRegistry()
    register_mcp_tools(registry, client)

    assert "get_server_time" in registry.names()
    # 菜单形状（OpenAI 格式）含 MCP 工具
    menu_names = {s["function"]["name"] for s in registry.schemas()}
    assert "read_local_file" in menu_names

    # 点菜：正常结果字符串
    assert registry.execute("get_server_time", "{}")
    # 点菜：业务错误 → 错误字符串（模型自我纠正的反馈环）
    bad = registry.execute(
        "read_local_file", json.dumps({"path": "/definitely/not/exist.md"})
    )
    assert bad.startswith("错误")


def test_close_terminates_process():
    c = McpClient([sys.executable, str(SERVER)])
    proc = c._proc
    c.close()
    assert proc.poll() is not None  # 进程已终止，无孤儿


def test_interop_with_official_sdk_server():
    """互操作验收：手写客户端连官方 SDK 实现的服务器（独立裁判）。

    自建服务器与自写客户端共享同一份协议直觉，只有独立实现能检验
    双方是否互相印证了同一个系统性错误。官方 SDK 没装则跳过，
    不炸全绿（importorskip）。
    """
    pytest.importorskip("mcp")
    client = McpClient([sys.executable, str(SDK_SERVER)])
    try:
        names = {t["name"] for t in client.list_tools()}
        assert {"get_server_time", "echo"} <= names
        assert client.call_tool("get_server_time", {}) == "2026-09-12 18:00:00"
        assert client.call_tool("echo", {"text": "roundtrip"}) == "roundtrip"
    finally:
        client.close()