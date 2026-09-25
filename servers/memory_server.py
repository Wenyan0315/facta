"""记忆层 MCP **只读**服务器（044 A 径：架构可插拔，不独立仓库）。

对外出口，内部 agent 不消费它（注入仍走 system prompt 快照，见 044 第 4 节）。
与 notes_server 的两点有意差异：

1. **import agent 源码**（notes_server 是零依赖自包含）——行格式解析、白名单、
   路径锚必须单份真值，复制进来就是两个真值源（044 第 3 节）。代价：必须用仓库
   venv 的解释器拉起（`.venv/bin/python servers/memory_server.py`）。
2. **只读**：写路径 `memory_add` 未实现，挂触发信号「tombstone 删除语义落地 +
   provenance」（044 第 1 节）。记忆是用户资产，没有撤销机制前不交出去。

JSON-RPC 骨架照抄 notes_server（协议最小子集 initialize / tools/list /
tools/call，业务错误 isError=true 不崩进程）。骨架的重复是有意的：notes_server
要保持「任意 Python 直跑」的自包含性，抽共用模块会破坏它——**第三个 stdio
服务器出现时再提取**（rule of three）。
"""

import json
import os
import sys
from pathlib import Path

from agent.memory.consolidate import CATEGORIES, SCOPES
from agent.memory.learned import read_learned
from agent.paths import LEARNED_DIR, user_memory_path

# 项目级记忆目录。默认值取 paths.LEARNED_DIR（单份真值），env 覆写只是测试注入
# 点——子进程里 monkeypatch 够不着，与 notes_server 的 MCP_SANDBOX_DIR 同款理由。
PROJECT_MEMORY_DIR = Path(os.environ.get("MCP_LEARNED_DIR", str(LEARNED_DIR)))

ENTRY_FORMAT = "- [YYYY-MM-DD] {[已验证] }内容"


def _bucket(category: str) -> Path:
    """category → 文件，与记忆面板 `app._learned_path` 同口径。

    user 桶走 `user_memory_path()`（函数而非常量：CORTEX_USER_MEMORY 覆写点在
    paths.py，面板与注入侧同源，这里也不能自己拼路径）。
    """
    if category == "user":
        return user_memory_path()
    return PROJECT_MEMORY_DIR / f"{category}.md"


def _recall(args: dict) -> str:
    scope = str(args.get("scope") or "all")
    category = str(args.get("category") or "")
    query = str(args.get("query") or "").strip().lower()

    if scope not in ("all", *SCOPES):
        raise ValueError(f"scope 非法：{scope}（可选 all / {' / '.join(SCOPES)}）")
    if category and category not in (*CATEGORIES, "user"):
        raise ValueError(
            f"category 非法：{category}（可选 {' / '.join((*CATEGORIES, 'user'))}）"
        )
    if category == "user" and scope == "project":
        raise ValueError("category=user 属用户级记忆，与 scope=project 冲突")

    # 选桶：category 优先（指名要一桶），否则按 scope 展开
    if category:
        buckets = [category]
    elif scope == "user":
        buckets = ["user"]
    elif scope == "project":
        buckets = list(CATEGORIES)
    else:
        buckets = [*CATEGORIES, "user"]

    sections: list[str] = []
    for name in buckets:
        entries = read_learned(_bucket(name))   # 读侧单份真值：不自己 open()
        if query:
            entries = [e for e in entries if query in e.content.lower()]
        if not entries:
            continue   # 空桶跳过（与 _learned_block 一致：「暂无」是给模型看的噪声）
        # 条目渲染 = 落盘格式（零翻译层，与 agent._learned_block 同款表达式）；
        # 节头多带 scope 是因为这里两个作用域会同屏返回
        label = "user" if name == "user" else f"project:{name}"
        lines = [f"[{label}]"]
        lines.extend(f"- [{e.date}] {e.content}" if e.date else e.content for e in entries)
        sections.append("\n".join(lines))

    if not sections:
        return "（无匹配的记忆条目）"
    return "\n".join(sections)


def _list_categories(args: dict) -> str:
    """白名单自述：外部 harness 不用靠报错试探合法参数值。"""
    return json.dumps(
        {
            "categories": list(CATEGORIES),
            "scopes": list(SCOPES),
            "entry_format": ENTRY_FORMAT,
            "write": "未暴露（只读服务器，写路径挂 tombstone 信号，见 docs/decisions/044）",
        },
        ensure_ascii=False,
    )


TOOLS = [
    {
        "name": "memory_recall",
        "description": (
            "读取个人 agent 的长时记忆（跨会话沉淀）。可按 scope（project=项目级 / "
            "user=用户级）、category（decisions / constraints / other）与 query"
            "（大小写不敏感子串）过滤；全部参数可省略=返回所有条目。"
            "条目形如「- [日期] 内容」，带 [已验证] 前缀的表示经过程序校验。只读。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["all", *SCOPES],
                    "description": "作用域，默认 all",
                },
                "category": {
                    "type": "string",
                    "enum": [*CATEGORIES, "user"],
                    "description": "记忆桶，默认全部；user=用户级记忆（仓库外）",
                },
                "query": {"type": "string", "description": "内容子串过滤，可省略"},
            },
        },
        "handler": _recall,
    },
    {
        "name": "memory_list_categories",
        "description": "返回可用的 category / scope 白名单与条目格式（挂载后先看这个）。",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _list_categories,
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
    for raw in sys.stdin:
        line = raw.strip()
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
                    "serverInfo": {"name": "memory-server", "version": "0.1.0"},
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
                # 未知工具（含写路径 memory_add）：isError 而不是崩——044 判定标准 2
                _reply(
                    msg,
                    {
                        "content": [{"type": "text", "text": f"未知工具 {name}（本服务器只读）"}],
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
