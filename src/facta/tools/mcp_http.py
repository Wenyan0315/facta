"""MCP 远程客户端（MCP-r）：streamable HTTP 传输——把「外部工具」从子进程扩到网络。

stdio 版（mcp_client.py）学的是「外面的进程」；本模块学的是「外面的服务器」。
Context7 实战验收的产物（2026-09-13）：手写客户端长出第 8 层——HTTP 传输。

协议要点（streamable HTTP）：
  1. 单一端点反复 POST；JSON-RPC 消息体不变——协议层与传输层分离
  2. initialize 响应头回 mcp-session-id；之后每个请求都得带回——
     HTTP 是无状态的，「记住我是谁」全靠这个头
  3. 通知（无 id）→ 202 空正文；请求 → 200 带结果
  4. 服务器可选 SSE：响应用 text/event-stream 分帧投递 data: 行——
     「流式响应」在项目里的首次现身（streaming 里程碑的远亲）
  5. 结束发 DELETE 通知服务器收摊（尽力而为，不较真）

健康语义与 stdio 不同：HTTP 没有进程尸体可 poll——is_alive() =
最近一次调用没撞「连接级失败」（网络错/5xx 置死，任何一次成功复活）。
摘牌计数器（register_mcp_tools）原样复用：远程服务器持续宕机时，
死菜照样被摘，模型照样不再撞墙。

接口与 McpClient（stdio）同构：list_tools / call_tool / is_alive / close——
register_mcp_tools 一行不改（接口同构、实现异构——换件不换衣服的第三件衣服）。
"""

import contextlib
import json

import httpx

from agent.tools.mcp_client import McpCallError, McpError, join_text_content


class HttpMcpClient:
    """最小远程 MCP 客户端：同步请求-响应，接口与 stdio 版同构。"""

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url
        self._session_id: str | None = None
        self._healthy = True
        self._next_id = 1
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **(headers or {}),   # 将来带 API key（Context7 正式量需要）走这条缝
            },
        )
        # 握手：initialize（带会话头回来）+ notifications/initialized（通知）
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "personal-agent", "version": "0.9"},
            },
        )
        self._notify("notifications/initialized", {})

    # ---- 传输层 ----

    def _send(self, payload: dict) -> list[dict]:
        """发一次 POST，按 content-type 解包响应；顺手维护会话头与健康度。

        返回消息列表而非单个 dict：SSE 可能分帧投递多条，统一成列表让
        _request 按 id 挑自己的那条；JSON/空正文则列表长 0 或 1。
        """
        headers = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        try:
            resp = self._http.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # 连接级失败（DNS/拒连/超时）：服务器死没死不知道，先按死处理
            self._healthy = False
            raise McpError(f"HTTP 连接失败（{type(exc).__name__}: {exc}）") from exc

        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if resp.status_code >= 500:
            # 5xx = 服务器侧的错：健康度置死，摘牌计数器可复用
            self._healthy = False
            raise McpError(f"HTTP {resp.status_code}：{resp.text[:200]}")
        if resp.status_code >= 400:
            # 4xx = 我们这边的错（如 401 没带 key）：服务器活着，别冤杀
            self._healthy = True
            raise McpError(f"HTTP {resp.status_code}：{resp.text[:200]}")
        self._healthy = True

        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return self._parse_sse(resp.text)
        if resp.status_code == 202 or not resp.text.strip():
            return []
        try:
            return [resp.json()]
        except json.JSONDecodeError as exc:
            raise McpError(f"响应不是合法 JSON：{resp.text[:200]}") from exc

    @staticmethod
    def _parse_sse(text: str) -> list[dict]:
        """SSE 分帧：data: 行攒成完整 JSON 消息；空行 flush 一帧。"""
        messages: list[dict] = []
        data_lines: list[str] = []

        def flush() -> None:
            if data_lines:
                # 心跳/注释帧解析失败：忽略，别让一条坏帧杀掉通道
                with contextlib.suppress(json.JSONDecodeError):
                    messages.append(json.loads("\n".join(data_lines)))
                data_lines.clear()

        for line in text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "":
                flush()
        flush()
        return messages

    # ---- JSON-RPC 层（与 stdio 版同一套语义，只是没有读线程）----

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict) -> dict:
        """同步请求：发带 id 的消息，在响应列表里挑自己 id 的那条。"""
        req_id = self._next_id
        self._next_id += 1
        responses = self._send(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        for msg in responses:
            if msg.get("id") != req_id:
                continue   # 别的帧（心跳/他人响应）：跳过
            if "error" in msg:
                err = msg["error"]
                raise McpError(f"MCP 协议错误（{method}）：{err.get('message', err)}")
            return msg.get("result", {})
        raise McpError(f"MCP 响应缺失或未配对（{method}，期望 id {req_id}）")

    # ---- 对外能力（与 stdio 版同构）----

    def list_tools(self) -> list[dict]:
        """服务器交出的工具说明书清单——「动态发现」的全部秘密。"""
        return self._request("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        """转发一次模型点菜；多段文本合并返回；业务错误抛 McpCallError。"""
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        text = join_text_content(result)
        if result.get("isError"):
            raise McpCallError(text or f"{name} 返回了错误")
        return text

    def is_alive(self) -> bool:
        """健康度：最近一次调用没撞连接级失败（无进程尸体可 poll，就用这个）。"""
        return self._healthy

    def close(self) -> None:
        """发 DELETE 通知服务器收摊（尽力而为），再关连接。"""
        # 服务器已经死了还讲什么礼貌——收反射动作别较真
        with contextlib.suppress(httpx.HTTPError):
            self._http.request("DELETE", self._url)
        self._http.close()
