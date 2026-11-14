"""skill 数据化（ADR 116 §1）：一个技能 = 一个 markdown 文件（data/skills/*.md）。

文件格式（平铺分节、行解析——AGENTS.md/learned 同款数据行，不是 DSL）：

    # <技能名>          # H1 = 技能名 = mode 值；须与文件名 stem 一致
    <人设 prompt 原文>   # 首个 ## 之前 = 模型看到的人设（注入格式=落盘格式）

    ## tools            # 可选：工具名一行一个；缺省 = 技能 registry 全量
    ## budget           # 可选：工具轮预算；缺省 = Agent 默认 5
    ## settle           # 可选：skip = 跳固化 + 就地标题；缺省 = 正常固化

校验不静默（build_review_agent「不静默降级」先例）：未知节名、H1 与文件名
不一致、tools 声明的名字不在技能 registry、budget 非正整数、settle 未知值、
保留字 chat——全部装配报错。拼错＝该工具静默不上菜单（review.py 头注记过
的坑），宁可服务起不来。

技能 registry（M2 唯一形态：只读锚点注册表）＝ time 1 件 + notes 只读 4 件
（list/read/search/search_and_summarize）+ files 只读 3 件（read_file/
search_code/list_dir，锚点按 repo_root replace，111 锚点缝复用）= **恰 8 件
只读**，恰＝REVIEW_TOOLS 同集。write_note/write_file/terminal/spawn/plan/历史
一律不注册——写与跑命令在结构上缺席（review「不改代码不跑命令」同一事实），
allowed_tools=None 的技能也越不了界。技能需要写工具 = M3 授权档一起做
（负决策）。技能一律不注入用户记忆（陪伴者语境噪音，111 先例；触发信号＝
某技能真需要用户偏好，再加节）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from facta.core.audit import AuditLog
from facta.memory.store import Session
from facta.orchestrator.agent import Agent, build_default_agent, ensure_persona
from facta.tools.builtin import register_builtin
from facta.tools.context import ToolContext
from facta.tools.files import register_file_tools
from facta.tools.registry import ToolRegistry

# 技能 registry 的工具全集（唯一真值源）：只读 8 件，见模块头。
SKILL_TOOLS = frozenset({
    "get_current_time",
    "list_notes",
    "read_notes",
    "search_notes",
    "search_and_summarize",
    "read_file",
    "search_code",
    "list_dir",
})

_SECTIONS = frozenset({"tools", "budget", "settle"})
_DEFAULT_BUDGET = 5   # 与 Agent.max_tool_rounds 缺省同值；改一处记得看另一处


@dataclass(frozen=True)
class SkillSpec:
    """一个技能的解析产物。tools=None = registry 全量（8 件只读）。"""

    name: str
    system_prompt: str
    tools: frozenset[str] | None = None
    max_tool_rounds: int = _DEFAULT_BUDGET
    skip_settle: bool = False


def _split_sections(path: Path) -> tuple[str, str, dict[str, list[str]]]:
    """行扫描切分 H1/正文/分节（格式规则见模块头）。返回 (name, body, sections)。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError(f"技能文件缺 H1 标题行：{path}")
    name = lines[0][2:].strip()
    if name != path.stem:
        raise ValueError(f"技能名（H1={name!r}）与文件名 stem（{path.stem!r}）不一致：{path}")
    if name == "chat":
        raise ValueError(f"技能名不可用保留字 chat（mode=chat 是主对话通道）：{path}")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    body: list[str] = []
    for line in lines[1:]:
        if line.startswith("## "):
            current = line[3:].strip()
            if current not in _SECTIONS:
                raise ValueError(f"未知分节 {current!r}（可用：{sorted(_SECTIONS)}）：{path}")
            if current in sections:
                raise ValueError(f"分节重复：{current!r}：{path}")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
        else:
            body.append(line)
    return name, "\n".join(body).strip(), sections


