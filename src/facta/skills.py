"""skill 数据化（ADR 116 §1）：一个技能 = 一个 markdown 文件（data/skills/*.md）。

文件格式（平铺分节、行解析——AGENTS.md/learned 同款数据行，不是 DSL）：

    # <技能名>          # H1 = 技能名 = mode 值；须与文件名 stem 一致
    <人设 prompt 原文>   # 首个 ## 之前 = 模型看到的人设（注入格式=落盘格式）

    ## tools            # 可选：工具名一行一个；缺省 = 技能 registry 全量
    ## budget           # 可选：工具轮预算；缺省 = Agent 默认 5
    ## settle           # 可选：skip = 跳固化 + 就地标题；缺省 = 正常固化
    ## authorization    # 可选：readonly（缺省）/ write——M3 授权档（ADR 117 §3）

校验不静默（build_review_agent「不静默降级」先例）：未知节名、H1 与文件名
不一致、tools 声明的名字不在技能 registry（按档位）、budget 非正整数、
settle 未知值、authorization 未知值、保留字 chat——全部装配报错。拼错＝该
工具静默不上菜单（review.py 头注记过的坑），宁可服务起不来。

技能 registry 两档（ADR 117 §3）：readonly = 只读锚点注册表，恰 8 件
（M2 现状）；write = 8 件 + write_file/write_note（标 write-auth 确认，
任务级首写授权由 app 层 on_confirm 包装置位）+ run_command（019 动态规则
原样：白名单免确认/危险停确认）。锚点全按 repo_root replace（111 锚点缝，
M3 从只读泛化到写与终端——写围栏机制不变只换根）。
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
from facta.tools.terminal import register_terminal_tools

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

# ADR 117 §3：write 档扩池三件（写两件 + 终端一件）。
SKILL_WRITE_TOOLS = frozenset({"write_file", "write_note", "run_command"})

# 授权档位值（## authorization 节的合法取值）
AUTH_READONLY = "readonly"
AUTH_WRITE = "write"

# write 档写工具的确认归因名（050 guard 先例）：进审计 extra["guard"]，
# app 层 on_confirm 包装按「工具名 + 此标记」识别写首停、approve 后置位
# Task.authorized_write（117 §3 任务级一次性写授权）。
WRITE_AUTH_GUARD = "write-auth"

_SECTIONS = frozenset({"tools", "budget", "settle", "authorization"})
_DEFAULT_BUDGET = 5   # 与 Agent.max_tool_rounds 缺省同值；改一处记得看另一处


@dataclass(frozen=True)
class SkillSpec:
    """一个技能的解析产物。tools=None = registry 全量（按档位 8 或 11 件）。"""

    name: str
    system_prompt: str
    tools: frozenset[str] | None = None
    max_tool_rounds: int = _DEFAULT_BUDGET
    skip_settle: bool = False
    authorization: str = AUTH_READONLY   # ADR 117 §3：readonly（缺省）/ write


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

    skip_settle = False
    if "settle" in sections:
        raw = "\n".join(sections["settle"]).strip()
        if raw == "skip":
            skip_settle = True
        elif raw:
            raise ValueError(f"settle 只支持 skip（或留空走正常固化）：{raw!r}：{path}")

    authorization = AUTH_READONLY
    if "authorization" in sections:
        raw = "\n".join(sections["authorization"]).strip()
        if raw not in (AUTH_READONLY, AUTH_WRITE):
            raise ValueError(
                f"authorization 只支持 {AUTH_READONLY}/{AUTH_WRITE}：{raw!r}：{path}"
            )
        authorization = raw

    # tools 校验按档位：readonly 只认只读 8 件；write 才认写三件（117 §3——
    # readonly 档声明 write_file 是配置错误，装配期拦住而非静默少给）
    allowed = SKILL_TOOLS | (SKILL_WRITE_TOOLS if authorization == AUTH_WRITE else frozenset())
    tools: frozenset[str] | None = None
    if "tools" in sections:
        names = [ln.strip() for ln in sections["tools"] if ln.strip()]
        if not names:
            raise ValueError(f"tools 节为空（缺省全量就整节不写）：{path}")
        unknown = sorted(set(names) - allowed)
        if unknown:
            raise ValueError(
                f"tools 声明了不在技能 registry 的工具 {unknown}"
                f"（{authorization} 档全集：{sorted(allowed)}）：{path}"
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

    return SkillSpec(
        name=name,
        system_prompt=system_prompt,
        tools=tools,
        max_tool_rounds=max_tool_rounds,
        skip_settle=skip_settle,
        authorization=authorization,
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


def build_skill_registry(
    mother_ctx: ToolContext,
    audit: AuditLog,
    repo_root: Path,
    authorization: str = AUTH_READONLY,
) -> ToolRegistry:
    """按档位构建技能 registry（ADR 117 §3），锚点 = repo_root（111 锚点缝）。

    与 review 先例同款 replace 摆脱共享库写器携的全局 ctx 句柄；rctx.history=None
    → 历史工具自跳过（技能不挂编排历史）。

    readonly（M2 现状）：家族注册后 unregister 写两件，结构上恰只读 8 件。
    write：写两件**留下并改标 WRITE_AUTH_GUARD 确认**（任务级首写授权的
    工具侧半边；app 层 on_confirm 包装是另一半——见 app.py），run_command
    整件注册（019 动态规则原样：白名单免确认/危险停确认+沙箱），锚点同样
    repo_root（写围栏机制不变只换根，117 §4）。菜单边界仍由 spec.tools
    过滤——这里只管池子里有什么。
    """
    if authorization not in (AUTH_READONLY, AUTH_WRITE):
        raise ValueError(f"未知授权档位：{authorization!r}（{AUTH_READONLY}/{AUTH_WRITE}）")
    reg = ToolRegistry(audit=audit)
    rctx = replace(mother_ctx, workspace_root=repo_root, session=None, history=None)
    register_builtin(reg, rctx)   # time + notes 族（含 write_note，下面按档位处置）
    register_file_tools(reg, rctx)   # files 族（含 write_file，下面按档位处置）
    if authorization == AUTH_WRITE:
        for name in ("write_note", "write_file"):
            tool = reg.get(name)
            if tool is not None:
                reg.register(replace(tool, needs_confirmation=WRITE_AUTH_GUARD))
        register_terminal_tools(reg, rctx)   # run_command：019 动态确认 + 沙箱，锚点 repo_root
    else:
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
