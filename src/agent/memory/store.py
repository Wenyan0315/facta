"""M6 会话状态：既存底片（原文），也存压缩缓存。

M6.2 之前只把 messages（底片）落盘，summary / summarized_upto（压缩缓存）
是 run_chat 的局部变量，重启即清零——滚动摘要退化成「每次启动一次性全量大压缩」。
本模块把「底片 + 缓存」打包成一个 Session 对象整体持久化，才是完整的会话状态。

存储格式（version 预留演进）：
    {
      "version": 1,
      "messages": [ {"role","content","tool_calls"?,"tool_call_id"?}, ... ],
      "memory": { "summary": "...", "summarized_upto": 37 }
    }
旧格式（M6.2 及以前）是纯 [Message, ...] 列表，load 时识别并自动迁移。
"""

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from agent.core.types import Message
from agent.memory.plan import PlanBoard

SESSION_VERSION = 1


@dataclass
class Session:
    """一次会话的完整状态：底片（原文）+ 缓存（滚动摘要与覆盖进度）。

    这是「状态」的完整定义——不仅消息是状态，消息之上的摘要游标也是状态。
    只存消息不存游标，重启后游标归零，滚动摘要等于白做。
    plan（S5b）：会话级任务计划棋盘（单活跃 + 归档 + 事件史）——复杂任务
    跨多轮对话，计划生命周期=会话，跟底片/缓存同进同出。
    """

    messages: list[Message] = field(default_factory=list)
    summary: str | None = None
    summarized_upto: int = 1
    title: str | None = None   # S2 验收修复轮：归档展示标签（LLM 提炼，None=尚未提炼）
    plan: PlanBoard = field(default_factory=PlanBoard)   # S5b：plan-then-act 状态（空板=无活跃计划）


def save_session(session: Session, path: Path) -> None:
    """把会话状态（底片 + 缓存 + 标题）存成 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)   # 父目录不存在就建（第一次跑 data/memory/ 还不存在）
    data = {
        "version": SESSION_VERSION,
        "title": session.title,
        "messages": [asdict(m) for m in session.messages],
        "memory": {
            "summary": session.summary,
            "summarized_upto": session.summarized_upto,
        },
        # S5b：计划棋盘整体落盘（事件史全量在——「为什么跳步」重启后仍可查；
        # 传输队列 _pending 不落盘：它是本轮 Run 的传输状态，不属于会话）
        "plan": session.plan.to_dict(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)   # ensure_ascii=False：中文原样存，不变 \u 天书


def load_session(path: Path) -> Session:
    """读回会话状态。文件不存在 → 空会话（第一次跑很正常，别报错）；旧列表格式 → 自动迁移。"""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return Session()

    # 旧格式迁移：M6.2 及以前存的是纯 [Message, ...] 列表，没有缓存——summary 归空、游标归 1
    if isinstance(raw, list):
        return Session(messages=[Message(**d) for d in raw])

    messages = [Message(**d) for d in raw.get("messages", [])]
    memory = raw.get("memory", {})
    summarized_upto = memory.get("summarized_upto", 1)
    # 游标不可越界：防御性钳到 [1, len(messages)] 内
    return Session(
        messages=messages,
        summary=memory.get("summary"),
        summarized_upto=max(1, min(summarized_upto, len(messages))),
        title=raw.get("title"),   # 旧归档无此字段 → None，list 时 fallback 首句
        plan=PlanBoard.from_dict(raw.get("plan", {})),   # S5b：老文件无 plan 段 → 空板宽进
    )


def archive_session(path: Path, archive_dir: Path, now: datetime | None = None) -> Path:
    """S1 归档原子操作：把会话文件复制进归档仓库，命名 = 零填充时间戳。

    - 文件名即身份（机器）：唯一、零填充 `%Y%m%d-%H%M%S` 保证「字典序 = 时间序」，
      列历史清单 sorted() 文件名即可，不用读内容
    - 同秒归档冲突：目标已存在时追加 `-1`/`-2` 序号（S2 验收修复轮#3——
      真实快速点击曾让同秒两次归档覆写，归档数凭空变少）
    - 标题是标签（人看）：这里不生成——由调用方派生或 LLM 提炼，
      身份和标签分离，不往文件名里塞易漂移的文本
    - 用 shutil.copy2 复制而非 rename：文件消失前先复制成功，
      「先存后清」的语义就落在这里——调用方确认本函数成功后才重置内存
    - now 参数留给测试注入固定时刻
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    base = f"{now:%Y%m%d-%H%M%S}"
    target = archive_dir / f"{base}.json"
    counter = 1
    while target.exists():
        # 同秒归档不覆写（S1 记录的边界兑现：触发信号=高频 /new 场景，
        # Web 快速点击切换已实测撞上）——追加序号而非覆盖，旧会话文件保命
        target = archive_dir / f"{base}-{counter}.json"
        counter += 1
    shutil.copy2(path, target)
    return target


