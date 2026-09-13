"""MCP 演示服务器：文件读写 + 当前时间（随 repo 走的接入目标与测试 fixture）。

独立进程从 stdin 读 JSON-RPC 行、向 stdout 写响应——不 import 任何 agent
源码，任何 Python 直跑：python servers/notes_server.py

实现协议最小子集：initialize / notifications/initialized / tools/list /
tools/call。业务错误以 isError=true 返回（不进程崩溃）。
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 沙箱根目录（MCP-b 评审#1）：「栅栏长在服务器侧」——能力提供者自守边界，
# 客户端只能做菜单层尽力而为（"什么路径算越界"是服务器的业务知识）。
# 环境变量可覆盖：测试把沙箱指到 tmp_path，避免污染仓库。
SANDBOX_ROOT = Path(
    os.environ.get(
        "MCP_SANDBOX_DIR", str(Path(__file__).resolve().parent / "sandbox")
    )
).resolve()


def _in_sandbox(raw: str) -> Path:
    """把请求路径锁进沙箱：resolve 后再判根前缀，一举挡住三类逃逸——
    绝对路径（Path/abs 除法会吞掉前缀）、`..` 上跳、符号链接指外。"""
    target = (SANDBOX_ROOT / raw).resolve()
    if target != SANDBOX_ROOT and not target.is_relative_to(SANDBOX_ROOT):
        raise PermissionError(f"路径越界：仅允许访问沙箱目录 {SANDBOX_ROOT}")
    return target


def _read_file(args: dict) -> str:
    path = _in_sandbox(str(args.get("path", "")))
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    return path.read_text(encoding="utf-8")


def _write_file(args: dict) -> str:
    path = _in_sandbox(str(args.get("path", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args.get("content", ""), encoding="utf-8")
    return f"已写入 {path}"


def _server_time(args: dict) -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


TOOLS = [
    {
        "name": "read_local_file",
        "description": "读取本地文件的全文内容。当需要查看某个文件的原始内容时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径"}},
            "required": ["path"],
        },
        "handler": _read_file,
    },
    {
        "name": "write_local_file",
        "description": "把内容写入本地文件（覆盖写）。需要持久化生成的内容时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的全文内容"},
            },
            "required": ["path", "content"],
        },
        "handler": _write_file,
    },
    {
        "name": "get_server_time",
        "description": "返回服务器当前时间。",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _server_time,
    },
]


def _reply(msg: dict, result: dict) -> None:
    if "id" not in msg:
        return  # 通知（如 notifications/initialized）不需要响应
    print(
        json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}, ensure_ascii=False),
        flush=True,
    )


def main() -> None:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # 忽略垃圾行，别让一条坏消息杀掉通道

        method = msg.get("method", "")
        if method == "initialize":
            _reply(
                msg,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "notes-server", "version": "0.1.0"},
                },
            )
        elif method == "tools/list":
            _reply(
                msg,
                {
                    "tools": [
                        {
                            "name": t["name"],
                            "description": t["description"],
                            "inputSchema": t["inputSchema"],
                        }
                        for t in TOOLS
                    ]
                },
            )
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {})
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                _reply(
                    msg,
                    {
                        "content": [{"type": "text", "text": f"未知工具 {name}"}],
                        "isError": True,
                    },
                )
                continue
            try:
                text = tool["handler"](args)
                _reply(msg, {"content": [{"type": "text", "text": text}]})
            except Exception as exc:  # 业务错误：isError 而非崩溃
                _reply(
                    msg,
                    {
                        "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                        "isError": True,
                    },
                )


if __name__ == "__main__":
    main()