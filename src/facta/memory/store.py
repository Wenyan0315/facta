"""M6 会话状态：既存底片（原文），也存压缩缓存。

M6.2 之前只把 messages（底片）落盘，summary / summarized_upto（压缩缓存）
是 run_chat 的局部变量，重启即清零——滚动摘要退化成「每次启动一次性全量大压缩」。
本模块把「底片 + 缓存」打包成一个 Session 对象整体持久化，才是完整的会话状态。

存储格式（version 预留演进）：
    {
      "version": 1,
      "title": "...", "collapsed": false,
      "messages": [ {"role","content","tool_calls"?,"tool_call_id"?}, ... ],
      "memory": { "summary": "...", "summarized_upto": 37, "consolidated_upto": 12 },
      "plan": {...}
    }
旧格式（M6.2 及以前）是纯 [Message, ...] 列表，load 时识别并自动迁移。

S8a 会话模型：身份 = 文件名，状态 = 内存概念
------------------------------------------------
S8a 之前是「active 固定位 + archive 目录」双布局：正在聊的那段住在
`data/memory/session.json`（位置即身份），聊完的被 copy 进 `archive/`。
这带来两个结构性限制：
  1. 「当前会话」全进程只有一个 —— 多标签页/多会话并发无从谈起
  2. 归档是 move/copy 操作，切会话要先存档再清内存，锁域覆盖整个会话切换

S8a 改成：所有会话同住 `data/memory/sessions/`，身份就是文件名（`%Y%m%d-%H%M%S`，
同秒冲突追加 `-1`/`-2`）——于是 archive_session / restore_session /
list_archived_sessions 三个「搬文件」的函数整体下线，SessionStore 只剩
create/load/save/delete/list_metas 五个不搬文件的操作。
「任何时刻一段对话只有一个物理副本」这条铁律没变，只是从「靠 move 顺序保证」
变成「靠位置唯一保证」——根本没有第二处可放。

live/running 也不是磁盘位置，而且服务端【不驻留】会话对象：worker 进场
load、出场 save，中间独占这一段对话（独占由 RunStore 的会话内准入保证，
不是靠锁）。没有驻留表 → 没有驱逐策略、没有跨请求的 per-session 锁，
「当前会话」这个主语在服务端彻底消失（前端自己记着正在看哪个 id）。
agent 同理每轮现造（assemble.build_agent 工厂），顺带让 learned/ 与用户
记忆的快照每轮都是新的。
"""

import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from facta.core.types import Message
from facta.memory.plan import PlanBoard

# 跨进程文件锁（ADR 071）：macOS/Linux 可用 fcntl.flock，Windows 没这接口；
# 缺平台降级到线程锁——进程内仍安全，但跨进程 create 同 sid 撞车不挡。
# 评审已知 macOS 是沙箱目标平台，跨进程需求（CLI + Web 并发启动）真实存在，
# 因此 flock 是程序性而非挂信号。
try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # Windows
    _HAS_FCNTL = False

SESSION_VERSION = 1

# 会话 id 白名单：id 就是文件名，而文件名来自 HTTP 路径参数——这是信任边界，
# 不校验就等于把 `../../etc/passwd` 交给 Path 拼接。格式 = 归档时间戳（+同秒序号）。
_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


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
    title: str | None = None   # S2 验收修复轮：清单展示标签（LLM 提炼，None=尚未提炼）
    plan: PlanBoard = field(default_factory=PlanBoard)   # S5b：plan-then-act 状态（空板=无活跃计划）
    collapsed: bool = False    # S8a：前端清单里的「收起」标记（纯展示偏好，跟内容同文件落盘）
    consolidated_upto: int = 0   # S8a：增量固化游标——已萃取进 learned/ 的消息数


