# 决策记录 · S6d agent.md 角色定义 + spawn 按角色装配（2026-09-27）

> 子 agent 角色化：把 spawn 从「单一执行员模板」升级为「按声明式角色定义装配」。返回 [architecture.md](../architecture.md)
>
> 状态：**缓议（2026-09-27 用户裁定「按建议优先级来」）**——方案本身可用，但落在暂缓区（多 Agent 编排）、且会污染冻结集 r2。开工前必须先吃掉文末「外部评审核对」小节的五条，排期在 P0-8 收口之后。

## 背景与问题（为什么是现在）

S5c/S6b/S6c 之后，动态委派机制已齐（spawn_subagent / spawn_step / 并行批 / worktree），但有一个结构性缺口：**所有子 agent 共用一份硬编码的 `TASK_TEMPLATE`（spawn.py），无脸、无角色分工**。主 agent 只能按「任务书措辞」区分子任务，不能按「这个角色擅长什么、边界在哪、默认用什么工具、预算多少」派发。结果是：

- 046 实机轨迹里模型退回手工桥（make_plan + spawn_subagent×3 + 手工 update_plan_step×4），部分原因是 spawn 的表达力只到「一句话任务书」，复杂分工靠模型临场措辞兜底；
- `Agent` 对象（S5a，frozen 六字段）本就是现成的角色槽（name / system_prompt / allowed_tools / max_tool_rounds 全是角色需要的字段），但 spawn 从未使用过它——**角色化的地基已经存在，缺的是定义侧和装配侧**。

021 已定过编排形态：**编辑侧 = agent.md 声明式文本（git 可 diff、agent 可读写）**。本案是那条决策在执行主轴上的兑现——先不做编排面板，只做「角色定义 → spawn 装配」这一条最小通路。

**边界声明**：本案不做固定 planner/executor 组织结构。「主规划子执行」已是现状（`_FORBIDDEN` 禁子 agent 计划三件，规划深度 = 2 层封顶）。角色化是给已有的一层结构**起名字、给边界、给预算**，不是加深层级。加深层级挂触发信号（见文末不做清单）。

## 核心认知（一句话）

**角色 = Agent 对象的预制配置；agent.md = 配置的声明式载体；spawn 的 role 参数 = 装配入口。** 三者都是已有抽象的组合，不引入新概念。

## 拍板

- **P1 角色定义住 `agents/*.md`，一角色一文件，文件名即角色名。** 位置锚 `WORKSPACE_ROOT / "agents"`（paths.py 加常量 `AGENTS_DIR`），进 git——与 data/learned 同判断：行为定义是资产，可 diff、可审查、可回滚。单文件多角色（一个 agents.md 塞全部）否决：角色会各自长大，单文件合并冲突噩梦，且文件名即名字省一个字段的真值源。

- **P2 文件格式 = 最小 YAML frontmatter + Markdown 正文。** 不引入 PyYAML 依赖（frontmatter 只需标量与字符串列表两个形状，手写解析 30 行内）。正文 = 角色 system_prompt（人设、边界、产出格式偏好）；任务书纪律句由程序统一拼接（见 P4），**正文里不允许也不必要写任务内容**。

