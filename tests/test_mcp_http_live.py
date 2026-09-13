"""MCP-r 真三方验收：Context7 远程服务器（live network，默认跳过）。

本文件是「真三方验收」的第三块拼图（自建服务器 → 官方 SDK 裁判 → 真实产品）：
对着真互联网打，不定时、不免费（无 key 有限速）、CI 不跑。
手动开跑：PYTEST_LIVE_NETWORK=1 .venv/bin/python -m pytest tests/test_mcp_http_live.py -q
"""

import os

import pytest

from agent.tools.mcp_client import register_mcp_tools
from agent.tools.mcp_http import HttpMcpClient
from agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.skipif(
    os.environ.get("PYTEST_LIVE_NETWORK") != "1",
    reason="真网络验收（Context7）需显式 PYTEST_LIVE_NETWORK=1",
)

URL = "https://mcp.context7.com/mcp"


def test_context7_live_acceptance():
    """与真实三方服务器握手、发现、调用、并进注册表（前缀 ctx7__）。"""
    client = HttpMcpClient(URL, timeout=60)
    try:
        names = {t["name"] for t in client.list_tools()}
        assert {"resolve-library-id", "query-docs"} <= names

        text = client.call_tool(
            "resolve-library-id", {"query": "chromadb", "libraryName": "chromadb"}
        )
        assert "chromadb" in text.lower()

        registry = ToolRegistry()
        register_mcp_tools(registry, client, prefix="ctx7__")
        assert "ctx7__resolve-library-id" in registry.names()
    finally:
        client.close()