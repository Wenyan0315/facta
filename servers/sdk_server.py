"""MCP 官方 SDK（FastMCP）实现的独立服务器——互操作验收的「裁判」。

与项目源代码无关：协议正确性由官方 SDK 背书。我们的手写客户端能不能
连上它，就是「自写两端是否互相印证了同一个系统性错误」的判据——
自建服务器和自写客户端共享同一份协议直觉，只有独立实现才能当裁判。

运行：python servers/sdk_server.py（stdio 传输）
依赖：pip install mcp（dev 可选依赖组已登记）
"""

from mcp.server.mcpserver import MCPServer  # mcp 2.x：FastMCP 已更名 MCPServer

mcp = MCPServer("sdk-demo")


@mcp.tool()
def get_server_time() -> str:
    """返回固定时间——便于测试断言，不依赖真实时钟。"""
    return "2026-09-12 18:00:00"


@mcp.tool()
def echo(text: str) -> str:
    """原样返回输入文本。"""
    return text


if __name__ == "__main__":
    mcp.run()