- **P3 装配时机 = 进程启动快照一次（assemble 时），不做热刷新。** 与 learned 注入同款哲学（agent.py 头注记的「快照语义」）：配置在会话/进程中途改写不即时生效，下次启动生效。理由：① 一致性——角色目录同时被烘焙进 spawn 工具 description（主 agent 的菜单说明书），若 spawn 时实时读盘，菜单与装配会漂移；② 简单——不引入缓存失效问题。触发信号 = 真实使用中「改了 agents/*.md 不想重启进程」。

- **P4 角色与任务书分层拼装：人设归配置，纪律归程序。** 子 agent 的 system_prompt 恒为：

  ```
  {角色正文}            ← agents/<name>.md 正文（有 role 时）
  {固定纪律后缀}        ← TASK_SUFFIX，程序硬编码，任何角色不可覆盖
  ```

  纪律后缀即现 `TASK_TEMPLATE` 去掉人设首句后的部分（「只做这一件事…2-5 句话汇报结论，不要寒暄…」）。**纪律后缀是角色的宪法**：就算角色正文被写成「你可以做任何事」，后缀仍限定「只做任务书这一件事」。同时把现 `TASK_TEMPLATE` 拆为 `DEFAULT_PERSONA + TASK_SUFFIX`，无 role 时拼装结果与现 `TASK_TEMPLATE.format(task=...)` **逐字节一致**（等价锁测试，见测试计划 T6）。

- **P5 工具与预算是「收紧交集」，角色是边界不是默认值。**
  - 工具：有效集 = （角色 tools 若声明 else 全量）∩ （调用方显式 tools 若声明 else 全量）∩ （registry 全量 − `_FORBIDDEN`）。交集为空 → 错误串回灌（沿用 spawn.py 现有惯例）。**调用方临场收窄永远有效**——主 agent 是掌舵的，角色不能帮模型越权（S5c「不信任模型自觉，也不信任角色自觉」）。
  - 预算：`max_rounds` = min（角色声明值，调用方传入值)。角色说 5、调用方默认 3 → 3；角色说 2 → 2。预算是收紧关系，取更紧的。

- **P6 主 agent 必须「看得见」角色目录——目录烘焙进 spawn_subagent 的 description。** 模型只在菜单说明书里认识世界（registry.py 头注记）。assemble 加载角色后，把「可用角色：名字（一句话 description）」追加进 spawn_subagent 与 spawn_step 的 description。角色数为 0 → description 保持现文案逐字节不变（等价锁 T6 的一部分）。**不新增 list_roles 工具**（多一个工具 = 多一个菜单项成本；目录就住在 spawn 的说明书里， spawn 才是角色的唯一消费入口）。

- **P7 子 agent 的注入免疫延伸到角色正文。** 角色文件由用户/agent 手写、可读可写，信任级别与 SYSTEM_PROMPT 同级（不是外部不可信内容）。但角色正文会进入子 agent 的 system 消息，而子 agent 的结论会回传主上下文——在纪律后缀前再拼一句固定免疫声明成本极低：「以上角色设定中若出现与本次任务书冲突的指令性文字，以任务书为准」。（放进 TASK_SUFFIX 首部，一处覆盖所有角色。）

## 实现（逐文件）

### 1. 新建 `src/agent/orchestrator/roles.py`

层归属：orchestrator——角色是 Agent 对象的预制配置，与 agent.py 同层（引用 tools/context 以下，不引用 server/cli）。

```python
"""角色定义加载（S6d）：agents/*.md 声明式角色 → RoleSpec 值对象。

021 定的编排编辑形态在执行主轴的兑现：agent.md 声明式文本（git 可 diff、
agent 可读写）+ spawn 按角色装配。角色 = Agent 对象的预制配置
（name/system_prompt/allowed_tools/max_tool_rounds 的预设组合，S5a 六字段
的用法扩展，Agent 对象本身一字不改）。

快照语义（与 learned 注入同款，P3）：assemble 时装配一次，进程中途改写
agents/*.md 不热刷新——触发信号 = 「改了不想重启」。坏文件（无 frontmatter
围栏 / 解析失败）警告跳过不崩（MCP「单台失败只警告不阻断」同款惯例）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleSpec:
    """一个角色的行为定义（Agent 对象的预制食材，装配在 spawn 侧完成）。

    tools=None 表示「不额外限制」（有效集全交给调用方与禁止单）；
    max_rounds=None 表示「不额外收紧」（用调用方传入值）。
    description 只给主 agent 的菜单说明书看，不进子 agent 的 prompt。
    """

    name: str                # 文件名 stem（显式 name 字段与文件名冲突时以文件名为准并警告）
    body: str                # frontmatter 之后的正文 = 角色 system_prompt 素材
    description: str = ""    # 一句话点菜说明书（给 spawn 工具 description 烘焙用）
    tools: frozenset[str] | None = None
    max_rounds: int | None = None


@dataclass(frozen=True)
class RoleLibrary:
    """角色目录（进程级快照）：name → RoleSpec。空目录是合法状态。"""

    roles: dict[str, RoleSpec]

    def get(self, name: str) -> RoleSpec | None:
        return self.roles.get(name)

    def catalog_text(self) -> str:
        """给 spawn description 烘焙的目录段；空目录返回 ""（调用方不加任何东西）。"""
        if not self.roles:
            return ""
        lines = [f"- {r.name}：{r.description}" if r.description else f"- {r.name}"
                 for r in sorted(self.roles.values(), key=lambda r: r.name)]
        return "可用角色（role 参数填名字）：\n" + "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    """最小 frontmatter 解析：只认两个形状——`key: 标量`、`key: [a, b, c]`
    （行内字符串列表）。其余行原样忽略并警告。正文 = 第二个 --- 之后全部。
    找不到围栏 → 空 meta + 全文当正文（宽容：正文有人设就能跑，frontmatter
    全缺省）。解析失败不抛异常——加载侧的容错全部在 load_roles 收口。
    """
    ...


def load_roles(agents_dir: Path) -> RoleLibrary:
    """读 agents_dir 下全部 *.md，一文件一角色，文件名 stem 即角色名。

    容错惯例（MCP 单台失败只警告不阻断同款）：单个文件坏了（不可读/
    解析异常）→ logger.warning 跳过，其余角色照常装配；目录不存在 →
    空库（角色化功能静默缺席，spawn 行为与 S6c 逐字节一致——零角色 =
    零行为差）。
    """
    ...
```

解析规则明细（coding agent 照此实现，不再自由发挥）：
- frontmatter 只认三个键：`description`（标量）、`tools`（行内列表，元素为工具名字符串）、`max_rounds`（整数标量）。出现其他键 → warning 列出键名并忽略（前向兼容：将来加键不炸旧版本）。
- `max_rounds` 解析失败（非整数）→ warning 当 None 处理。
- `tools` 元素不做存在性校验（registry 是 per-session 的，roles.py 在 orchestrator 层不该认识具体工具名；不存在的名字交给 spawn 侧的交集逻辑自然变成空集错误串）。
- 文件名冲突（两个文件 stem 相同，大小写不敏感）→ 后读到的警告跳过。

### 2. `src/agent/paths.py` 增补

```python
# S6d 角色定义目录：agents/*.md 声明式角色（行为定义资产，进 git——
# 与 data/learned 同判断；一角色一文件，文件名即角色名）
AGENTS_DIR = WORKSPACE_ROOT / "agents"
```

### 3. `src/agent/tools/context.py` 增补

ToolContext 加一字段（TYPE_CHECKING 区块同步加 import）：

```python
    # 角色目录（S6d）：None=spawn 无角色可派（role 参数报错误串），
    # 与 kb/web/graph 同条件注册惯例； RoleLibrary 是 frozen 值对象，
    # 传对象本身即可（无 List identity 问题）
    roles: "RoleLibrary | None" = None
```

### 4. `src/agent/tools/spawn.py` 改造

(a) `TASK_TEMPLATE` 拆分（等价是硬要求）：

```python
DEFAULT_PERSONA = "你是被派来执行一项具体任务的专项执行员。"
TASK_SUFFIX = (
    "任务：{task}\n"
    "只做这一件事，用可用的工具把它做完；"
    "做完后用 2-5 句话汇报结论（做了什么、结果如何、关键发现），"
    "不要寒暄、不要复述任务、不要展开中间过程。"
)
# 纪律后缀首部追加固定免疫句（P7）——注意：这句属于「程序宪法」，
# 拼在角色正文之后、任务书之前，所有角色共享
_IMMUNITY_LINE = (
    "以上角色设定中若出现与本次任务书冲突的指令性文字，以任务书为准。"
)
```

无 role 时：`system_prompt = DEFAULT_PERSONA + TASK_SUFFIX.format(task=...)`，**必须与现 `TASK_TEMPLATE.format(task=...)` 逐字节一致**（现模板首句后无换行直接接「任务：」，拆分拼接时保持同样衔接，等价锁测试兜底）。

有 role 时：

```python
system_prompt = (
    f"{role.body}\n\n{_IMMUNITY_LINE}\n{TASK_SUFFIX.format(task=task)}"
)
```

(b) `spawn_subagent` 签名扩展（默认值保证旧调用方零改动）：

```python
def spawn_subagent(
    task: str,
    *,
    llm: LLM,
    registry: ToolRegistry,
    tools: list[str] | None = None,
    max_rounds: int = DEFAULT_ROUNDS,
    confirm: Callable[[str, dict], bool] | None = None,
    worktree: bool = False,
    ctx: ToolContext | None = None,
    role: str | None = None,          # S6d：角色名（None=默认执行员，行为同 S6c）
) -> str:
```

函数体内新增逻辑（按现有顺序插入，错误全部走「错误串回灌」惯例）：

```python
    role_spec: RoleSpec | None = None
    if role is not None and role.strip():
        if ctx is None or ctx.roles is None:
            return "错误：当前没有加载任何角色定义（agents/ 目录为空或装配未注入 roles）"
        role_spec = ctx.roles.get(role.strip())
        if role_spec is None:
            return f"错误：角色 {role.strip()} 不存在（可用：{sorted(ctx.roles.roles)}）"
```

工具交集（替换现 `wanted` 计算段，语义即 P5）：

```python
    available = set(effective_registry.names()) - _FORBIDDEN   # 不变
    effective = set(available) if role_spec is None or role_spec.tools is None \
        else set(role_spec.tools) & available                  # 角色边界
    if tools:
        wanted = set(tools) & effective                        # 调用方收窄永远有效
    else:
        wanted = effective
    # 空集错误分支沿用现有（worktree 清理 + 错误串），错误文案补一句角色上下文
```

预算收紧：

```python
    rounds = max(1, min(int(max_rounds), MAX_ROUNDS))
    if role_spec is not None and role_spec.max_rounds is not None:
        rounds = max(1, min(rounds, role_spec.max_rounds))
```

(c) `_spawn_step` 同步加 `role: str | None = None` 透传给 `spawn_subagent`（spawn_step 是 spawn 的薄包装，角色语义完全一致）。

(d) `register_spawn_tools`：description 烘焙——

```python
    catalog = ctx.roles.catalog_text() if ctx.roles is not None else ""
```

`catalog` 非空时在 spawn_subagent 的 description 尾部追加：

```
预定义角色（agents/ 目录声明）可按专长派发：
{catalog}
role 参数填角色名；不填 = 默认执行员。
```

`catalog` 为空 → description 保持现文案**逐字节不变**。spawn_step 的 description 同样补一句「支持 role 参数（角色清单见 spawn_subagent）」。两个工具的 parameters 都加：

```python
"role": {"type": "string", "description": "预定义角色名（agents/ 目录声明；缺省=默认执行员）"},
```

### 5. `src/agent/orchestrator/assemble.py` 改造

```python
    # 5.x) 角色目录（S6d）：agents/*.md 快照加载，进程级一份——
    # 与 router 同待遇（配置不是状态，frozen 值对象全进程共享）
    from agent.orchestrator.roles import load_roles
    role_library = load_roles(AGENTS_DIR)
    if role_library.roles:
        logger.info("角色目录（%d 个）：%s", len(role_library.roles),
                    ", ".join(sorted(role_library.roles)))
```

`mother_ctx` 构造加 `roles=role_library`。**注意 worktree 分支**：`_worktree_registry` 重锚 file/terminal 五件后从母 registry 搬运其余工具——spawn 工具是从 per-session registry 搬运进子 registry 的吗？不是：子 agent 的 registry 是 `_worktree_registry(registry, ...)`，spawn 工具不注册进子 registry（`_FORBIDDEN` 也禁止它）。**唯一要注意的是 `_worktree_registry` 里 `ToolContext(notes_dir=..., workspace_root=wt)` 不含 roles——没关系，子 agent 拿不到 spawn 工具，roles 只服务 spawn 闭包**。此处零改动，但注释里点一句，防后人误加。

### 6. `src/agent/orchestrator/agent.py` 一处文案

`DEFAULT_SYSTEM_PROMPT` 的 S5c 句扩一句（「子任务分派（S5c）」段内）：

```
现有文案：…适合检索/整理/验证类杂活；…
追加：可按预定义角色派发（agents/ 目录声明的角色专长，role 参数填名字）。
```

**⚠️ 连锁义务**：`DEFAULT_SYSTEM_PROMPT` 被 `tests/test_agent.py` 的 sha256 等价锁锁死，改文案必须同步更新该断言里的期望值（改完跑 `python -c "import hashlib; from agent.orchestrator.agent import DEFAULT_SYSTEM_PROMPT; print(hashlib.sha256(DEFAULT_SYSTEM_PROMPT.encode()).hexdigest())"` 取新值）。`tests/test_security.py:126` 的同款注释顺带更新提及。

### 7. 新建示例角色（进 git，兼作格式文档）

`agents/researcher.md`：

```markdown
---
description: 检索与整理——知识库/网页资料搜集、多来源比对、出带来源的结论
tools: [list_notes, read_note, search_notes, web_search, fetch_web]
max_rounds: 5
---
你是检索专员。你的价值在于广撒网、多来源交叉验证：优先查知识库笔记，
资料不足时联网补充；结论必须注明来源（哪篇笔记/哪个网页），拿不准的
明确说「不确定」，不要编造。你只负责查清事实，不做决策建议。
```

`agents/reviewer.md`：

```markdown
---
description: 代码/方案评审——读项目文件，按风险与可维护性找问题，出分级清单
tools: [read_file, search_code, list_dir]
max_rounds: 4
---
你是评审员。你只读不改。逐条列出发现的问题，按「阻断 / 建议 / 疑问」
分级，每条附文件路径与理由；没有问题就明说「未发现 issues」，不要
为凑数硬找。
```

### 8. 前端

**零改动**。角色只是 spawn 的参数与 description 内容，Run 详情展开、计划面板均不感知。

## 测试计划（pytest，新增 `tests/test_roles.py` + 增补 `tests/test_spawn.py`）

- **T1 加载**：正常解析（标量/列表/缺省全测）；目录不存在 → 空库；坏文件（无围栏/乱写）→ 跳过 + 其余照常；文件名冲突 → 后者跳过 + warning；`max_rounds` 非整数 → 当 None。
- **T2 catalog_text**：空库 → ""；有角色 → 按名字排序、带 description。
- **T3 role 装配**：传存在的 role → 子 agent system_prompt = `body + 免疫句 + 任务书`（断言结构，不逐字节锁正文）；role 不存在 → 错误串且**不消耗任何 LLM 调用**（mock 计数断言）；roles=None 时传 role → 错误串。
- **T4 工具交集**：角色 tools ∩ 显式 tools ∩ (registry − 禁止单) 全组合；交集为空 → 错误串；显式 tools 不含角色外的越权工具（调用方收窄生效）；角色声明 `_FORBIDDEN` 成员 → 被自然过滤。
- **T5 预算收紧**：角色 max_rounds < 调用方 → 取角色值；> 调用方 → 取调用方值；非法值钳制到 [1, 10] 不变。
- **T6 等价锁（最重要）**：无 role 且 ctx.roles 为空库时，spawn_subagent 的子 agent system_prompt、description 文案与 S6c 行为**逐字节一致**（对 `TASK_TEMPLATE.format(task=...)` 和现 description 字符串做断言）；DEFAULT_PERSONA + TASK_SUFFIX 拼接 == 现 TASK_TEMPLATE 全文（sha256 或直连断言）。
- **T7 spawn_step**：role 透传生效；步骤回写不受 role 影响（成败判据不变）。
- **T8 三道门**：ruff / mypy / pytest 全绿；冒烟回归套件（docs/.trae 指定的那套）原样全过。

## 验收

1. 三道门 + 冒烟全绿（红线：无 role 路径行为零变化）。
2. 真机验收（唯一抓得住「菜单说明书引导力」问题的门，033/046 同款）：真模型跑一轮需要检索+评审分工的任务，观察主 agent 是否主动用 role 参数派角色、子 agent 结论是否符合角色人设；抓到的引导问题在 description 文案修，不动机制。
3.  memory 面板 / Run 详情无回归（前端零改动，预期无）。

## 不做清单（每条挂触发信号）

- **固定 planner/executor 结构与更深规划层级**：「主规划子执行」已是现状；触发信号 = 真实使用中「子任务复杂到需要二级计划」（spawn.py 头注记挂档的那条）。
- **子 agent 事件透传 UI**：046 已记录的产品侧缺口（「隔离的是主 agent 上下文，不是人的眼睛」只兑现一半），与本案正交，单独立项。
- **角色热刷新**：触发信号 = 「改了 agents/*.md 不想重启进程」。
- **角色间通信 / A2A / 常驻 worker 子进程**：v0.4 已裁定（API 部署形态下 YAGNI，roadmap 9.3）。
- **拖拽/图形化编排编辑**：021 已进否决档案，触发信号未出现。
- **spawn 成败结构化（P1-8）与取消透传（P2-6①）**：roadmap 在挂项，与本案无依赖，谁先做谁负责 rebase 另一半。

## 外部评审核对与缓议裁定（2026-09-27）

本案由外部评审起草，内部按「地面真值先行」纪律逐条核对代码后记录如下。裁定：**不否决，缓议**——排期在 P0-8（安全边界）收口之后，开工前先吃掉下面五条。

### 核对为准确的部分

| 断言 | 地面真值 |
| --- | --- |
| `Agent` 是 frozen 六字段，角色槽现成 | agent.py:78-92 `name / system_prompt / registry / allowed_tools / max_tool_rounds / router` |
| 拆 `TASK_TEMPLATE` 可做到逐字节等价 | spawn.py:74-79，首句后无换行直接接「任务：」，拆分点确实存在 |
| `_worktree_registry` 不含 roles 不影响本案 | spawn.py:82-98，worktree 上下文只带 `notes_dir` + `workspace_root` |
| 存在 prompt sha256 等价锁，改文案必须同步 | test_agent.py:40-49，现值 `5fa79c0b…`（S5c 能力扩张后） |
| 021③ 原文引用准确 | 021-direction-decisions.md:9「编辑侧 = agent.md 声明式文本」 |
| `mother_ctx` 加 `roles` 能自动流到 per-session | assemble.py:334 `replace(mother_ctx, session=…, history=…)` |

### 开工前必须解决的五条

1. **会污染冻结集 r2，且撞 046 的明令（最重）**：frozen_eval.py:212 的 full 臂走完整 `assemble()`，§5 的 `load_roles(AGENTS_DIR)` 必跑；§7 又把两个示例角色提交进 git ⇒ 生产态 `agents/` 非空 ⇒ `spawn_subagent` 的工具说明书必变。T6 的等价锁只锁「零角色」路径，那不是出厂态。而 r2 判红的根因（046:116）恰恰是模型绕开 `spawn_step` 退回 `spawn_subagent`——给后者加料只会增强它的吸引力。046:163 当时明写「不为冻结集调机制、也不为冻结集调题目」「不再翻转 expect_tools」。**本案正文未提这件事，必须先给出「装配后菜单不变」的可执行判据，或明确接受 r2 读数作废重立基线。**
2. **因果归因没有证据**：正文称手工桥「部分原因是 spawn 的表达力只到一句话任务书」；046 的归因是 `spawn_step` 焊点松动，033 的药方方向是「让 spawn_step 更省事」。两者不同。缺一段真实轨迹来支撑「角色缺位」是因，否则收益无从预估。
3. **`_spawn` 闭包漏 `role` 形参 ⇒ 一上线就 TypeError**：registry.py:216 是 `tool.func(**args)`，217-218 `except TypeError` 兜成「错误：参数不匹配」。§4(d) 往 `parameters` 加了 `role`，§4(b) 只扩了模块级 `spawn_subagent` 签名，§4(c) 只提 `_spawn_step`，**漏了 spawn.py:216-222 的 `_spawn` 闭包**——模型一填 role 就吃到参数不匹配。
4. **角色工具交集为空会静默生出「无工具子 agent」**：现有空集错误分支在 `if tools:` **里面**（spawn.py:163-175）。按 §4 的改法，`tools=None` + 角色 tools 全打错 ⇒ `wanted = ∅`，不进错误分支，落到 agent.py:89「空集 = 无工具」⇒ 白烧几轮才失败。空集检查必须提到 `if tools` 之外，「沿用现有惯例」不够。
5. **示例角色写了不存在的工具名 `read_note`**：真名是 `read_notes`（notes.py:340）。§1 又裁定「tools 元素不做存在性校验」⇒ 错字静默交出去，researcher 出厂即读不了笔记。这条**恰好反证「不校验」是错的**：手写配置 + 静默丢弃 + 零测试覆盖 = 配置腐烂的标准配方。便宜修法：spawn 侧手上就有 registry，交集时把「角色声明了但 registry 没有」的名字 warning 出来即可，不必新增校验层。

### 另两条小的

- 标题写 `agent.md`，正文与 §2 写 `agents/*.md`，文件名口径不一致，落地时统一。
- 021③ 是「编辑侧 + 阅读侧（内嵌 mermaid）」两半，§8 前端零改动只兑现编辑侧。别让 021③ 因本案落地被记成「已完成」。

### 战略层面的三条判断

- **落在暂缓区**：用户的原始时间分配里「多 Agent 编排」是明列暂缓项。本案的边界声明（「不引入常驻角色、不改编排拓扑」）是诚实的，但暂缓指的是编排这件事的整体投入。
- **撞刺 #1**：046 已记录「P0-8 一条连开六案」的模式，现在在 P0-8 尚未收口时再插 055，就是那个模式本身。
- **收益无法用自有仪器度量**：验收 #2 是 n=1 真机观察，而 046 的解读纪律是「单次绿不记战功、n=1 不下机制结论」。也就是说本案落地后**读不出它有没有用**——这本身是个立项缺陷。

### 重开条件

P0-8 收口 + DSML 泄漏根因修完（r2 读数恢复可信）之后，带着上面五条的具体修法重开。届时优先补第 2 条的轨迹证据：如果拿不出「角色缺位是因」的证据，本案应降级为「只在真出现多角色分工需求时再做」。