TITLE_MAX_LEN = 20   # 标题截断上限：它是清单里的一行标签，不是摘要


def derive_title(session: Session) -> str:
    """身份 vs 标签（S1 裁定二）：身份在文件名，标签 = 用户首句【派生】。

    派生而非另存的用意：标签永远是「算出来的」，和内容天然一致——
    不存在「改完内容忘了同步标题」的漂移；想换截断长度随时重派生，
    身份（时间戳文件名）纹丝不动。
    """
    for m in session.messages:
        if m.role == "user":
            first = m.content.strip().replace("\n", " ")
            if len(first) <= TITLE_MAX_LEN:
                return first
            return first[:TITLE_MAX_LEN] + "…"
    return "（空会话）"


def list_archived_sessions(archive_dir: Path) -> list[tuple[Path, str]]:
    """历史会话清单：按文件名升序（零填充 → 字典序 = 时间序），每项 (归档路径, 标题)。

    这是 S2 会话列表的直接原料。标题优先取归档文件里的 LLM 提炼标签
    （session.title），没有（旧归档未提炼）则 fallback 到首句派生。
    v1 每个文件全量 load 再取标题——教学规模足够；升级触发信号（文件数 >20
    或单文件 >10MB）届时换只读头的索引。
    """
    if not archive_dir.is_dir():
        return []
    items = []
    for path in sorted(archive_dir.glob("*.json")):
        try:
            session = load_session(path)
        except (json.JSONDecodeError, TypeError):
            # 损坏文件跳过不炸清单（写盘中途被杀会留 partial write——
            # 清单读取是展示路径，不该被一个坏文件整垮）
            continue
        items.append((path, session.title or derive_title(session)))
    return items


def restore_session(archive_path: Path, active_path: Path) -> Session:
    """S2a 切回继续聊：把归档会话切回 active（move 语义）。

    与 archive_session（copy2 复制）方向相反——那一步「存档但真人留在原地」，
    这一步「把过去的一段对话拉回来当下正在聊的」：
    - 读归档 → 写回 active（固定位置）→ 删归档文件
    - 铁律：任何时刻一段对话只有一个物理副本。写回 active 成功后归档即删，
      杜绝「active 与 archive 各存一份」的双真值源
    - 顺序即安全：先写回成功、再删归档——写回失败时归档还在，旧对话不丢
      （「先存后清」哲学的 restore 版）
    - 前置：归档必须存在——restore 的语义是「必须已经有了」，不同于 load
      的「可能还没有」（后者静默返回空会话）。传不存在的路径是调用方 bug，
      大声崩而非把空会话静默写进 active 覆盖真人对话
    - 返回值是新会话；内存「换血」（装进现有 messages 列表、绝不 rebind）
      不在这里做——那是装配层与 run_chat 之间的列表身份契约
    """
    if not archive_path.is_file():
        raise FileNotFoundError(f"归档会话不存在：{archive_path}")
    session = load_session(archive_path)
    save_session(session, active_path)
    archive_path.unlink()
    return session
