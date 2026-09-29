"""P0-3 Run checkpoint 与崩溃恢复（038 决策记录）。

问题：崩溃（kill / 断电 / OOM）发生在工具执行中间时，会留下两处残局——
  ① session.json 里 assistant 已点菜、tool 结果还没回填（孤儿 tool_calls，
     下次发给 API 直接 400）；
  ② 更糟的是无从判断那次调用「跑没跑过」：重放可能出双重副作用
     （add_todo 加两条、run_command 再扣一次款）。

038 的裁定是不做全量内存快照，只记「重建上下文的最小状态」。落地拆成两件：

  session.json（既有）= 底片真值源。在工具边界原子落盘（P0-3 顺手把
    save_session 改成 tmp + os.replace，半截 JSON 从此不可能存在）。
  {sid}.jsonl（本模块）= append-only 账本，只存底片表达不了的东西：
    intent —— 「这次调用发起过」的事实（底片里的 tool_calls 分不清
              「没跑」和「跑了但结果丢了」，这条能分）
    result —— 工具结果全文（heal 时原样回注，不重跑；账本不截断，
              与 audit 的 100/500 字摘要相反——那份是给人查的，这份要还原现场）

恢复语义（038 P2「已成功的非幂等调用不重复执行，改为回注其原始结果」）：
    heal() 把悬挂的 tool_calls 逐条补齐成合法底片，然后 run_turn(user_text=None)
    从现场往前走——**不重放任何调用**。副作用天然不会重复，因为根本没有第二次执行。
    补齐的 tool 消息内容分三档（有结果 / 发起过没结果 / 没发起过），
    并按 Tool.idempotent（只读工具免声明）告诉模型这一条能不能放心重做。

038 P3/P4 的边界照旧：粒度只到工具事件，不引图编排框架；只覆盖主 loop，
子 agent / worktree 交互不在本轮范围（spawn 的整段子任务崩溃后按
「发起过、结果未知」的非幂等档处理——保守方向）。

ponytail: 账本每轮 begin() 时截断重写，只保留当前 run 的记录——历史 run 的
账本没有消费者（底片已经是最终态），留全量等于无界增长。真要回溯执行史，
audit/ 那份才是长期档案。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent.core.types import Message
from agent.memory.store import Session
from agent.paths import CHECKPOINT_DIR
from agent.tools.registry import ToolRegistry


def _now() -> str:
    """账本时间戳（秒级 UTC，人可读、可 diff）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def ledger_path(sid: str, directory: Path = CHECKPOINT_DIR) -> Path:
    """会话 id → 账本文件位置。

    sid 已在 SessionStore 侧过白名单校验（`^\\d{8}-\\d{6}(-\\d+)?$`），这里不再
    重复校验：信任边界在 HTTP 路由那一层，本函数只被 store/装配层调用。
    """
    return directory / f"{sid}.jsonl"


@dataclass
class Ledger:
    """账本的内存投影：一次 run 里「发起过哪些调用、拿到了哪些结果」。"""

    intents: dict[str, str] = field(default_factory=dict)    # tool_call id → 工具名
    results: dict[str, str] = field(default_factory=dict)    # tool_call id → 结果全文


def read_ledger(path: Path) -> Ledger:
    """读账本。文件不存在 → 空账本；坏行跳过。

    坏行不是防御性编程而是主场景：进程被 SIGKILL 时，最后一行很可能只写了
    半截 JSON。丢掉半截行 = 丢掉「最后一次调用的结果」，heal 会把它当
    「发起过、结果未知」处理——保守方向，正确。
    """
    ledger = Ledger()
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ledger
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue                      # 半截行：崩溃现场，见 docstring
        cid = record.get("id")
        kind = record.get("type")
        if not cid or kind not in ("intent", "result"):
            continue                      # run/done 行不参与配对
        if kind == "intent":
            ledger.intents[cid] = str(record.get("name", "?"))
        else:
            ledger.results[cid] = str(record.get("result", ""))
    return ledger


