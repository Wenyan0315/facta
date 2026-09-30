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
  ⑤ 写盘原子（#13 丁案）：走同目录 tmp + os.replace（save_session / 面板保存同款），
     写失败不留半截、旧内容不被中途截断
  ⑥ 覆盖变短显式警告（#13 丙案）：read 有窗口而 write 是全量——这对不对称不能让
     模型自己心算，变短时在返回里量化提示
"""

from __future__ import annotations

import difflib
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

from facta.paths import MEMORY_WRITE_FENCE, WORKSPACE_ROOT
from facta.tools.context import ToolContext
from facta.tools.registry import Tool, ToolRegistry

MAX_FILE_BYTES = 1024 * 1024      # 1MB：超限拒读拒写（防灌爆上下文/内存）
DEFAULT_READ_LIMIT = 100          # read_file 默认窗口（037 P1：~100 行，配上下方余量指示）
MAX_SEARCH_HITS = 30              # search_code 命中上限（防海啸）
MAX_DIFF_LINES = 40               # write_file 返回的 diff 行数上限

# 敏感黑名单：路径 resolve 后命中即拒（读都不行）
# 注意：本清单被 _resolve_in_workspace 用于读写两条路径——052 的记忆写围栏
# （data/notes 等「可写要拒、可读必须放行」的资产）另列 paths.MEMORY_WRITE_FENCE。
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


def _read_file(path: str, offset: int = 1, limit: int = DEFAULT_READ_LIMIT, *, root: Path = WORKSPACE_ROOT) -> str:
    """读项目文件：行窗口分页（offset 从 1 起，limit 行），二进制/超 1MB 拒。

    037 P1（SWE-agent ACI）：窗口上下方余量显式指示——模型知道前后还有
    多少行、怎么续读，不再靠「共 N 行」心算；空文件显式标记（P2）。
    """
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
    if not lines:
        return f"{path}（空文件）"
    offset = max(1, offset)
    limit = max(1, limit)
    shown = lines[offset - 1 : offset - 1 + limit]
    if not shown:
        return f"{path}（共 {len(lines)} 行；offset={offset} 超出文件范围）"

    above = offset - 1
    below = len(lines) - (offset - 1 + len(shown))
    header = f"{path}（共 {len(lines)} 行"
    if above or below:
        header += f"，显示第 {offset}~{offset + len(shown) - 1} 行"
    header += "）：\n"
    hints = []
    if above:
        hints.append(f"上方还有 {above} 行（从 offset=1 起）")
    if below:
        hints.append(f"下方还有 {below} 行（续读 offset={offset + len(shown)}）")
    footer = ("\n〔" + "；".join(hints) + "〕") if hints else ""
    return header + "\n".join(shown) + footer


def _iter_searchable_text(root: Path) -> Iterator[tuple[str, str]]:
    """产出 (相对路径, 文本)——黑名单目录/敏感文件/超限/非文本整树跳过。

    黑名单目录整树跳过（含 .venv 几万文件——不跳会搜到天荒地老）；
    .env 等文件级黑名单此前只挡 read_file 的路径解析，search_code 直接
    rglob 绕过了它——密钥内容会随命中行吐给模型（评审修复轮）。与
    _resolve_in_workspace 的 _BLACKLIST_PARTS 同一清单（不 import 那个
    函数：解析语义不同）。
    """
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        try:
            rel = file.relative_to(root).as_posix()
            if any(rel == b or rel.startswith(b + "/") for b in _BLACKLIST_DIRS) or ".git" in file.parts:
                continue
            if any(part in _BLACKLIST_PARTS for part in Path(rel).parts) or rel.startswith(".env"):
                continue
            if file.stat().st_size > MAX_FILE_BYTES:
                continue
            yield rel, file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def _search_code(pattern: str, show_lines: bool = False, *, root: Path = WORKSPACE_ROOT) -> str:
    """grep 式代码定位：正则跨 workspace 搜文本文件。

    037 P3：默认只回「文件清单+命中计数」——一次宽搜不再把几十行内容
    喷进上下文；需要行级明细时 show_lines=true 二次展开（文件:行号:内容）。
    """
    if not pattern.strip():
        return "搜索模式为空"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"正则不合法：{e}（如搜字面量请转义，如 search_code 用 'def run_turn' 不用引号）"

    file_counts: list[tuple[str, int]] = []   # 计数模式：每文件命中数
    line_hits: list[str] = []                  # 行级模式：文件:行号:内容
    for rel, text in _iter_searchable_text(root):
        if show_lines and len(line_hits) >= MAX_SEARCH_HITS:
            break
        count = 0
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                count += 1
                if show_lines and len(line_hits) < MAX_SEARCH_HITS:
                    line_hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
        if count:
            file_counts.append((rel, count))

    if not file_counts:
        return "没有命中（可换关键词，或确认文件在项目内）"
    if show_lines:
        out = f"命中 {len(line_hits)} 处：\n" + "\n".join(line_hits)
        if sum(c for _, c in file_counts) > len(line_hits):
            out += f"\n〔命中超 {MAX_SEARCH_HITS} 处已截断，请用更具体的模式缩小范围〕"
        return out
    total = sum(c for _, c in file_counts)
    listing = [f"{rel}（{count} 处）" for rel, count in file_counts[:MAX_SEARCH_HITS]]
    out = f"{len(file_counts)} 个文件命中，共 {total} 处：\n" + "\n".join(listing)
    if len(file_counts) > MAX_SEARCH_HITS:
        out += f"\n〔仅列前 {MAX_SEARCH_HITS} 个文件〕"
    return out + "\n〔需要行级内容时重搜并加 show_lines=true〕"


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
    """写项目文件（新建或覆盖）。覆盖时返回 diff 摘要——改了什么一眼可见。

    #13 起写盘有两种兜底：①原子写（丁案）——tmp + os.replace，中途被杀只可能
    看到旧版或新版，不会留下半截，**旧内容也不会在写之前就被截断**；②覆盖变短
    时量化警告（丙案）——把「读一半就写」这个静默事故形态变成返回里的一句话。
    """
    target = _resolve_in_workspace(path, root=root)
    # 052 记忆写围栏：只拒写，读语义不动（_read_file 仍走 _resolve_in_workspace
    # 原路径）。记忆落盘的唯一入口是 write_note / sync_graph——它们带内容闸，
    # 从这里直写等于绕过闸门投毒（bash 臂的同款路径由 sandbox.py 的 deny 挡）。
    rel = target.relative_to(root).as_posix()
    for banned in MEMORY_WRITE_FENCE:
        if rel == banned or rel.startswith(banned + "/"):
            return f"记忆资产拒绝直写（走 write_note / sync_graph，内容闸在那条路上）：{rel}"
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

    # 丙案（#13）：覆盖后文件变短 → 显式硬警告。read_file 默认只给 100 行窗口，write_file
    # 却是全量覆写——读一半就写会静默截断尾部（#10 修 web.py 时的实证事故），而 diff 自己
    # 也截断到 40 行，事后信号只剩这一处。**只警告不拒写**：正常删减代码不该被挡；「拒写」
    # 要动 write_file 的契约（甲/乙案），留给维护者单独裁定。
    shrink_note = ""
    if overwritten:
        old_lines, new_lines = len(old_text.splitlines()), len(content.splitlines())
        if new_lines < old_lines:
            shrink_note = (
                f"\n⚠ 新内容比原文件少 {old_lines - new_lines} 行"
                f"（原 {old_lines} 行 → 新 {new_lines} 行）。若本次是「只读了窗口内一部分就全量重写」，"
                f"尾部已被截断——请用 read_file 的 offset/limit 分段读完整个文件，确认无误后整体重写。"
            )

    # 丁案（#13）：原子写——同目录 tmp + os.replace，与 save_session（ADR 040）、笔记面板
    # 保存（042）同款手法。此前直接 write_text 是「先截断再写」：进程在写盘中途被杀，旧内容
    # 当场消失、新内容半截，截断从此不可恢复（换个环境就是纯数据丢失）。同文件系统内换名是
    # 原子的——崩溃只可能看到旧版或新版，没有中间态。它不阻止截断，但把「静默丢失」降级成
    # 「可恢复」。tmp 名带 uuid（并发写同一目标不互踩）；后缀 .tmp 不匹配任何 *.py/*.md glob，
    # 半截临时文件不会混进清单或被打包。不做 fsync：与 store.py 同判据——威胁模型是「进程被
    # 杀」，内核 page cache 仍在，掉电持久化是另一档需求。
    tmp = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(content, encoding="utf-8")
        if overwritten:
            tmp.chmod(target.stat().st_mode)   # 保住原权限位：换名会把可执行脚本的 +x 写掉
        os.replace(tmp, target)
    except OSError as e:
        if tmp is not None:                    # 失败路径清尾巴（replace 没跑成，tmp 还留在盘上）
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return f"写入失败：{e}"
    action = "覆盖" if overwritten else "新建"
    return f"已{action} {path}（{len(content)} 字）{shrink_note}{diff_note}"


def register_file_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """注册文件四件。恒注册（无外部依赖）——workspace 围栏即安全边界。

    S6a 注入化：锚点从 ctx 取（闭包捕获）——主 agent = 主工作区；
    spawn 子 agent = worktree 目录。锚点跟着 ctx 走，无全局态。
    """
    root = ctx.workspace_root   # 闭包捕获（非循环变量，无 B023 风险）
    registry.register(Tool(
        name="read_file",
        description="读取项目工作区里的文件（代码/文档/配置），按行窗口分页（默认 100 行，返回带上下方余量与续读指示）。先 search_code 定位或 list_dir 浏览，再读目标文件。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目内相对路径，如 src/facta/loop.py 或 docs/architecture.md"},
                "offset": {"type": "integer", "description": "起始行号（从 1 起），默认 1"},
                "limit": {"type": "integer", "description": "读取行数，默认 100"},
            },
            "required": ["path"],
        },
        func=lambda path, offset=1, limit=DEFAULT_READ_LIMIT: _read_file(path, int(offset), int(limit), root=root),
        is_readonly=True,
    ))
    registry.register(Tool(
        name="search_code",
        description="在项目全部代码/文档里按正则搜索。默认只回命中的文件清单+每文件命中计数（防宽搜喷爆上下文）；需要行级内容时把 show_lines 设为 true，回 文件:行号:行内容。找「某函数定义在哪」「谁调用了 X」时用它定位，再用 read_file 读上下文。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式（搜字面量直接写，特殊字符需转义），如 'def run_turn'、'memory|session'"},
                "show_lines": {"type": "boolean", "description": "true=回行级明细（文件:行号:内容），默认 false 只回文件清单+命中计数"},
            },
            "required": ["pattern"],
        },
        func=lambda pattern, show_lines=False: _search_code(pattern, bool(show_lines), root=root),
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
        description="写入项目工作区文件（新建或覆盖）。覆盖已有文件时返回 diff 改动摘要；覆盖后内容比原文件变短会显式警告（防「只读了窗口内一部分就全量重写」截断尾部）。用于改代码/写文档/建配置；往知识库存笔记用 write_note 而非本工具。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目内相对路径"},
                "content": {"type": "string", "description": "完整文件内容（全量写入，非追加）"},
            },
            "required": ["path", "content"],
        },
        func=lambda path, content: _write_file(path, content, root=root),
        idempotent=True,   # P0-3：全量覆写同内容=同结果，崩溃后可安全重做
    ))
