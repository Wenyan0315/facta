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
import os
import queue
import subprocess
import threading
import time

from agent.tools.registry import Tool, ToolRegistry


class McpError(RuntimeError):
    """MCP 通信层错误（连接断 / 超时 / 协议违例）。"""


class McpCallError(McpError):
    """工具已被服务器执行、但返回了业务错误（isError=true）。"""


def join_text_content(result: dict) -> str:
    """MCP 工具结果统一收口：content 多段文本合并成一段字符串。

    stdio 与 HTTP 两个客户端共享（MCP-r）——协议层的产物形状是同一套，
    收口逻辑不该各写一份（两个真值源会漂移）。
    """
    texts = [
        item.get("text", "")
        for item in result.get("content", [])
        if item.get("type") == "text"
    ]
    return "\n".join(texts)


class McpClient:
    """最小 MCP 客户端：启动服务器子进程，同步请求-响应。"""

    def __init__(
        self,
        command: list[str],
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._timeout = timeout
        # stderr 直接丢弃：不读它会让子进程的日志写满管道缓冲区把双方卡死。
        # 调试期可改为重定向到文件（open(path, "w")）——防死锁同时留证据
        # env：在继承的基础上叠加（沙箱目录注入、将来真实服务器的 API key 都走这条缝）
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env={**os.environ, **(env or {})},
        )
        # 启动探活：服务器起不来（脚本路径错/依赖缺）就立刻报，别等 initialize
        # 空等超时（三方评审第 5 条）。100ms 沉降期：Popen 刚返回时子进程可能
        # 还没来得及退出，立刻 poll 有竞态——健康服务器多等 100ms 无感
        time.sleep(0.1)
        if self._proc.poll() is not None:
            raise McpError(
                f"MCP 服务器启动失败（退出码 {self._proc.returncode}）：{' '.join(command)}"
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
        """同步请求：发带 id 的消息，等到同 id 的响应（跳过通知与他人响应）。

        超时语义：每等一条消息最多 timeout 秒，不是整次调用总超时——
        服务器若持续吐无关消息，总等待可超 timeout。已知边界，当前够用。
        """
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

    def is_alive(self) -> bool:
        """服务器进程还活着吗——点菜前的快探（MCP-c ①）。

        服务器中途挂掉时，若直接 call 会撞 _request 的 30s 超时才醒。
        先用 poll 看一眼：死了直接快速失败，不让 agent 对着死进程干等。
        """
        return self._proc.poll() is None

    def call_tool(self, name: str, arguments: dict) -> str:
        """转发一次模型点菜；多段文本合并返回；业务错误抛 McpCallError。"""
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        text = join_text_content(result)
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


DEFAULT_PREFIX = "mcp__"
DEAD_TOOL_EVICT_AFTER = 2   # 同一工具连续碰到「服务器已死」几次就摘牌


def register_mcp_tools(
    registry: ToolRegistry,
    client: McpClient,
    prefix: str = DEFAULT_PREFIX,
) -> None:
    """把服务器交出的工具登记进 ToolRegistry——与内置工具同等待遇。

    冲突治理（MCP-b）：外部工具统一加前缀。动机是 registry.register 的
    语义——「重名后者覆盖前者」：若无前缀且 MCP 后注册，外部工具会
    顶掉内置工具（write_note 的四道栅栏被 write_local_file 换掉！）。
    前缀让外部工具装不成内置；连前缀都撞（有人故意撞名）则抛错拒绝，
    静默覆盖等于把菜单卖给外部进程。

    顽健性（MCP-c）：服务器中途挂掉时对模型暴露的两个出口——
    ① 点菜前 is_alive 快探：死进程直接回「离线」错误，不干等 30s 超时；
    ② 死菜摘牌：同一工具连续 DEAD_TOOL_EVICT_AFTER 次碰到死服务器，
    从菜单摘除并告知模型——模型看不到死菜，自然不会反复撞墙。
    """
    fail_counts: dict[str, int] = {}   # 注册名 → 连续碰到死服务器的次数

    def _report_dead(register_name: str) -> str:
        """服务器确认已死：计一次数；够阈值就把死菜从菜单摘掉。"""
        fail_counts[register_name] = fail_counts.get(register_name, 0) + 1
        message = f"MCP 服务器已离线，无法调用 {register_name}"
        if fail_counts[register_name] >= DEAD_TOOL_EVICT_AFTER:
            registry.unregister(register_name)
            message += "；该工具已从菜单移除"
        return message

    for mcp_tool in client.list_tools():
        server_name = mcp_tool["name"]          # 服务器端的真名（call 要用）
        register_name = f"{prefix}{server_name}"  # 菜单里的前缀名（防撞）
        if register_name in registry.names():
            raise McpError(
                f"MCP 工具名冲突：{register_name} 已被占用"
                "——前缀是标准防撞配置，撞名说明有人故意为之，须人工裁定"
            )
        schema = mcp_tool.get("inputSchema") or {}
        # 全量透传，不挑字段：title/anyOf/$defs/枚举等一旦被白名单挑丢，
        # schema 就残缺——模型按残缺菜单生成的参数会被服务器拒收。
        # 只会补默认值，绝不删键（复杂 schema 是三方服务器的常态）。
        parameters = dict(schema)
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})

        def _func(
            client=client,
            server_name=server_name,
            register_name=register_name,
            **args,
        ) -> str:
            # 默认参数锚定：闭包捕获的是「值」不是循环变量
            # （List identity trap 的表亲——循环里造闭包，变量必须钉住）
            # 注意锚的是 server_name（原真名）——服务器不认识前缀名
            if not client.is_alive():
                # 返回消息而非抛异常：registry 转的通用错误壳会丢掉摘牌告知
                return _report_dead(register_name)
            try:
                return client.call_tool(server_name, args)
            except McpError:
                if not client.is_alive():
                    # 死在执行途中（断开被哨兵/超时捕获）：同样计一次
                    return _report_dead(register_name)
                raise   # 服务器活着的业务错误/协议错误：照旧交给 registry 转字符串

        registry.register(
            Tool(
                name=register_name,
                description=mcp_tool.get("description", ""),
                parameters=parameters,
                func=_func,
            )
        )