def save_session(session: Session, path: Path) -> None:
    """把会话状态（底片 + 缓存 + 标题 + 游标）存成 JSON。

    原子写（P0-3）：先写同目录临时文件再 os.replace 换名。直接 open(path,"w")
    截断后，进程在写盘中途被杀会留下半截 JSON——会话本体直接毁掉
    （list_metas 只是「跳过坏文件」，救不回内容）。os.replace 在同文件系统内
    是原子的：崩溃只可能看到旧版或新版，没有中间态。
    """
    path.parent.mkdir(parents=True, exist_ok=True)   # 父目录不存在就建（第一次跑 data/memory/ 还不存在）
    data = {
        "version": SESSION_VERSION,
        "title": session.title,
        "collapsed": session.collapsed,
        "messages": [asdict(m) for m in session.messages],
        "memory": {
            "summary": session.summary,
            "summarized_upto": session.summarized_upto,
            "consolidated_upto": session.consolidated_upto,
        },
        # S5b：计划棋盘整体落盘（事件史全量在——「为什么跳步」重启后仍可查；
        # 传输队列 _pending 不落盘：它是本轮 Run 的传输状态，不属于会话）
        "plan": session.plan.to_dict(),
    }
    # 临时文件与目标同目录（同文件系统）才能用 os.replace 原子换名。
    # 后缀 .tmp 不匹配 list_metas 的 `*.json` glob，半截临时文件不会混进会话清单。
    # tmp 名带 uuid（P1-6 评审修复）：同 sid 的并发 save 会互踩同一个
    # `<sid>.json.tmp`（后写的先 replace，先写的再 replace 旧内容——内容倒退；
    # 更糟的是一个线程正在写时另一个已把它 replace 走，Windows 上还会句柄冲突）。
    # 独占 tmp 后 os.replace 仍可能交错，但那是准入层该挡的（见 SessionStore 锁注记）；
    # 本层至少保证「写半截的文件永远不会以正式名字出现」。
    # 不做 fsync：038 的威胁模型是「进程被杀」，内核 page cache 仍在；
    # 掉电持久化是另一档需求，等真踩到再加。
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)   # ensure_ascii=False：中文原样存，不变 \u 天书
    os.replace(tmp, path)


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
    # consolidated_upto 的缺省取 len(messages) 而非 0：老文件是在「退出时全量固化」
    # 的旧语义下写的，历史已萃取过；给 0 会让第一次增量固化把整段历史重烧一遍 LLM
    return Session(
        messages=messages,
        summary=memory.get("summary"),
        summarized_upto=max(1, min(summarized_upto, len(messages))),
        title=raw.get("title"),   # 旧归档无此字段 → None，list 时 fallback 首句
        plan=PlanBoard.from_dict(raw.get("plan", {})),   # S5b：老文件无 plan 段 → 空板宽进
        collapsed=bool(raw.get("collapsed", False)),   # S8a：老文件无此字段 → 不收起
        consolidated_upto=max(0, min(memory.get("consolidated_upto", len(messages)), len(messages))),
    )


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


@dataclass
class SessionMeta:
    """会话清单的一行：只带展示所需的最小信息，不带消息本体。

    v1 为了拿标题仍要 load 整个文件（教学规模足够）；升级触发信号
    （文件数 >20 或单文件 >10MB）届时换只读文件头的索引。
    """

    id: str
    title: str
    mtime: int            # 最后修改时刻（纳秒级 epoch）——「最近聊过的排前面」
    collapsed: bool


