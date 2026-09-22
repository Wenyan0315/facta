"""S4a 文件工具四件：read_file / search_code / list_dir / write_file（coding agent 的手）。

与 write_note 的分工：note 是知识库（data/notes/，语义资产）；本组是
**项目工作区文件**（代码/文档/配置）——读改项目本身，自我改进闭环的腿。

安全栅栏（S4a 草案裁定，先栅栏后开门的执行）：
  ① workspace 围栏：路径 resolve 后必须在 WORKSPACE_ROOT 内——挡 ../ 逃逸、
     绝对路径跳脱、symlink 指外（MCP 沙箱同款三招）
  ② 敏感黑名单：.env*（密钥）、.git/、data/memory/（会话隐私）、data/audit/
     （审计）、data/vector_db/——读都不行（读 .env 比写更致命，MCP-b 教训）
  ③ write_file：二进制/超 1MB 拒写拒读；覆盖现有文件时返回 diff 摘要
     （S2 预留 diff 视图的数据源；改了什么模型和用户都一眼可见）
  ④ 分级：read/search/list = L0；write_file = L1（审计强化）
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from agent.paths import WORKSPACE_ROOT
from agent.tools.context import ToolContext
from agent.tools.registry import Tool, ToolRegistry

MAX_FILE_BYTES = 1024 * 1024      # 1MB：超限拒读拒写（防灌爆上下文/内存）
MAX_READ_CHARS = 8000             # read_file 默认窗口（与 fetch_web 同量级纪律）
MAX_SEARCH_HITS = 30              # search_code 命中上限（防海啸）
MAX_DIFF_LINES = 40               # write_file 返回的 diff 行数上限

# 敏感黑名单：路径 resolve 后命中即拒（读都不行）
_BLACKLIST_PARTS = (".env", ".git")
_BLACKLIST_DIRS = ("data/memory", "data/audit", "data/vector_db", "servers/sandbox", ".venv", "data/worktrees")   # 末项 S6a：worktree 沙箱区（search_code rglob 双扫+主 agent 读子沙箱都挡）


def _resolve_in_workspace(path_str: str, *, root: Path = WORKSPACE_ROOT) -> Path:
    """把用户/模型给的路径安全解析到 workspace 内；越界/敏感即 ValueError。

    root（S6a 注入化）：文件锚点——主 agent = 主工作区；子 agent = worktree。
    keyword-only 带 paths.py 默认（真值源唯一，引用传播非第二真值源）。

    ValueError 经 registry 变错误字符串回给模型——它可自纠（换个合法路径），
    不炸会话（与 fetch_web 栅栏同一错误通道）。
    """
    if not path_str or not path_str.strip():
        raise ValueError("路径为空")

    candidate = (root / path_str).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"路径越界（只允许项目内相对路径）：{path_str}")

    rel = candidate.relative_to(root).as_posix()
    for part in _BLACKLIST_PARTS:
        if part in Path(rel).parts or rel.startswith(part):
            raise ValueError(f"敏感路径拒绝访问：{rel}")
    for banned in _BLACKLIST_DIRS:
        if rel == banned or rel.startswith(banned + "/"):
            raise ValueError(f"敏感目录拒绝访问：{rel}")
    return candidate


def _read_file(path: str, offset: int = 1, limit: int = 200, *, root: Path = WORKSPACE_ROOT) -> str:
    """读项目文件：行窗口分页（offset 从 1 起，limit 行），二进制/超 1MB 拒。"""
    target = _resolve_in_workspace(path, root=root)
    if not target.is_file():
        return f"文件不存在：{path}（可用 list_dir 浏览目录）"
    if target.stat().st_size > MAX_FILE_BYTES:
        return f"文件超过 1MB，拒绝读取：{path}"

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"不是文本文件（或非 UTF-8），拒绝读取：{path}"

    lines = text.splitlines()
    if len(lines) > limit:
        shown = lines[offset - 1 : offset - 1 + limit]
        return (
            f"{path}（共 {len(lines)} 行，显示第 {offset}~{offset + len(shown) - 1} 行）：\n"
            + "\n".join(shown)
            + f"\n〔未完，继续读 offset={offset + limit}〕"
        )
    return f"{path}（共 {len(lines)} 行）：\n" + text


def _search_code(pattern: str, *, root: Path = WORKSPACE_ROOT) -> str:
    """grep 式代码定位：正则跨 workspace 搜文本文件，返回 文件:行号:内容。"""
    if not pattern.strip():
        return "搜索模式为空"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"正则不合法：{e}（如搜字面量请转义，如 search_code 用 'def run_turn' 不用引号）"

    hits: list[str] = []
    for file in sorted(root.rglob("*")):
        if not file.is_file() or len(hits) >= MAX_SEARCH_HITS:
            continue
        try:
            rel = file.relative_to(root).as_posix()
            # 黑名单目录整树跳过（含 .venv 几万文件——不跳会搜到天荒地老）
            if any(rel == b or rel.startswith(b + "/") for b in _BLACKLIST_DIRS) or ".git" in file.parts:
                continue
            # 敏感文件跳过（评审修复轮）：.env 等文件级黑名单此前只挡
            # read_file 的路径解析，search_code 直接 rglob 绕过了它——
            # 密钥文件的内容会随命中行吐给模型。与 _resolve_in_workspace
            # 的 _BLACKLIST_PARTS 同一清单（不 import 那个函数：解析语义不同）
            if any(part in _BLACKLIST_PARTS for part in Path(rel).parts) or rel.startswith(".env"):
                continue
            if file.stat().st_size > MAX_FILE_BYTES:
                continue
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
                if len(hits) >= MAX_SEARCH_HITS:
                    break
    if not hits:
        return "没有命中（可换关键词，或确认文件在项目内）"
    return f"命中 {len(hits)} 处：\n" + "\n".join(hits)


def _list_dir(path: str = ".", *, root: Path = WORKSPACE_ROOT) -> str:
    """列目录一层：目录加 / 后缀，标注文件大小。"""
    target = _resolve_in_workspace(path, root=root)
    if not target.is_dir():
        return f"目录不存在：{path}"

    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    if not entries:
        return f"{path}（空目录）"
    lines = []
    for e in entries[:100]:
        if e.is_dir():
            lines.append(f"{e.name}/")
        else:
            size = e.stat().st_size
            lines.append(f"{e.name}（{size} B）")
    return f"{path}（{len(entries)} 项）：\n" + "\n".join(lines)


def _write_file(path: str, content: str, *, root: Path = WORKSPACE_ROOT) -> str:
    """写项目文件（新建或覆盖）。覆盖时返回 diff 摘要——改了什么一眼可见。"""
    target = _resolve_in_workspace(path, root=root)
    if target.exists() and not target.is_file():
        return f"目标不是普通文件：{path}"
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return f"内容超过 1MB，拒绝写入：{path}"

    old_text = ""
    overwritten = target.exists()
    if overwritten:
        try:
            old_text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"已存在的是二进制文件，拒绝覆盖：{path}"

    diff_note = ""
    if overwritten:
        diff = list(difflib.unified_diff(
            old_text.splitlines(), content.splitlines(),
            fromfile=f"{path}（旧）", tofile=f"{path}（新）", lineterm="",
        ))
        if not diff:
            return "内容与现有文件完全相同，未写入"
        diff_note = "\n改动（unified diff，前 40 行）：\n" + "\n".join(diff[:MAX_DIFF_LINES])
        if len(diff) > MAX_DIFF_LINES:
            diff_note += f"\n〔diff 共 {len(diff)} 行，已截断〕"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"写入失败：{e}"
    action = "覆盖" if overwritten else "新建"
    return f"已{action} {path}（{len(content)} 字）{diff_note}"


def register_file_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """注册文件四件。恒注册（无外部依赖）——workspace 围栏即安全边界。

    S6a 注入化：锚点从 ctx 取（闭包捕获）——主 agent = 主工作区；
    spawn 子 agent = worktree 目录。锚点跟着 ctx 走，无全局态。
    """
    root = ctx.workspace_root   # 闭包捕获（非循环变量，无 B023 风险）
    registry.register(Tool(
        name="read_file",
        description="读取项目工作区里的文件（代码/文档/配置），按行窗口分页。先 search_code 定位或 list_dir 浏览，再读目标文件。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目内相对路径，如 src/agent/loop.py 或 docs/architecture.md"},
                "offset": {"type": "integer", "description": "起始行号（从 1 起），默认 1"},
                "limit": {"type": "integer", "description": "读取行数，默认 200"},
            },
            "required": ["path"],
        },
        func=lambda path, offset=1, limit=200: _read_file(path, int(offset), int(limit), root=root),
        is_readonly=True,
    ))
    registry.register(Tool(
        name="search_code",
        description="在项目全部代码/文档里按正则搜索，返回 文件:行号:行内容。找「某函数定义在哪」「谁调用了 X」时用它定位，再用 read_file 读上下文。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式（搜字面量直接写，特殊字符需转义），如 'def run_turn'、'memory|session'"},
            },
            "required": ["pattern"],
        },
        func=lambda pattern: _search_code(pattern, root=root),
        is_readonly=True,
    ))
    registry.register(Tool(
        name="list_dir",
        description="列出项目里某目录的内容（一层），目录带 / 后缀。不知道文件在哪时先浏览目录结构。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目内相对路径，默认根目录 '.'"},
            },
            "required": [],
        },
        func=lambda path=".": _list_dir(path, root=root),
        is_readonly=True,
    ))
    registry.register(Tool(
        name="write_file",
        description="写入项目工作区文件（新建或覆盖）。覆盖已有文件时返回 diff 改动摘要。用于改代码/写文档/建配置；往知识库存笔记用 write_note 而非本工具。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目内相对路径"},
                "content": {"type": "string", "description": "完整文件内容（全量写入，非追加）"},
            },
            "required": ["path", "content"],
        },
        func=lambda path, content: _write_file(path, content, root=root),
    ))
