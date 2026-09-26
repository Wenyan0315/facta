"""MCP HTTP 演示服务器：纯标准库、零 agent 依赖（MCP-r 的测试 fixture 与教学参照物）。

streamable HTTP 协议的最小子集，刻意立下的行为规则：
- initialize 响应头回 mcp-session-id；**之后的任何非 initialize 请求缺这个头
  一律 401**——钉死客户端的会话头回传（客户端忘了带，测试当场炸）
- 工具：echo_server（原样回参）/ fail（isError 业务错误）/ sse_echo（SSE 分帧返回）
- POST /shutdown 收摊（测试拆卸用）

独立运行：python servers/http_demo_server.py [port]（默认 8760）
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION = "demo-session-1"
DEFAULT_PORT = 8760

TOOLS = [
    {
        "name": "echo_server",
        "description": "把参数原样回显（走普通 JSON 响应）。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "sse_echo",
        "description": "把参数原样回显（走 SSE 分帧响应）。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "永远返回业务错误（isError=true）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Handler(BaseHTTPRequestHandler):
    def _read(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n))
        except (ValueError, json.JSONDecodeError):
            return {}

    def _reply(self, payload, status=200, ctype="application/json", extra=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _ok(self, msg, result):
        self._reply({"jsonrpc": "2.0", "id": msg.get("id"), "result": result})

    def do_POST(self):
        if self.path == "/shutdown":
            self._reply({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        msg = self._read()
        method = msg.get("method", "")
        if method != "initialize" and self.headers.get("mcp-session-id") != SESSION:
            # 会话头哨卡：没带对 mcp-session-id 的请求一律 401
            self._reply(
                {"jsonrpc": "2.0", "id": msg.get("id"),
                 "error": {"code": -32000, "message": "mcp-session-id 缺失或错误"}},
                401,
            )
            return
        if method == "initialize":
            self._reply(
                {"jsonrpc": "2.0", "id": msg.get("id"),
                 "result": {"protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "http-demo-server", "version": "0.1.0"}}},
                extra={"Mcp-Session-Id": SESSION},
            )
        elif method == "notifications/initialized":
            self._reply(None, 202)
        elif method == "tools/list":
            self._ok(msg, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {})
            if name == "fail":
                self._ok(msg, {"content": [{"type": "text", "text": "业务失败了"}], "isError": True})
            elif name == "sse_echo":
                payload = {"jsonrpc": "2.0", "id": msg.get("id"),
                           "result": {"content": [{"type": "text",
                                                   "text": json.dumps(args, ensure_ascii=False)}]}}
                body = f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif name == "echo_server":
                self._ok(msg, {"content": [{"type": "text",
                                            "text": json.dumps(args, ensure_ascii=False)}]})
            else:
                self._ok(msg, {"content": [{"type": "text", "text": f"未知工具 {name}"}],
                               "isError": True})
        else:
            self._reply(
                {"jsonrpc": "2.0", "id": msg.get("id"),
                 "error": {"code": -32601, "message": f"未知方法 {method}"}},
                404,
            )

    def log_message(self, *args):   # 静音测试日志
        pass


def start(port: int = 0):
    """启动服务器线程，返回 (server, thread)；port=0 时系统挑可用口。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop(server, thread):
    """正确收摊：shutdown 只停 serve_forever 循环，监听 socket 还活着——
    连接能建立但没人应答，客户端会白等到超时（死而不僵）。
    必须补 server_close() 真关 socket，客户端才能立刻收到「拒连」。
    """
    server.shutdown()
    server.server_close()
    thread.join()


def main():
    # 端口解析放这里而不是模块级：被测试 import 时 sys.argv 是 pytest 的，
    # 模块级读取会撞车（ValueError: invalid literal）
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server, _ = start(port)
    print(f"HTTP 演示服务器已启动：http://127.0.0.1:{server.server_address[1]}")
    server.serve_forever()


if __name__ == "__main__":
    main()