class SessionStore:
    """S8a 会话仓库：一个目录 + 文件名即身份，不搬文件。

    五个操作 create/load/save/delete/list_metas 全是单文件级别的，
    没有跨文件的原子性要求——因为「一段对话只有一个物理副本」由位置唯一保证，
    不再由「先复制成功再删源」的顺序保证。
    并发：同一段对话的并发写由上层的【准入】挡住——Web 是 RunStore 的会话内
    单锁 + 写操作对 in-flight 会话返 409（server/run_store.py、server/app.py），
    CLI 恒一个会话且单线程；用策略代替锁是因为临界区是一整轮对话（几十秒），
    排队没有意义。**但 create 是例外（P1-6 评审修复）**：创建时还没有会话可让
    RunStore 锁，Web 线程池里两个同秒请求会同时通过 _alloc_id 的「不存在」
    检查、拿到同一个 id、互踩 tmp（评审实测只落一个文件 + FileNotFoundError）。
    所以 create 全程持一把进程内 threading.Lock（锁域毫秒级，无排队压力）。
    **ADR 071**：跨进程（CLI 与 Web 同时跑，或多 worker）会绕开线程锁——补一把
    fcntl.flock 文件锁。Windows 没 fcntl → 跨进程仍撞车，挂信号记录（评审边界
    一直说「沙箱仅 macOS」，跨平台本来是已知未铺）。lockfile 是临时文件，与
    会话同目录（同一文件系统 flock 才有意义）。
    """

    def __init__(self, dir: Path) -> None:
        self._dir = dir
        self._create_lock = threading.Lock()
        self._create_lockfile = self._dir / ".create.lock"
        # lockfile 不存在则创建——首次启动 _dir 可能尚未存在
        self._create_lockfile.parent.mkdir(parents=True, exist_ok=True)
        self._create_lockfile.touch(exist_ok=True)
        # 永久持有 fd：POSIX 上 flock 在 fd 关闭时自动释放，必须跨临界区
        # 持同一个 fd 才能让 LOCK_EX 真正生效。失败兜底= None
        try:
            self._lock_fd: int | None = os.open(
                str(self._create_lockfile), os.O_RDWR | os.O_CREAT, 0o644
            )
        except OSError:
            self._lock_fd = None

    @property
    def dir(self) -> Path:
        return self._dir

    def path(self, sid: str) -> Path:
        """会话 id → 文件路径。id 不合白名单直接崩，不做静默清洗。"""
        if not _ID_RE.match(sid):
            raise ValueError(f"非法会话 id：{sid!r}")
        return self._dir / f"{sid}.json"

    def list_metas(self) -> list[SessionMeta]:
        """全部会话，按最后修改时刻降序（最近聊过的在最前）。

        空会话也在清单里（标题「（空会话）」）——跟主流聊天产品一致：
        用户刚点的「新建」不该凭空消失。老布局靠「归档前空会话守卫」把空壳
        挡在清单外，那是 move 语义的补丁，不是产品需求。
        """
        if not self._dir.is_dir():
            return []
        metas = []
        for path in self._dir.glob("*.json"):
            sid = path.stem
            if not _ID_RE.match(sid):
                continue   # 非本模块产出的文件（人手塞进来的东西）不进清单
            try:
                # 纳秒级：秒级 mtime 在粗时间戳文件系统（部分 overlay/网络盘）上
                # 同刻两写会拿到相同值，排序退化成 glob 顺序——
                # test_latest_is_most_recently_touched 曾因此 flake（issue #15 收尾时发现）
                mtime = path.stat().st_mtime_ns
                session = load_session(path)
            except (json.JSONDecodeError, TypeError, OSError):
                # 损坏文件跳过不炸清单（写盘中途被杀会留 partial write——
                # 清单读取是展示路径，不该被一个坏文件整垮）
                continue
            metas.append(SessionMeta(
                id=sid,
                title=session.title or derive_title(session),
                mtime=mtime,
                collapsed=session.collapsed,
            ))
        # 决胜键 id：mtime 仍可能同刻（文件系统粒度再粗也有极限），id 时间戳前缀
        # 让同刻排序至少是确定性的，不再随 glob 顺序漂移
        metas.sort(key=lambda m: (m.mtime, m.id), reverse=True)
        return metas

    def load(self, sid: str) -> Session:
        """读回一个会话。缺席大声崩 —— 「必须已经有了」是调用方的前提，
        静默返回空会话会把新壳子写回去覆盖真人对话。"""
        path = self.path(sid)
        if not path.is_file():
            raise FileNotFoundError(f"会话不存在：{sid}")
        return load_session(path)

    def save(self, sid: str, session: Session) -> None:
        save_session(session, self.path(sid))

    def create(self, session: Session, now: datetime | None = None) -> str:
        """新建会话并立即落盘，返回 id。

        立即落盘（而非等第一次说话）是 S8a 的选择：id 一旦返回给前端就是身份，
        身份必须已经存在——否则「新建后刷新页面」会看到一个不存在的会话。
        代价是清单里可能留空壳，而空壳本来就该显示（见 list_metas）。
        P1-6：分配 + 落盘整体持锁（同秒并发 create 曾拿到同一个 id——
        「检查不存在」与写盘之间存在窗口，见类 docstring 的锁注记）。
        ADR 071：再加一把 fcntl.flock 跨进程锁（macOS/Linux）。Windows 缺 fcntl
        降级回线程锁——跨进程仍可能撞车（评审已知边界）。
        now 参数留给测试注入固定时刻。
        """
        with self._create_lock:
            if _HAS_FCNTL and self._lock_fd is not None:
                # flock 阻塞直到拿到；进程被杀 OS 自动释放
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                sid = self._alloc_id(now or datetime.now())
                save_session(session, self._dir / f"{sid}.json")
                return sid
            finally:
                if _HAS_FCNTL and self._lock_fd is not None:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def delete(self, sid: str) -> bool:
        path = self.path(sid)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def latest(self) -> str | None:
        """最近聊过的会话 id；一个都没有 → None（CLI 启动时据此决定新建）。"""
        metas = self.list_metas()
        return metas[0].id if metas else None

    def _alloc_id(self, now: datetime) -> str:
        """分配一个新 id：零填充时间戳（字典序 = 时间序），同秒冲突追加序号。

        同秒不覆写是血泪教训（S2 验收修复轮#3）：Web 上快速点击曾让同秒两次
        归档互相覆写，会话数凭空变少。S8a 归档操作没了，但「快速连点新建」
        是同一个触发信号，序号逻辑原样保留。
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        base = f"{now:%Y%m%d-%H%M%S}"
        sid = base
        counter = 1
        while (self._dir / f"{sid}.json").exists():
            sid = f"{base}-{counter}"
            counter += 1
        return sid

    def migrate_legacy_active(self, legacy_path: Path) -> str | None:
        """一次性搬迁老布局的 active 固定位（`data/memory/session.json`）进 sessions/。

        只搬这一个文件：老布局的另一半——归档仓库 `paths.SESSIONS_DIR`——
        就是新布局的会话目录本身，且归档文件名本就是 `%Y%m%d-%H%M%S.json`
        （合法 id）。也就是说老归档在 S8a 眼里【已经】是正常会话，
        原地不动即完成迁移，历史清单一条不丢。
        内容格式两代完全一致（同一个 save_session 写的），所以搬迁 = move：
        不读不解析不重写，坏文件也照搬（清单读取时才跳过）。
        幂等：源文件不存在（已搬过 / 全新安装）返回 None。
        """
        if not legacy_path.is_file():
            return None
        # active 的文件名是 "session"（不是合法 id）→ 按 mtime 分配，
        # 保留原创建时刻，迁移后它在清单里的位置跟迁移前一致
        sid = self._alloc_id(datetime.fromtimestamp(legacy_path.stat().st_mtime))
        self._dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_path), str(self._dir / f"{sid}.json"))
        return sid