def load_skill(path: Path) -> SkillSpec:
    """解析 + 校验一个技能文件（规则见模块头）。任何不合法都抛 ValueError。"""
    name, system_prompt, sections = _split_sections(path)
    if not system_prompt:
        raise ValueError(f"技能缺人设正文（首个 ## 之前）：{path}")

    tools: frozenset[str] | None = None
    if "tools" in sections:
        names = [ln.strip() for ln in sections["tools"] if ln.strip()]
        if not names:
            raise ValueError(f"tools 节为空（缺省全量就整节不写）：{path}")
        unknown = sorted(set(names) - SKILL_TOOLS)
        if unknown:
            raise ValueError(
                f"tools 声明了不在技能 registry 的工具 {unknown}（registry 全集：{sorted(SKILL_TOOLS)}）：{path}"
            )
        tools = frozenset(names)

    max_tool_rounds = _DEFAULT_BUDGET
    if "budget" in sections:
        raw = "\n".join(sections["budget"]).strip()
        try:
            max_tool_rounds = int(raw)
        except ValueError:
            raise ValueError(f"budget 须为整数：{raw!r}：{path}") from None
        if max_tool_rounds < 1:
            raise ValueError(f"budget 须 ≥ 1：{path}")

    skip_settle = False
    if "settle" in sections:
        raw = "\n".join(sections["settle"]).strip()
        if raw == "skip":
            skip_settle = True
        elif raw:
            raise ValueError(f"settle 只支持 skip（或留空走正常固化）：{raw!r}：{path}")

    return SkillSpec(
        name=name,
        system_prompt=system_prompt,
        tools=tools,
        max_tool_rounds=max_tool_rounds,
        skip_settle=skip_settle,
    )


def load_skills(directory: Path) -> dict[str, SkillSpec]:
    """扫目录建技能表（assemble 启动时一次）。文件即技能：新增技能 = 新增一个
    data/skills/*.md，零 py 改动。坏文件直接抛——装配报错不静默（模块头）。
    """
    specs: dict[str, SkillSpec] = {}
    for path in sorted(directory.glob("*.md")):
        spec = load_skill(path)
        specs[spec.name] = spec
    return specs


def build_skill_registry(mother_ctx: ToolContext, audit: AuditLog, repo_root: Path) -> ToolRegistry:
    """只读锚点注册表：恰 SKILL_TOOLS 8 件，文件锚点 = repo_root（111 锚点缝复用）。

    与 review 先例同款 replace 摆脱共享库写器携的全局 ctx 句柄；rctx.history=None
    → 历史工具自跳过（技能不挂编排历史）。写工具的「不注册」由 unregister 落实：
    家族注册入口（register_builtin/register_file_tools）是整族注册，拿掉
    write_note/write_file 后 registry 里结构上只剩只读 8 件。
    """
    reg = ToolRegistry(audit=audit)
    rctx = replace(mother_ctx, workspace_root=repo_root, session=None, history=None)
    register_builtin(reg, rctx)   # time + notes 族（含 write_note，下面拿掉）
    register_file_tools(reg, rctx)   # files 族（含 write_file，下面拿掉）
    reg.unregister("write_note")
    reg.unregister("write_file")
    return reg


def build_skill_agent(
    spec: SkillSpec,
    registry: ToolRegistry,
    session: Session,
    *,
    learned_dir: Path | None = None,
) -> Agent:
    """技能 Agent：人设=技能文件正文、菜单=spec.tools（缺省 registry 全量）、
    预算=spec.max_tool_rounds、settle 节→skip_settle 属性。

    router=None **显式钉死**（评审问题 8）：「评审 run 不挂场景路由」是 111
    的等价点，随 build_default_agent 缺省值漂移会丢。用户记忆不注入（技能无
    陪伴语境）。learned_dir=归属判定结果（facta.projects.resolve_learned_dir），
    None=不注入。
    """
    agent = build_default_agent(
        registry,
        learned_dir,
        router=None,
        user_memory_path=None,
        base_prompt=spec.system_prompt,
    )
    agent = replace(
        agent,
        allowed_tools=spec.tools,
        max_tool_rounds=spec.max_tool_rounds,
        skip_settle=spec.skip_settle,
    )
    ensure_persona(session, agent)
    return agent
