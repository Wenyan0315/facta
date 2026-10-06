"""MCP 配置化（MCP-config）：改清单加工具，零代码。

你见过的那类 agent（Claude Desktop 等）都让用户「配置 MCP 服务器」——
本模块就是把组装层从写死变成数据驱动。

清单（mcp_servers.json）每台服务器一条：
    {"name": "notes", "command": ["{python}", "servers/notes_server.py"], "prefix": "mcp__"}
    {"name": "ctx7", "url": "https://mcp.context7.com/mcp", "enabled": false}
- command（stdio 型）与 url（streamable HTTP 型）二选一
- command 列表第一个元素可以是占位符 "{python}"：装配时替换成当前 agent
  自己的解释器（sys.executable）。清单里写死 "python" 会撞上「venv 未激活时
  PATH 里没有 python」的坑——配置要可移植，解释器由运行时自己填
- command 里的相对路径（脚本、服务器自己读的数据）一律相对**仓库根**解析：
  装配时给子进程传 cwd=WORKSPACE_ROOT，换目录启动也不漂（S8a 边界①同款）
- prefix 缺省 = f"{name}__"：服务器名唯一 → 前缀唯一 → 服务器之间不互踩
- enabled=false 只挂名不拉（远程服务器默认都不拉——启动背网络依赖不值）
- headers 给远程服务器带 API key 用——**含密钥的配置不得进仓库**：
  放个人文件，用环境变量 MCP_SERVERS 指向它（.env 纪律的延伸）

装配循环 = 衣服工厂：命令型穿 McpClient、URL 型穿 HttpMcpClient；
register_mcp_tools 通吃两者（接口同构、实现异构）。
单台失败/撞名只警告不阻断——坏一台不拖垮全场（MCP-c 降级哲学的配置版）。
"""

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from facta.paths import WORKSPACE_ROOT
from facta.tools.mcp_client import McpClient, McpError, register_mcp_tools
from facta.tools.registry import ToolRegistry

if TYPE_CHECKING:
    # ADR 095（R09）：httpx 在 rag extras——mock 档（零外部依赖）装 wheel 后
    # 顶层 import 直接 ModuleNotFoundError。HttpMcpClient 只在 URL 型分支用，
    # 延迟到用时导入；注解走 TYPE_CHECKING 保 mypy（局部变量注解运行时不求值）。
    from facta.tools.mcp_http import HttpMcpClient

logger = logging.getLogger(__name__)


@dataclass
class ServerSpec:
    """清单里的一台服务器：穿哪件衣服 + 在菜单里叫什么。"""

    name: str
    command: list[str] | None = None
    url: str | None = None
    prefix: str = ""
    headers: dict = field(default_factory=dict)
    timeout: float = 30.0
    enabled: bool = True


def load_server_specs(path: Path) -> list[ServerSpec]:
    """读清单并校验。文件不存在 → 空清单（第一次跑很正常）；结构错 → 抛 ValueError。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        raise ValueError(f"MCP 配置不是合法 JSON（{path}）：{exc}") from exc
    servers = raw.get("servers")
    if not isinstance(servers, list):
        raise ValueError(f"MCP 配置缺少 servers 列表（{path}）")
    specs: list[ServerSpec] = []
    for index, item in enumerate(servers):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError(f"MCP 配置第 {index} 条缺少 name")
        command, url = item.get("command"), item.get("url")
        if command is not None and url is not None:
            raise ValueError(f"MCP 服务器 {item['name']}：command 与 url 只能二选一")
        if command is None and url is None:
            raise ValueError(f"MCP 服务器 {item['name']}：command 与 url 必须给一个")
        specs.append(
            ServerSpec(
                name=item["name"],
                command=command if isinstance(command, list) else None,
                url=url if isinstance(url, str) else None,
                prefix=item.get("prefix") or f"{item['name']}__",
                headers=item.get("headers") or {},
                timeout=float(item.get("timeout", 30.0)),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return specs


def assemble_servers(registry: ToolRegistry, specs: list[ServerSpec]) -> list:
    """按清单一台台装配：成功返回客户端（调用方负责 finally 关闭）。"""
    clients = []
    for spec in specs:
        if not spec.enabled:
            logger.info("MCP 服务器已禁用，跳过：%s", spec.name)
            continue
        try:
            if spec.command is not None:
                command = list(spec.command)
                if command and command[0] == "{python}":
                    # 占位符：用 agent 自己的解释器（venv 未激活也不怕找不到 python）
                    command = [sys.executable] + command[1:]
                # 095：联合里的 HttpMcpClient 在 else 分支才延迟导入（局部注解
                # 运行时不求值，F823 此处为误报；加引号又撞 UP037，故 noqa）
                client: McpClient | HttpMcpClient = McpClient(  # noqa: F823
                    # cwd 锚仓库根：清单里的相对脚本路径（"servers/notes_server.py"）
                    # 与 mcp_servers.json 自己的默认位置同源，不随启动目录漂
                    command,
                    timeout=spec.timeout,
                    cwd=str(WORKSPACE_ROOT),
                )
            else:
                if not spec.url:   # 畸形配置（command/url 双缺）：跳过而非喂 None 给 httpx
                    logger.warning("MCP 服务器配置缺 url/command，跳过：%s", spec.name)
                    continue
                from facta.tools.mcp_http import (
                    HttpMcpClient,  # 延迟导入（095）：httpx 属 rag extras
                )

                client = HttpMcpClient(
                    spec.url, timeout=spec.timeout, headers=spec.headers or None
                )
        except (McpError, OSError) as exc:
            # OSError：命令路径不存在（FileNotFoundError）这类启动层面的失败
            logger.warning("MCP 服务器接入失败，跳过：%s（%s）", spec.name, exc)
            continue
        try:
            register_mcp_tools(registry, client, prefix=spec.prefix)
        except McpError as exc:
            # 撞名（前缀重复等）：拒绝而不是静默覆盖——静默覆盖等于把菜单卖给外部进程
            logger.warning("MCP 服务器登记失败，跳过：%s（%s）", spec.name, exc)
            client.close()
            continue
        clients.append(client)
        logger.info("MCP 服务器已接入：%s", spec.name)
    return clients
