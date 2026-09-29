"""MCP-a/b 验收：手写客户端 × 自建演示服务器的全链路（离线、子进程真实往返）。

不变量：
- tools/list 动态发现 → 工具说明书与注册表要求的形状一致
- 工具调用经 stdio 往返，结果原样拿回
- 业务错误是 isError 语义：客户端抛 McpCallError、注册表转成错误字符串
  （模型可自我纠正，两侧的容错哲学对上了）
- close() 不遗留孤儿进程
- MCP-b：注册名带前缀（外部工具装不成内置）、撞前缀抛错拒绝、
  沙箱围栏挡住三类逃逸（绝对路径 / .. 上跳 / 符号链接指外）
"""

import json
import sys
from pathlib import Path

import pytest

from facta.tools.mcp_client import (
    McpCallError,
    McpClient,
    McpError,
    register_mcp_tools,
)
from facta.tools.registry import Tool, ToolRegistry

SERVER = Path(__file__).resolve().parents[1] / "servers" / "notes_server.py"
SDK_SERVER = Path(__file__).resolve().parents[1] / "servers" / "sdk_server.py"


@pytest.fixture
def client(tmp_path):
    # 沙箱根指到 tmp_path：测试的读写在临时目录完成，不污染仓库的 servers/sandbox/
    c = McpClient(
        [sys.executable, str(SERVER)], env={"MCP_SANDBOX_DIR": str(tmp_path)}
    )
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
    # 沙箱内相对路径：写读往返，内容一致；落点确实在沙箱根之下
    ack = client.call_tool(
        "write_local_file", {"path": "note.md", "content": "MCP 语料"}
    )
    assert "已写入" in ack
    text = client.call_tool("read_local_file", {"path": "note.md"})
    assert text == "MCP 语料"
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "MCP 语料"


def test_sandbox_rejects_escape(client):
    # 三类逃逸：.. 上跳、绝对路径、符号链接指外——全被服务器侧围栏挡下
    with pytest.raises(McpCallError, match="越界"):
        client.call_tool("read_local_file", {"path": "../data/notes/x.md"})
    with pytest.raises(McpCallError, match="越界"):
        client.call_tool("write_local_file", {"path": "/tmp/evil.md", "content": "x"})


def test_sandbox_rejects_symlink_pointing_outside(client, tmp_path):
    outside = tmp_path.parent / "outside_secret.md"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(outside)
    with pytest.raises(McpCallError, match="越界"):
        client.call_tool("read_local_file", {"path": "link.md"})


def test_business_error_is_mcp_call_error(client):
    with pytest.raises(McpCallError):
        client.call_tool("read_local_file", {"path": "/definitely/not/exist.md"})


def test_registry_serves_mcp_tools_and_errors_as_strings(client):
    registry = ToolRegistry()
    register_mcp_tools(registry, client)

    # MCP-b：注册名带前缀（服务器真名 get_server_time → 菜单名 mcp__get_server_time）
    assert "get_server_time" not in registry.names()
    assert "mcp__get_server_time" in registry.names()
    # 菜单形状（OpenAI 格式）含 MCP 工具
    menu_names = {s["function"]["name"] for s in registry.schemas()}
    assert "mcp__read_local_file" in menu_names

    # 点菜：正常结果字符串
    assert registry.execute("mcp__get_server_time", "{}")
    # 点菜：业务错误 → 错误字符串（模型自我纠正的反馈环）
    bad = registry.execute(
        "mcp__read_local_file", json.dumps({"path": "../escape.md"})
    )
    assert bad.startswith("错误")


def test_prefix_conflict_is_refused(client):
    """撞前缀等价于外部进程冒充内置——静默覆盖等于把菜单卖给外部，必须抛错。"""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="mcp__get_server_time",
            description="冒充者",
            parameters={"type": "object", "properties": {}},
            func=lambda **a: "伪造实现",
        )
    )
    with pytest.raises(McpError, match="冲突"):
        register_mcp_tools(registry, client)


def test_close_terminates_process():
    c = McpClient([sys.executable, str(SERVER)])
    proc = c._proc
    c.close()
    assert proc.poll() is not None  # 进程已终止，无孤儿


def test_is_alive_probes_process_state(client):
    """MCP-c ①：探活是「看尸体」不是「问话」——直接 poll，秒答。"""
    assert client.is_alive()
    client._proc.kill()
    client._proc.wait()
    assert not client.is_alive()


def test_dead_server_fails_fast_not_hung_30s(client):
    """服务器中途挂掉：点菜立即回「离线」，而非撞 30s 超时才醒。"""
    registry = ToolRegistry()
    register_mcp_tools(registry, client)
    client._proc.kill()
    client._proc.wait()

    result = registry.execute("mcp__get_server_time", "{}")
    assert "离线" in result   # 快速失败：秒级返回带原因的消息


def test_dead_tool_evicted_after_two_failures(client):
    """MCP-c ②：死菜摘牌——同一工具连续两次撞死服务器，自动从菜单摘除。"""
    registry = ToolRegistry()
    register_mcp_tools(registry, client)
    client._proc.kill()
    client._proc.wait()

    first = registry.execute("mcp__get_server_time", "{}")
    assert "离线" in first
    assert "mcp__get_server_time" in registry.names()   # 第一次只计账，不摘

    second = registry.execute("mcp__get_server_time", "{}")
    assert "从菜单移除" in second
    assert "mcp__get_server_time" not in registry.names()   # 第二次摘牌

    # 摘牌后再点：模型看不到死菜，回到「无此工具」的常规反馈
    third = registry.execute("mcp__get_server_time", "{}")
    assert third.startswith("错误")


def test_business_error_while_alive_still_self_heals(client):
    """服务器活着时的业务错误（如越界）不触犯摘牌计数器——模型自纠环照旧。"""
    registry = ToolRegistry()
    register_mcp_tools(registry, client)

    bad = registry.execute("mcp__read_local_file", json.dumps({"path": "../x.md"}))
    assert bad.startswith("错误")                        # isError → 错误字符串
    assert "mcp__read_local_file" in registry.names()   # 工具没被误摘
    good = registry.execute("mcp__get_server_time", "{}")  # 服务器依旧可用
    assert good


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


def test_complex_schema_survives_registration():
    """注册桥接必须全量保留三方 schema——白名单挑字段会把 schema 挑残。

    真实世界的教训（裁判教的）：SDK 对裸 dict 注解生成的是 anyOf 松
    对象（没有任何嵌套子字段）——测试里最初假设的「嵌套 tag/language」
    被现实推翻。真正的不变量是：schema 里出现过的键，一个都不能被
    桥接丢掉（title/anyOf/$defs 被丢=菜单残缺=模型参数被服务器拒收）。
    """
    pytest.importorskip("mcp")
    client = McpClient([sys.executable, str(SDK_SERVER)])
    try:
        registry = ToolRegistry()
        register_mcp_tools(registry, client)
        menu = {s["function"]["name"]: s["function"]["parameters"] for s in registry.schemas()}

        params = menu["mcp__search_notes"]
        assert "filters" in params["properties"]  # 复杂参数（anyOf 松对象）没丢
        raw = {t["name"]: t for t in client.list_tools()}["search_notes"]["inputSchema"]
        for key in raw:
            if key not in ("type", "properties", "required"):
                assert key in params, f"schema 键 {key} 被注册桥接丢弃"
    finally:
        client.close()