class CheckpointWriter:
    """把 run 的执行事实写进账本 + 在工具边界落盘底片。

    挂在既有 on_event 缝上当消费者（不给 run_turn 加第六条缝——loop 不认得
    checkpoint，它只是照常发事件）。为此 tool_started/tool_result 的 payload
    各带一个 id 字段（P0-3 加的），前端忽略未知字段。

    save 是无参回调（调用方绑定 session 与路径）：writer 因此完全不懂
    SessionStore，测试可以直接塞 lambda。
    """

    def __init__(self, ledger: Path, save: Callable[[], None]) -> None:
        self._ledger = ledger
        self._save = save

    def begin(self, run_id: str) -> None:
        """开一轮 run：截断旧账本（ponytail: 见模块 docstring）+ 记 run 行。"""
        self._ledger.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._ledger, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"type": "run", "run_id": run_id, "ts": _now()}, ensure_ascii=False
                ) + "\n")
        except OSError:
            pass   # 写不进去 = 本轮没有崩溃恢复能力，但不该因此拒跑任务

    def end(self, run_id: str, status: str) -> None:
        """收尾行：人工排查时能一眼看出这轮是正常结束还是被中断。"""
        self._append({"type": "done", "run_id": run_id, "status": status})

    def on_event(self, type_: str, data: dict) -> None:
        """消费 loop 的语义事件（只认工具与计划两类，其余忽略）。

        落盘策略（038 反方意见 2「I/O 开销」的答复）：
          tool_started → 先写 intent 再存底片。顺序不能反：intent 先落，
            崩溃后才能区分「没跑过」与「跑了结果丢了」。
          tool_result  → 只写账本，不存底片。结果全文进账本就够恢复用，
            下一次 tool_started / plan 变更 / 轮末 settle 会把它带进底片。
          plan.*       → 只存底片。计划状态住在 session.json 的 plan 段，
            账本不重复记。
        即「每工具一次全量写」——会话底片量级下（几十 KB）远小于一次 LLM
        往返的耗时。真要超限，升级路径是 delta JSONL（只追加新消息），
        不是现在就写。
        """
        if type_ == "tool_started":
            # intent 只记 id + 工具名：完整参数在底片的 assistant.tool_calls 里，
            # 账本再抄一份纯属重复（038 P1 说的「调用摘要」即此）
            self._append({
                "type": "intent",
                "id": data.get("id"),
                "name": data.get("name"),
            })
            self._save()
        elif type_ == "tool_result":
            self._append({
                "type": "result",
                "id": data.get("id"),
                "name": data.get("name"),
                "result": data.get("result", ""),
            })
        elif type_.startswith("plan."):
            self._save()

    def _append(self, record: dict) -> None:
        """追加一行（append-only，与 audit 同款写法）。失败静默：丢的是恢复
        精度不是用户数据（底片另有原子写兜着），不值得为它中断任务。"""
        record["ts"] = _now()
        try:
            with open(self._ledger, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _recovery_note(call_id: str, name: str, ledger: Ledger, registry: ToolRegistry) -> str:
    """给一条悬挂的 tool_call 编出「补位结果」文本。

    三档事实 × 两档幂等性。文本是给模型看的（它据此决定重做还是核验），
    所以直说现场状态，不含糊。
    """
    if call_id in ledger.results:
        return ledger.results[call_id]      # 038 P2：原样回注，不重复执行

    tool = registry.get(name)
    redoable = tool is None or tool.is_readonly or tool.idempotent
    # tool is None（工具已下线，如 MCP 服务器死了）按可重做处理：
    # 重做会得到「工具不存在」的错误串，无副作用风险。

    if call_id in ledger.intents:
        if redoable:
            return (
                f"[崩溃恢复] {name} 在上次进程被中断时已发起，结果没来得及落盘。"
                f"这个工具重复执行是安全的，需要它的结果就重新调用一次。"
            )
        return (
            f"[崩溃恢复] {name} 在上次进程被中断时已发起，是否执行完成、"
            f"现场变成什么样都未知。它会产生副作用，请先核验现场"
            f"（读文件 / 查状态），确认没做过再重做——不要盲目重试。"
        )
    return (
        f"[崩溃恢复] {name} 在上次进程被中断时账本里没有发起记录，通常意味着没跑过。"
        + (
            "需要的话可以正常调用。"
            if redoable
            else "但它会产生副作用，重做前先看一眼现场确认没做过。"
        )
    )


def heal(session: Session, ledger: Ledger, registry: ToolRegistry) -> int:
    """补齐底片尾部悬挂的工具轮次，返回补上的 tool 消息条数（0 = 底片本就完整）。

    与 compressor.trim_incomplete_round 是一对反向操作：trim 是「掐掉残局」
    （取消路径用——用户主动不要这轮了），heal 是「补齐残局」（恢复路径用——
    这轮的工作要接着做，掐掉等于丢掉已完成的调用）。

    只处理尾部一个区块：崩溃只可能发生在最后一次写入处，更早的轮次要么
    完整、要么已被当时的 trim 收拾过。
    """
    messages = session.messages
    ai = len(messages) - 1
    while ai >= 0 and messages[ai].role == "tool":
        ai -= 1                            # 跳过已回填的结果，找点菜那条
    if ai < 0:
        return 0
    pending = messages[ai]                 # 绑成名字：mypy 的窄化不认下标表达式
    if pending.role != "assistant" or not pending.tool_calls:
        return 0                           # 尾部是完整边界（user/assistant 纯文本）

    answered = {m.tool_call_id for m in messages[ai + 1:] if m.role == "tool"}
    healed = 0
    for call in pending.tool_calls:
        call_id = call.get("id")
        if not call_id or call_id in answered:
            continue
        messages.append(Message(
            role="tool",
            tool_call_id=call_id,
            content=_recovery_note(call_id, str(call.get("name", "?")), ledger, registry),
        ))
        healed += 1
    return healed
