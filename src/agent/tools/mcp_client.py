"""MCP 客户端（手写最小实现）：通过 stdio 与外部工具服务器对话。

MCP（Model Context Protocol）让工具从「自己写」变成「外面长出来」：
启动一个服务器进程，它的工具自动出现在 agent 的菜单里。

协议三层（「动态发现」的全部机制）：
  initialize  → 握手，服务器自报能力
  tools/list  → 服务器交出工具说明书（name/description/inputSchema）
  tools/call  → 按模型点菜转发调用，拿回结果

为什么手写而不是用官方 SDK：SDK 把协议封装得看不见，手写这三层
才知道「动态发现的机制」长什么样（从简到真——教学版手写，工业版换 SDK）。

实现原理（本文件就是这些原理的代码形态）：
  1. 子进程 + 双管道（stdin 写入 / stdout 读取）
  2. 换行分帧：每条消息一行 JSON + flush
  3. JSON-RPC 2.0：靠 id 把响应配对到请求；无 id 的是通知（无人回应）
  4. 后台读线程 + 队列：stdout 读取是阻塞的，交给专职线程，请求线程按 id 对号取件
  5. 哨兵 None：服务器断开（EOF）时唤醒所有在途请求
  6. 超时：外部进程的调用不能让 agent 陪葬（横切所有外部调用，同 b 段）

企业级路径：将来接远程服务器或切官方 SDK 时，只换本模块内部——
register_mcp_tools 的接口与 ToolRegistry 的形状不动（换件不换衣服）。
"""

import json
import queue
import subprocess
import threading

from agent.tools.registry import Tool, ToolRegistry


class McpError(RuntimeError):
    """MCP 通信层错误（连接断 / 超时 / 协议违例）。"""


class McpCallError(McpError):
    """工具已被服务器执行、但返回了业务错误（isError=true）。"""


class McpClient:
    """最小 MCP 客户端：启动服务器子进程，同步请求-响应。"""

    def __init__(self, command: list[str], timeout: float = 30.0) -> None:
        self._timeout = timeout
        # stderr 直接丢弃：不读它会让子进程的日志写满管道缓冲区把双方卡死
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self._queue: "queue.Queue[dict | None]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._next_id = 1

        # 握手：initialize（要响应）+ notifications/initialized（通知，无 id 无响应）
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "personal-agent", "version": "0.9"},
            },
        )
        self._notify("notifications/initialized", {})

    # ---- 管道与分帧 ----

    def _send(self, payload: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read_loop(self) -> None:
        """后台读线程：每行一个 JSON 入队；管道 EOF 时放哨兵 None。"""
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._queue.put(json.loads(line))
            except json.JSONDecodeError:
                continue  # 垃圾行忽略，别让一条坏消息杀掉通道
        self._queue.put(None)  # 哨兵：服务器已断开

    # ---- JSON-RPC ----

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict) -> dict:
        """同步请求：发带 id 的消息，等到同 id 的响应（跳过通知与他人响应）。"""
        req_id = self._next_id
        self._next_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        while True:
            try:
                msg = self._queue.get(timeout=self._timeout)
            except queue.Empty:
                raise McpError(f"MCP 调用超时（{method}，>{self._timeout}s 无响应）")
            if msg is None:
                raise McpError(f"MCP 服务器已断开连接（{method} 在途）")
            if msg.get("id") != req_id:
                continue  # 通知或别人的响应：跳过，继续等自己的号
            if "error" in msg:
                err = msg["error"]
                raise McpError(f"MCP 协议错误（{method}）：{err.get('message', err)}")
            return msg.get("result", {})

    # ---- 对外能力 ----

    def list_tools(self) -> list[dict]:
        """服务器交出的工具说明书清单——「动态发现」的全部秘密。"""
        return self._request("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        """转发一次模型点菜；多段文本合并返回；业务错误抛 McpCallError。"""
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        texts = [
            item.get("text", "")
            for item in result.get("content", [])
            if item.get("type") == "text"
        ]
        text = "\n".join(texts)
        if result.get("isError"):
            raise McpCallError(text or f"{name} 返回了错误")
        return text

    def close(self) -> None:
        """关掉服务器进程——退出时调用，不留孤儿进程。"""
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def register_mcp_tools(registry: ToolRegistry, client: McpClient) -> None:
    """把服务器交出的工具登记进 ToolRegistry——与内置工具同等待遇。

    MCP-a：工具名直接采用（演示服务器与内置工具名不冲突）。
    冲突治理（前缀命名/覆盖策略）是 MCP-b 的活。
    """
    for mcp_tool in client.list_tools():
        name = mcp_tool["name"]
        schema = mcp_tool.get("inputSchema") or {}
        parameters = {
            "type": schema.get("type", "object"),
            "properties": schema.get("properties", {}),
        }
        if schema.get("required"):
            parameters["required"] = schema["required"]

        def _func(client=client, name=name, **args) -> str:
            # 默认参数锚定：闭包捕获的是「值」不是循环变量
            # （List identity trap 的表亲——循环里造闭包，变量必须钉住）
            return client.call_tool(name, args)

        registry.register(
            Tool(
                name=name,
                description=mcp_tool.get("description", ""),
                parameters=parameters,
                func=_func,
            )
        )