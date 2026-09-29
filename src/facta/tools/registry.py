"""工具注册表：agent 的「手」。

核心认知（M5 最重要的一句话）：
模型从不执行任何东西——它只是返回"我想调用哪个工具、参数是什么"（JSON），
真正执行函数的是我们的程序。工具清单是我们给的，函数是我们实现的，
模型只能在"菜单"里点菜。

Tool：一个工具的四要素（名字/说明书/参数schema/函数本体）
ToolRegistry：登记所有工具，对外提供两件事——
  1. schemas()  → 生成给模型看的"菜单"
  2. execute()  → 按模型点菜执行，返回字符串结果（错误也返回字符串）
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from facta.core.audit import AuditLog
from facta.tools.sandbox import detect_backend


def _validate_args(args: dict, parameters: dict) -> str | None:
    """按 JSON Schema 最小子集校验参数结构；返回错误描述，合法返回 None。

    校验三维分工里的「结构层」——语法层（json.loads）与语义层（工具函数
    自身抛异常）之外的补全。只实现 required + 基础类型：
    花哨的 anyOf/$defs 是给模型看菜单用的，执行防线只需要
    「缺了必填」和「类型错了」两道；未声明字段放行（宽松，不矫枉过正）。
    """

    def type_name(spec: dict) -> str | None:
        # anyOf 等复合写法（MCP 三方 schema 常见）没有单一 type → 跳过
        return spec.get("type") if isinstance(spec.get("type"), str) else None

    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"
    properties = parameters.get("properties", {})
    for key in parameters.get("required", []):
        if key not in args:
            return f"缺少必填参数 {key}"
    for key, value in args.items():
        spec = properties.get(key) or {}
        expected = type_name(spec)
        if not expected:
            continue
        actual = type(value).__name__
        if expected == "string" and not isinstance(value, str):
            return f"参数 {key} 应为字符串，实际 {actual}"
        if expected == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
            return f"参数 {key} 应为整数，实际 {actual}"  # bool 是 int 子类，显式排除
        if expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            # 括号必须齐全：and 优先级高于 or，漏括号会让所有 bool 值
            # 一律误报「应为数字」（list_todos 的 only_pending:true 曾中招）
            return f"参数 {key} 应为数字，实际 {actual}"
        if expected == "boolean" and not isinstance(value, bool):
            return f"参数 {key} 应为布尔，实际 {actual}"
        if expected == "array" and not isinstance(value, list):
            return f"参数 {key} 应为数组，实际 {actual}"
        if expected == "object" and not isinstance(value, dict):
            return f"参数 {key} 应为对象，实际 {actual}"
    return None


@dataclass
class Tool:
    """一个工具 = 元信息 + 函数本体。

    name/description/parameters 都是给模型看的"使用说明书"：
    模型能不能正确选用这个工具，全靠 description 写得好不好。

    is_readonly（S3 权限分级）：True=只读（L0，结果截 100 字审计）；
    False=写类（L1，结果截 500 字）。默认 False 是保守选择——不声明的
    工具按写类处理（审计多记不错，漏记才错）。

    needs_confirmation（S4b L2 高危确认）：True=每次调用都需用户裁决；
    也可传 callable(args)->bool 按参数动态判定（run_command 的白名单：
    只读命令免确认，其余弹窗）。裁决走 execute 的 confirm 缝；
    无 confirm 通道时按拒绝处理（保守默认：没有眼睛就不动手）。
    050 起返回值可以是**规则名**（非空 str）：真值语义不变（非空 str 为真、
    None 为假），额外把「撞的是哪道围栏」写进审计的 extra["guard"]——
    否则安全叙事只能靠模型自述（049 执行校正 ⑥）。

    idempotent（P0-3 / 038 P2 崩溃恢复）：True=同样参数重复执行，效果与执行
    一次相同、无累积副作用。恢复时用它决定悬挂调用的处置文案——幂等的可以
    放心重做，非幂等的必须先核验现场（写没写进去、跑没跑过）。
    只读工具天然幂等，不必重复声明：判定处（orchestrator/checkpoint.py）按
    `is_readonly or idempotent` 合并。本字段专给「写类但可安全重做」的那批
    （write_file 覆写同内容、sync_graph 全量重建）。默认 False 是保守方向：
    没声明的一律按「重做可能出双重副作用」处理（add_todo 就是这种——重复
    调用会加两条）。
    """

    name: str                       # 工具名，模型用它"点菜"
    description: str                # 说明书：什么时候该用这个工具
    parameters: dict                # JSON Schema：参数结构
    func: Callable[..., str]        # 真正执行的 Python 函数
    is_readonly: bool = False       # S3 权限分级：只读 L0 / 写 L1（保守默认写类）
    idempotent: bool = False        # P0-3 崩溃恢复：写类工具能否安全重做（只读免声明）
    # S4b L2 确认标记；050 宽化：非空 str = 命中的规则名（真值语义不变）
    needs_confirmation: (
        bool | str | None | Callable[[dict], bool | str | None]
    ) = False
    sandboxed: bool = False           # 048：True=执行走进程级沙箱，审计条目
                                      # 带 sandbox=seatbelt/off 标记（批准拒绝都带）
    receives_confirm: bool = False  # S5c 编排工具标记：func 额外接收 confirm 参数
                                    # （spawn_subagent 把主循环的确认缝透传给子执行流——
                                    # 子 agent 的高危工具照常弹确认，人审不分主子）
    receives_event: bool = False    # 059 编排工具标记：func 额外接收 event 参数
                                    # （spawn 把主循环的事件缝透传给子执行流——子 agent 的
                                    # 工具过程以 sub.* 命名空间进父事件流：隔离的是主 agent
                                    # 上下文，不是人的眼睛。默认 False，老工具零改动）


class ToolRegistry:
    """登记工具 + 生成菜单 + 执行点单。"""

    def __init__(
        self,
        audit: AuditLog | None = None,
        scope_check: Callable[[str, dict], str | None] | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._audit = audit   # S3 审计：None=不落盘（测试/教学路径）；装配层注入真 log
        # P0-8（057）计划工具范围闸门：(工具名, 参数) → 错误串（越界）或 None（放行）。
        # 公开属性不设 property——挂它的是 tools/plan.py（策略在 plan 域，registry 只认
        # 回调，依赖方向不反），继承它的是 spawn._worktree_registry（与 audit 同源同理）。
        self.scope_check = scope_check

    def register(self, tool: Tool) -> None:
        """登记一个工具（名字重复时后者覆盖前者）。"""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """把工具从菜单摘除（MCP-c：服务器已死时摘掉死菜，模型不再撞墙）。"""
        self._tools.pop(name, None)

    def names(self) -> list[str]:
        """当前已登记的工具名清单（给外部展示用）。"""
        return list(self._tools.keys())

    @property
    def audit(self) -> AuditLog | None:
        """审计实例（只读暴露——S6a 子 registry 构造需审计同源收口）。"""
        return self._audit

    def get(self, name: str) -> Tool | None:
        """取工具对象（S6a 子 registry 搬运用——同一 Tool 对象可注册进
        多个 registry，闭包锚定的资源（kb/todos 等）随之共享）。"""
        return self._tools.get(name)

    def tool_descriptions(self) -> dict[str, str]:
        """名字→说明书（M10 场景路由的 criteria 原料——Jev 按语义选工具，
        description 本来就是给模型看的使用说明书，直接复用）。"""
        return {name: t.description for name, t in self._tools.items()}

    def schemas(self) -> list[dict]:
        """生成 OpenAI 格式的工具清单——这就是发给模型的「菜单」。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def execute(
        self,
        name: str,
        arguments_json: str,
        confirm: Callable[[str, dict], bool] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> str:
        """执行模型点的菜。注意：错误也返回字符串，而不是抛异常。

        为什么？——错误信息回填给模型后，模型能自我纠正重试。
        崩溃没有意义；让模型看到"哪里错了"才有意义。这是 agent 的容错反馈环。

        confirm（S4b L2 缝）：工具标了 needs_confirmation 时，裁决回调
        （名字+参数 → 批准/拒绝）。拒绝不执行，回灌「用户拒绝」让模型换
        方案；无 confirm 通道按拒绝处理（保守默认）。批准与拒绝都落审，
        且**裁决结果两条路都回灌给模型**（054：批准原先静默，模型只能靠
        「结果回来了」反推「没弹框」，实测幻觉出不存在的白名单项）。
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：不存在名为 {name} 的工具"

        # 模型给的 arguments 是 JSON 字符串，先解析成 dict
        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
        except json.JSONDecodeError as e:
            return f"错误：参数不是合法的 JSON（{e}）"

        # 结构层校验（JSON Schema 最小子集）：语法合法但缺必填/类型错，
        # 同样以错误字符串回给模型——自我纠正反馈环在两层校验间无差别
        error = _validate_args(args, tool.parameters)
        if error:
            return f"错误：参数校验失败（{error}）"

        # P0-8（057）计划工具范围闸门：越界在执行前拦掉（细节见 _scope_denial）
        denied = _scope_denial(self, tool, name, args)
        if denied:
            return denied

        # S4b L2 确认：裁决点在执行前。拒绝走同一审计收口（留痕可查）
        needs = (
            tool.needs_confirmation(args)
            if callable(tool.needs_confirmation)
            else tool.needs_confirmation
        )
        # 050 归因：needs 可能是规则名（非空 str）而不是 bool——摘出来带进审计。
        # 批准与拒绝两条路径都要带：i4 那次确认是**批准后执行**的，只记拒绝路径
        # 就正好漏掉要归因的那一条。
        guard = needs if isinstance(needs, str) else None
        if needs and (confirm is None or not confirm(name, args)):
            result = "用户拒绝了这次操作（未经确认不执行）。请换方案，或先向用户说明理由再重试。"
            _record(self._audit, tool, name, args, result, guard)
            return result

        try:
            # 编排缝注入（S5c confirm / 059 event）：作为关键字参数注入——
            # 显式声明而非 registry 隐藏状态（接口演进老规矩：默认 False，
            # 老工具零改动）。func 签名须有同名形参（spawn_subagent 两个都有）
            extra: dict = {}
            if tool.receives_confirm:
                extra["confirm"] = confirm
            if tool.receives_event:
                extra["event"] = on_event
            result = tool.func(**extra, **args)
        except TypeError as e:
            result = f"错误：参数不匹配（{e}）"
        except Exception as e:  # 兜底：工具内部任何异常都不让程序崩溃
            result = f"错误：工具执行失败（{type(e).__name__}: {e}）"
        else:
            result = str(result)
            # 037 P2（SWE-agent ACI）：空输出显式化——任何工具（含 MCP）
            # 返回空串都在这唯一收口点替成显式标记，模型不再把「什么都没
            # 返回」误读成「执行成功只是没内容」或「调用丢了」。
            if not result.strip():
                result = "（无输出）"

        # 054 批准痕迹：必须拼在空输出显式化**之后**——痕迹非空，先拼会让
        # 037 P2 的「（无输出）」永不触发。抽成函数是为了 execute 不超分支预算。
        result += _approval_trace(needs, guard)

        # S3 审计收口：所有工具调用（含失败）在这里落盘——单一必经点，
        # 新工具零成本继承。失败也记（result 是错误串，事后可查）。
        _record(self._audit, tool, name, args, result, guard)

        return result


def _record(
    audit: AuditLog | None,
    tool: Tool,
    name: str,
    args: dict,
    result: str,
    guard: str | None,
) -> None:
    """审计落盘收口（057 抽出）：三处调用点（确认拒绝／正常执行／范围越界）
    共用同一份 extra 组装。抽出前是三份逐字重复的 `if audit is not None`，
    抽出后顺带把 execute 的分支数压回 ruff PLR0912 预算内——不提高阈值。
    """
    if audit is not None:
        audit.record(name, args, result, tool.is_readonly, extra=_audit_extra(tool, guard))


def _scope_denial(registry: ToolRegistry, tool: Tool, name: str, args: dict) -> str | None:
    """P0-8（057）计划工具范围闸门：越界**直接拒，不进 L2 弹窗**——弹窗等于给
    注入多一次「说服人批准」的机会（i 系列已多次观测到人照样批准）。升级路径
    另有其门：错误串指路 make_plan 修订，而 make_plan 自己 needs_confirmation=True
    ⇒ 扩大范围必经人审、绕过范围不必经人审，方向是对的。

    拒绝也落审计，且 guard 复用 050 的同一份 extra["guard"] ⇒ 评测 record 的
    guards 字段零新仪器就能看到「机制有没有出手」。抽成函数与 054 的
    _approval_trace 同款理由：execute 的分支数已撞 ruff PLR0912，不提高阈值。
    """
    if registry.scope_check is None:
        return None
    denied = registry.scope_check(name, args)
    if denied:
        _record(registry.audit, tool, name, args, denied, "plan-scope")
    return denied


def _approval_trace(needs: object, guard: str | None) -> str:
    """054 批准痕迹：批准路径原本对模型静默——回灌的只有工具原始输出，模型
    无从得知这次调用经过了人审，只能从「结果回来了」反推「大概没弹框」。
    实测后果（data/audit 2026-09-27T14:16:00 那条 `sleep 30`，guard 明记
    not-whitelisted）：模型幻觉出「没弹确认，被当只读放行」+「sleep 居然在
    只读白名单里」，并据此两次提议去摘一个不存在的白名单项。与拒绝路径的
    固定文案对称——裁决结果两条路都回灌，模型不必猜。
    规则名复用 guard（050 的同一份 _confirm_rule 返回值），不另立真值源；
    拼进 result 后审计记的与模型所见逐字一致。免确认路径返回空串＝零行为差。
    """
    if not needs:
        return ""
    return f"\n（本次调用经用户确认批准{f'；命中规则：{guard}' if guard else ''}）"


def _audit_extra(tool: Tool, guard: str | None = None) -> dict[str, str] | None:
    """048/050 审计打标：沙箱工具的条目带 sandbox=seatbelt/off（批准拒绝都带），
    命中确认规则的带 guard=规则名；两者都没有则 None=零行为差。
    which 毫秒级，不为省一次探测做缓存。"""
    extra: dict[str, str] = {}
    if tool.sandboxed:
        extra["sandbox"] = detect_backend() or "off"
    if guard:
        extra["guard"] = guard
    return extra or None
