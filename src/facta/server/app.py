"""FastAPI 壳：把内核 run_turn 与装配 ctx 接上 HTTP（S2b）。

分层（课件「Loop 生产事实，Gateway 传输事实」）：
- 路由      收发包（create/subscribe/cancel）
- 后台线程  把 run_turn 的 on_text/on_event 接进 Run Store，跑一轮
- Run Store 给事实编号、留存、可重放（承载状态机 + 取消标志）

内核 run_turn 一行不改——它产语义事实，本层负责事实的传输与生命周期。

日志：Web 入口在此配置 logging——内核库的 logger 输出进 uvicorn 日志流。
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)   # ADR 076：finish 后的异步收官失败无人在听事件流，落日志

from facta.knowledge.extract import sync_graph
from facta.knowledge.graph import GRAPH_LOCK
from facta.knowledge.sync import file_hash, sync_notes
from facta.memory.consolidate import CATEGORIES
from facta.memory.learned import (
    delete_line_by_id,
    ensure_ids,
    read_learned,
    update_line_by_id,
    visible_text,
)
from facta.memory.store import Session
from facta.orchestrator.assemble import AppContext, settle_session
from facta.orchestrator.checkpoint import CheckpointWriter, heal, ledger_path, read_ledger
from facta.orchestrator.loop import RunResult, run_turn
from facta.paths import GRAPH_PATH, LEARNED_DIR, NOTES_DIR, user_memory_path
from facta.server.run_store import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    Run,
    RunStore,
)
from facta.server.sse import encode_heartbeat, encode_sse
from facta.tools.notes import record_provenance, resolve_note_path

# run_turn 的 on_event 类型 → Run 事件类型（统一用点分层命名，前端按 type 路由）
_EVENT_MAP = {
    "tool_started": "tool.started",
    "tool_result": "tool.result",
    "max_rounds": "max_rounds",
    "stuck": "stuck",
    "error": "error",
}

_HEARTBEAT_SECONDS = 15.0   # 无事件时的保活间隔（模型推理可能几十秒静默）

_TIME_RE = re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})")


def _time_label(sid: str) -> str:
    """会话 id（时间戳）→ 清单里的一行时间标签「09-15 23:15」。

    身份即时间戳，所以展示时间不需要另存字段——从 id 解析即可。
    """
    m = _TIME_RE.match(sid)
    return f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}" if m else ""


# 记忆面板可编辑的类别白名单（041）：项目级三桶 + 用户级伪 category "user"。
# 白名单同时是路径穿越防线——category 直接参与拼路径，只认枚举值。
PANEL_CATEGORIES = CATEGORIES + ("user",)


def _learned_path(category: str) -> Path:
    """category → 记忆文件（调用方保证 category 已在 PANEL_CATEGORIES 内）。

    user 走 paths.user_memory_path()：它是函数不是常量（FACTA_USER_MEMORY
    覆写点），面板必须与 agent 注入侧（assemble.py）同源——存常量的事故形态
    很具体：测试覆写了环境变量，面板却仍盯着真 home 里的 user.md，等于拿
    测试操作生产隐私文件。user.md 与项目桶是同款落盘物（同行格式、同一把
    LEARNED_LOCK），差别只在住在仓库外 → git 兜不住，删了就是删了。
    """
    if category == "user":
        return user_memory_path()
    return LEARNED_DIR / f"{category}.md"


# 知识语料面板（042）单篇读取上限：超限给明确错误，不把浏览器拖死。
# notes 是 agent 写的 markdown，正常几百字到几万字；1MB 已经是事故级。
NOTE_MAX_BYTES = 1_000_000


def _note_path(name: str) -> Path:
    """笔记名（URL 路径段）→ 绝对路径；围栏不过即 400。

    与 learned 的白名单枚举不同，这里的 name 是自由字符串——直接拿它拼路径
    等于开了任意文件写（`../../.env`、`../../../.ssh/id_rsa`）。围栏本体住在
    tools/notes.resolve_note_path，与 agent 的 read_notes/write_note 同一份
    （042 裁定 2：复用而非各写一遍，防的就是 029 那种「防御不对称」漂移）。
    """
    try:
        return resolve_note_path(NOTES_DIR, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


class CreateRunRequest(BaseModel):
    """创建 Run 的请求体。系统边界处用 JSON Schema 校验（与工具同一纪律）。

    session_id 可省：省略即「新开一段对话」——前端首次发送不必先建会话，
    响应里带回真实 id，客户端从此认它。
    """

    text: str
    session_id: str | None = None


class CreateTodoRequest(BaseModel):
    """添加待办的请求体。"""

    text: str


class UpdateTodoRequest(BaseModel):
    """修改待办文本的请求体。"""

    text: str


class ConfirmRequest(BaseModel):
    """L2 确认裁决的请求体（S4b）：approve=true 批准 / false 拒绝。"""

    approve: bool


class RenameSessionRequest(BaseModel):
    """会话重命名的请求体（2026-09-17 体验轮）：只改 title 标签。"""

    text: str


class LearnedUpdateRequest(BaseModel):
    """记忆条目编辑的请求体（089 起 by-id + 乐观锁）：只改正文，日期归程序管。

    base_content 是乐观锁：客户端把 GET 拿到的 content（可见文本）原样带回，
    服务端与磁盘现值比对，不一致 → 409。挡的事故＝旧页面在条目被别人改过后
    仍提交，把别人的改动盲覆盖掉（089 与 042 notes 的 base_hash 同款先例）。
    """

    content: str
    base_content: str


class NoteSaveRequest(BaseModel):
    """笔记保存的请求体（042）。

    base_hash 是乐观锁：客户端把 GET 拿到的 hash 原样带回，服务端与磁盘现值
    比对，不一致 → 409。要挡的事故很具体——用户在 IDE 里改着同一批 md，面板
    一保存就是整文件盲覆盖（与 041 的「行号错位」不是一个量级，那次不值得加
    锁，这次值得）。不引入版本号字段、不落盘元数据：hash 就是版本。
    """

    content: str
    base_hash: str


def _run_worker(ctx: AppContext, run: Run, user_text: str | None) -> None:
    """后台线程：跑一轮 run_turn，把事实灌进 Run Store，收尾时推终态。

    S8a 起 worker 自带会话生命周期：进场 load、出场 save，中间独占。
    「独占」不是靠锁而是靠准入——RunStore.create_if_idle 保证同一会话
    同时只有一个 in-flight Run，所以这里的 load/改/save 不会与别人交错。
    agent 也是每轮现造（ctx.build_agent）：人设由工厂保证，且 learned/ 与
    用户记忆的快照因此每轮都是新的——常驻 agent 会让本轮刚固化的记忆
    要等重启才进 prompt。

    P0-3（038）崩溃恢复：user_text=None 即「续跑」——不追加新提问，先把
    上次被杀留下的悬挂工具轮次 heal 成合法底片，再让 run_turn 从现场往前走。
    执行期间 CheckpointWriter 挂在 on_event 缝上：工具边界落盘底片、
    账本记 intent/result（细节见 orchestrator/checkpoint.py）。

    收尾（settle_session：补标题 → 增量固化 → 落盘）在 finally 里拆两截
    （ADR 076 reflect 全面异步化）：
      ① **保底 save + run.settling 事件**留在 run.finish 之前——Web 壳是
      常驻进程，没有 CLI 的退出保存钩子，不保底就在服务被杀时丢掉这一整
      轮对话；「读到 run.completed 时盘上必有这轮对话」的验收不变。
      ② 标题/固化的 LLM 重活挪到 run.finish **之后**（同线程继续跑）——
      终态推送与准入释放不再被固化拖住，用户收完回复即可开下一轮。
      代价是与「同会话的下一轮」失去准入串行：settle 的终态写因此改
      store.update 窄写（见 assemble.settle_session 头注记），messages
      的所有权仍在轮次 worker 手里，丢消息在结构上不可能。
    """
    run.status = STATUS_RUNNING
    run.emit("run.started", {})
    status = STATUS_FAILED
    session: Session | None = None
    writer: CheckpointWriter | None = None
    try:
        session = ctx.store.load(run.session_id)
        agent = ctx.build_agent(session)
        ledger = ledger_path(run.session_id)
        # 绑定 save：writer 因此不懂 SessionStore，测试可注入
        writer = CheckpointWriter(ledger, lambda: ctx.store.save(run.session_id, session))
        # heal 必须在 begin 之前——begin 会截断账本，先截就丢了「上次跑没跑过」的证据
        healed = heal(session, read_ledger(ledger), agent.registry)
        if healed:
            run.emit("run.healed", {"count": healed})
        writer.begin(run.run_id)

        def _forward(type_: str, data: dict) -> None:
            writer.on_event(type_, data)        # 先落盘再外发：客户端看见的事实必然已在盘上
            run.emit(_EVENT_MAP.get(type_, type_), data)

        result, reply = run_turn(
            session,
            user_text,
            agent=agent,
            llm=ctx.llm,
            summarizer=ctx.internal_llm,
            on_text=lambda text: run.emit("text.delta", {"delta": text}),
            on_event=_forward,
            should_cancel=lambda: run.cancel_requested,
            on_confirm=run.request_confirm,
        )
        # 终态判定：RunResult 枚举替代 None 二义性（S4 评审 #3）
        if result is RunResult.COMPLETED and reply is not None:
            run.preview = (reply.content or "")[:300]   # 任务视图的交付摘要
            run.reply_text = reply.content or ""   # ADR 081：headless 同步端点的完整结果
            status = STATUS_COMPLETED
        elif result is RunResult.CANCELLED:
            status = STATUS_CANCELLED
    except Exception as exc:   # 防御性兜底：run_turn 已捕获 LLMUnavailableError，这里是意外
        run.emit("error", {"message": str(exc)})
    finally:
        try:
            if writer is not None:
                writer.end(run.run_id, status)   # 人工排查时能看出这轮是正常结束还是中断
            if session is not None:
                run.emit("run.settling", {})   # 收尾可能几秒（标题/固化都要调 LLM），别让用户以为是卡死
                ctx.store.save(run.session_id, session)   # ① 保底（毫秒级）：finish 前落盘
        except Exception as exc:   # 保底失败不吞终态：告知用户，流照常收口
            run.emit("error", {"message": f"会话保底落盘失败：{exc}"})
        run.finish(status)   # ② 终态事件 + 释放准入——从这里起同会话可开下一轮
        if session is not None:
            try:
                settle_session(session, run.session_id, ctx.store, ctx.internal_llm)
            except Exception:   # 对话本体已保底落盘，游标未推进下轮重烧（P2-7 兜底）
                logger.warning("异步收官失败：游标未推进，下轮重烧", exc_info=True)
        run._settle_done.set()   # 测试同步点（ADR 076），生产只 set 不 wait


def create_app(ctx: AppContext, store: RunStore | None = None) -> FastAPI:
    # 并发上限（S8a）：默认 3——「长任务在跑，我另开一段对话问点别的」是真实
    # 需求，而不是要上多进程（architecture.md 待讨论区「多进程演进」的关键修正）。
    store = store or RunStore(max_in_flight=int(os.environ.get("FACTA_MAX_CONCURRENT_RUNS", "3")))
    app = FastAPI(title="Facta")
    static_dir = Path(__file__).parent / "static"

    def _require_session(sid: str) -> None:
        """会话 id 是 HTTP 路径/请求体来的外部输入：格式与存在性都要过一遍。
        格式非法直接 400（store.path 会抛 ValueError，那是信任边界的兜底，
        不该以 500 的形式漏给客户端）。
        """
        try:
            exists = ctx.store.path(sid).is_file()
        except ValueError:
            raise HTTPException(400, "非法会话 id") from None
        if not exists:
            raise HTTPException(404, "会话不存在")

    def _require_writable(sid: str) -> None:
        """写会话前的准入检查：该会话有 in-flight Run 时拒绝。

        不是加锁而是定策略——worker 在整轮里独占这段对话（load→改→save），
        此时任何外部写都会在 worker 落盘时被覆盖（lost update）。与其用锁把
        写排队到几十秒后，不如直接告诉用户「这段对话正在被写」。
        """
        if store.active_run(sid) is not None:
            raise HTTPException(409, "该会话有任务在运行，请等它结束")

    @app.post("/api/runs", status_code=202)
    def create_run(body: CreateRunRequest):
        sid = body.session_id
        if sid is None:
            sid = ctx.store.create(Session())   # 省略 session_id = 新开一段对话
        else:
            _require_session(sid)
        run = store.create_if_idle(sid, title=body.text[:60])
        if isinstance(run, str):
            raise HTTPException(409, run)
        # daemon 线程：服务退出时不留阻塞；请求线程立即 202 返回
        threading.Thread(target=_run_worker, args=(ctx, run, body.text), daemon=True).start()
        return {"run_id": run.run_id, "session_id": sid}

    @app.post("/api/runs/sync")
    def create_run_sync(body: CreateRunRequest, timeout: float | None = Query(default=None, gt=0)):
        """Headless 同步提交（ADR 081；R05/092 起支持等待预算）：一条往返拿结果。

        与 `POST /api/runs`（202 异步 + SSE 订阅）正交——这里是「提交 →
        阻塞到终态 → 返回 JSON」。复用同一 `_run_worker` 与 `create_if_idle`，
        唯一区别是请求线程 `wait` 在 `_settle_done`（076 的收官同步点）上。

        `?timeout=<秒>` 是调用方自报的等待预算（默认 None = 无限等，081
        原契约）。预算耗尽返回 200 + `timed_out: true` + 当时真实 `status`
        （`running`/`waiting_approval`——后者即「在等人工确认」）——
        **超时不取消**：后台 Run 继续跑，调用方拿 `run_id` 自行决定去路
        （订阅 events / cancel / confirm）。

        供外部自动化（Kimi Work 定时任务、CI）非交互驱动。阻塞不堵事件循环——
        FastAPI 同步端点跑默认线程池。
        """
        sid = body.session_id
        if sid is None:
            sid = ctx.store.create(Session())   # 省略 session_id = 新开一段对话
        else:
            _require_session(sid)
        run = store.create_if_idle(sid, title=body.text[:60])
        if isinstance(run, str):
            raise HTTPException(409, run)
        threading.Thread(target=_run_worker, args=(ctx, run, body.text), daemon=True).start()
        settled = run._settle_done.wait(timeout=timeout)   # 阻塞到终态 + 收官（076 同步点）
        return {
            "run_id": run.run_id,
            "session_id": sid,
            "status": run.status,
            "text": run.reply_text,   # 完整回复；失败/取消/未完成为空
            "timed_out": not settled,   # R05/092：等待预算耗尽 ≠ 任务取消
        }

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, request: Request):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")

        def gen():
            # 断线重连：EventSource 自动带 Last-Event-ID，按 seq 重放缺的事件
            raw_last = request.headers.get("last-event-id", "0") or "0"
            try:
                last = int(raw_last)
            except ValueError:
                last = 0
            # 先订阅再快照（顺序是竞态修复）：旧序（先 list 再 subscribe）在
            # 两步间隙里有终态事件 emit 时——重放段没它、实时段只收到 None
            # 哨兵，run.completed 丢失（CI 偶发踩中，本地几百次不遇的窗口）。
            # 新序：subscribe 后到达的事件必进 q；快照可能含重复（也进了 q），
            # 靠 ev.seq <= last 去重——窗口消失。
            q = run.subscribe()
            try:
                for ev in list(run.events):
                    if ev.seq > last:
                        yield encode_sse(run.run_id, ev)
                        last = ev.seq   # 更新哨兵，防实时段重复发

                # 实时读：阻塞 + 心跳保活；None 哨兵 = Run 结束。
                # 广播模型（评审修复轮）：每连接独立队列；断开必须退订
                # （finally 兜底——客户端断连时 generator 被 close，此处清理）
                while True:
                    try:
                        ev = q.get(timeout=_HEARTBEAT_SECONDS)
                    except queue.Empty:
                        yield encode_heartbeat()
                        continue
                    if ev is None:
                        break
                    if ev.seq <= last:   # 重放段已发过，去重
                        continue
                    yield encode_sse(run.run_id, ev)
            finally:
                run.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.request_cancel():
            return {"status": "cancelling"}
        raise HTTPException(409, "任务已结束，无法取消")

    # L2 确认裁决（S4b）：前端弹窗的批准/拒绝落到这里，唤醒挂起的 worker
    @app.post("/api/runs/{run_id}/confirm")
    def confirm(run_id: str, body: ConfirmRequest):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.resolve_confirm(body.approve):
            return {"status": "approved" if body.approve else "rejected"}
        raise HTTPException(409, "当前没有待确认的操作")

    # 会话清单（S8a）：身份=文件名，没有「active」这个特例——正在聊的那段
    # 也只是清单里的一行，靠 running 字段标出来。老布局要把 active 置顶拼进
    # 清单（还得防「切回的会话从列表消失」），那是 move 语义的产物。
    @app.get("/api/sessions")
    def list_sessions():
        return [
            {
                "id": m.id,
                "title": m.title,
                "time": _time_label(m.id),
                "collapsed": m.collapsed,
                "running": store.active_run(m.id) is not None,
            }
            for m in ctx.store.list_metas()   # 已按最后修改时刻降序
        ]

    @app.post("/api/sessions", status_code=201)
    def new_session():
        """显式新建一段空会话（前端「新对话」按钮）。

        空会话也进清单（跟主流聊天产品一致：刚点的新建不该凭空消失）。
        与 POST /api/runs 省略 session_id 的区别：这条不发消息，只占位。
        """
        return {"id": ctx.store.create(Session())}

    @app.post("/api/sessions/{sid}/fork", status_code=201)
    def fork_session(sid: str):
        """fork 会话（ADR 085）：从源会话 copy 出完整副本，换个思路重来不丢上下文。

        只读源、建新会话——fork 不写源会话，故不走 _require_writable；源正被
        worker 独占时 fork 也安全（读到的是当刻落盘的快照）。
        """
        _require_session(sid)
        return {"id": ctx.store.fork(sid)}

    @app.put("/api/sessions/{sid}")
    def rename_session(sid: str, body: RenameSessionRequest):
        """重命名（2026-09-17 体验轮）：改的是 title 标签，身份（文件名）不动。"""
        title = body.text.strip()
        if not title:
            raise HTTPException(400, "标题不能为空")
        _require_session(sid)
        _require_writable(sid)
        session = ctx.store.load(sid)
        session.title = title
        ctx.store.save(sid, session)
        return {"ok": True}

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str):
        """删除会话（2026-09-17 体验轮）。S8a 起没有「active 不能删」的特例——
        任何会话都能删，只要它当下没在跑。
        """
        _require_session(sid)
        _require_writable(sid)
        ctx.store.delete(sid)
        return {"ok": True, "deleted": sid}

    # 历史消息（S8a：按 session id 取，不再有「当前会话」这个隐含主语）
    # 只回放 user/assistant 轮——system=人设、tool=中间产物，不进对话回放；
    # 空 content 的纯点菜轮跳过（历史回放只讲故事线）
    @app.get("/api/sessions/{sid}/messages")
    def messages(sid: str):
        _require_session(sid)
        return [
            {"role": m.role, "content": m.content}
            for m in ctx.store.load(sid).messages
            if m.role in ("user", "assistant") and m.content
        ]

    # 任务列表：Run Store（新的在前），任务视图原料；给 session_id 则只列该会话的
    @app.get("/api/runs")
    def runs(session_id: str | None = None):
        return [
            {
                "run_id": r.run_id,
                "session_id": r.session_id,
                "status": r.status,
                "title": r.title,
                "preview": r.preview,
            }
            for r in store.list_runs(session_id)
        ]

    # 个人待办（014 语义：任务=个人待办）：UI 直连 store，与 agent 工具共用同一实例
    @app.get("/api/todos")
    def todos_list():
        return [{"id": t.id, "text": t.text, "done": t.done} for t in ctx.todos.list()]

    @app.post("/api/todos", status_code=201)
    def todos_add(body: CreateTodoRequest):
        todo = ctx.todos.add(body.text)
        return {"id": todo.id, "text": todo.text, "done": todo.done}

    @app.post("/api/todos/{todo_id}/complete")
    def todos_complete(todo_id: int):
        todo = ctx.todos.complete(todo_id)
        if todo is None:
            raise HTTPException(404, f"待办 #{todo_id} 不存在")
        return {"id": todo.id, "text": todo.text, "done": todo.done}

    @app.delete("/api/todos/{todo_id}")
    def todos_delete(todo_id: int):
        todo = ctx.todos.delete(todo_id)
        if todo is None:
            raise HTTPException(404, f"待办 #{todo_id} 不存在")
        return {"deleted": todo.id}

    @app.put("/api/todos/{todo_id}")
    def todos_update(todo_id: int, body: UpdateTodoRequest):
        todo = ctx.todos.update_text(todo_id, body.text)
        if todo is None:
            raise HTTPException(404, f"待办 #{todo_id} 不存在")
        return {"id": todo.id, "text": todo.text, "done": todo.done}

    # 记忆面板（021「护城河可视化」+ 041 用户级分栏）：learned 三桶 +
    # 用户级 user.md 的读/改/删——固化管线的产出不再是黑箱。089 起定位协议
    # 从行号切到稳定 id（见 memory/learned.py 模块注释），类别→路径的解析见
    # _learned_path。
    @app.get("/api/learned")
    def learned_list():
        out = []
        for category in PANEL_CATEGORIES:
            path = _learned_path(category)
            # 懒迁移：旧文件无 id 注释 → 先补上，by-id 定位才有锚（幂等）
            ensure_ids(path)
            for entry in read_learned(path):
                out.append({
                    "category": category,
                    "id": entry.id,
                    "date": entry.date,
                    # 053：content = 可见 tag + 正文（前端契约不变：改完原样
                    # PUT 回来，update_line_by_id 再把 tag 与正文拆开）。
                    # [固化:sid] 属排查用元数据，不在 VISIBLE_TAGS 里 → 面板
                    # 看不到它，要看就去磁盘上看原行。
                    "content": visible_text(entry),
                })
        return out

    @app.put("/api/learned/{category}/{line_id}")
    def learned_update(category: str, line_id: str, body: LearnedUpdateRequest):
        if category not in PANEL_CATEGORIES:
            raise HTTPException(400, "未知记忆类别")
        if not body.content.strip():
            raise HTTPException(400, "内容不能为空")
        path = _learned_path(category)
        if not path.is_file():
            raise HTTPException(404, "该类别暂无记忆")
        ensure_ids(path)
        current = next((e for e in read_learned(path) if e.id == line_id), None)
        if current is None:
            raise HTTPException(404, "条目不存在")
        if visible_text(current) != body.base_content:
            raise HTTPException(409, "条目已变化，请重新载入再改")
        update_line_by_id(path, line_id, body.content.strip())
        return {"ok": True}

    @app.delete("/api/learned/{category}/{line_id}")
    def learned_delete(category: str, line_id: str):
        if category not in PANEL_CATEGORIES:
            raise HTTPException(400, "未知记忆类别")
        path = _learned_path(category)
        if not path.is_file():
            raise HTTPException(404, "该类别暂无记忆")
        ensure_ids(path)
        # read_learned 默认过滤 inactive：已被撤回的条目再删 → 404（不重盖 tombstone）
        if not any(e.id == line_id for e in read_learned(path)):
            raise HTTPException(404, "条目不存在")
        delete_line_by_id(path, line_id)
        return {"ok": True}

    # 知识语料面板（042）：notes 的列表/读取/保存 + 向量库同步。
    # 裁定见 docs/decisions/042-notes-panel.md——只改已有（新建仍归对话里的
    # write_note：查重闸门只在工具层，Web 新建等于绕过它）；保存后不自动重抽
    # 图谱（sync_graph 走 LLM，花钱的事人点），改为如实回报「哪里陈旧了」。
    @app.get("/api/notes")
    def notes_list():
        # 目录不存在时 glob 给空列表——第一次跑还没建 data/notes/ 是正常状态
        return [
            {"name": p.name, "size": p.stat().st_size}
            for p in sorted(NOTES_DIR.glob("*.md"))
        ]

    @app.get("/api/notes/{name}")
    def notes_read(name: str):
        path = _note_path(name)
        if not path.is_file():
            raise HTTPException(404, f"知识库里没有 {name}")
        if path.stat().st_size > NOTE_MAX_BYTES:
            raise HTTPException(413, "笔记过大，面板不加载（请用 IDE 打开）")
        content = path.read_text(encoding="utf-8")
        return {"name": name, "content": content, "hash": file_hash(content)}

    @app.put("/api/notes/{name}")
    def notes_save(name: str, body: NoteSaveRequest):
        path = _note_path(name)
        if not path.is_file():
            # 404 而不是「顺手创建」：新建会绕过 write_note 的查重闸门（042 裁定 1）
            raise HTTPException(404, f"知识库里没有 {name}（面板只改已有笔记，新建请在对话里说）")
        if body.base_hash != file_hash(path.read_text(encoding="utf-8")):
            raise HTTPException(409, "磁盘上的版本已变（可能在 IDE 里改过），请重新载入再改")
        # 原子写（P0-3 手法）：sync_notes/sync_graph 按内容指纹判变化，读到半截
        # 会把半截当新事实抽进图谱。.tmp 后缀不匹配 *.md glob，不进任何清单。
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body.content, encoding="utf-8")
        os.replace(tmp, path)
        # 053：面板保存＝人工改过。这是 sidecar 里 "human" 唯一的产生点，而
        # sidecar 本身在 052 的 MEMORY_WRITE_FENCE 内 → 模型伪造不了这个标记，
        # search_notes 才敢把它当「用户背书过」的信号用。
        record_provenance(NOTES_DIR, name, "human")

        new_hash = file_hash(body.content)
        return {
            "ok": True,
            "hash": new_hash,
            # 陈旧标记（042 裁定 4）：原文一改，向量库与图谱都还是旧投影。
            # 按指纹现算不猜——图谱只在该篇曾进过图时才算陈旧（从没抽过的笔记，
            # 图里没有它的旧信息可陈旧）；kb 未启用（无 embedder）时无从陈旧。
            "stale": {
                "kb": ctx.kb is not None and new_hash not in {
                    meta["hash"] for meta in ctx.kb.store.get_all().values()
                },
                "graph": ctx.graph.note_hashes.get(name) not in (None, new_hash),
            },
        }

    @app.post("/api/notes/sync")
    def notes_sync():
        """把向量库与磁盘对齐（042 裁定 4 的第一个动作）。

        只花 embeddings 的钱，所以是人点的按钮，不进保存路径（保存里偷偷同步
        会把「花时间」和「同步失败」两种语义混进「存盘成功」）。不加服务端锁：
        运行时只有这一个同步入口（assemble 那次在启动时跑完了），连点由前端
        disable 挡。
        """
        if ctx.kb is None:
            raise HTTPException(503, "向量库未启用（未配置 embedder）")
        try:
            report = sync_notes(ctx.kb, NOTES_DIR)
        except (FileNotFoundError, RuntimeError) as exc:   # 目录异常 / 删除安全阀
            raise HTTPException(503, str(exc)) from None
        return {
            "added": report.added,
            "removed": report.removed,
            "unchanged": report.unchanged,
        }

    # 知识图谱面板（S7b）：图数据 + 重建图谱。图是 notes 的结构化投影
    # （知识资产），面板一次拉全量——图小，搜索定位/路径高亮/类型过滤
    # 全在前端内存算，无需额外交互端点。「重建图谱」= force 全量重抽
    # （同步端点，花 LLM 钱——前端 loading 状态等待），与对话内
    # sync_graph 工具共用 GRAPH_LOCK 串行（图级并发协议见 graph.py）。
    @app.get("/api/graph")
    def graph_data():
        d = ctx.graph.to_dict()
        return {
            "nodes": d["nodes"],
            "edges": d["edges"],
            "stats": ctx.graph.overview(),
        }

    @app.post("/api/graph/rebuild")
    def graph_rebuild():
        try:
            with GRAPH_LOCK:
                ctx.graph.note_hashes.clear()   # force：清空指纹全量重抽（不动图）
                report = sync_graph(ctx.graph, NOTES_DIR, ctx.internal_llm)
                if report.extracted or report.removed:
                    ctx.graph.save(GRAPH_PATH)
        except RuntimeError as exc:   # 删除安全阀：笔记目录疑似异常，如实上报
            raise HTTPException(503, str(exc)) from None
        return {
            "report": {
                "extracted": report.extracted,
                "unchanged": report.unchanged,
                "removed": report.removed,
                "skipped": report.skipped,
                "failed": report.failed,
            },
            "stats": ctx.graph.overview(),
        }

    # 任务视图（S2 双视图的另一半）：FW 站 v2（Preact，021 裁定）——
    # 源码 frontend/，构建产物 static/fw/（进 git）；v1 vanilla 版已删（git 史可查）
    @app.get("/tasks")
    def tasks():
        return FileResponse(static_dir / "fw" / "tasks.html")

    # 记忆面板页面（FW 新栈第二入口，025）：与 /tasks 同款伺服
    @app.get("/memory")
    def memory_page():
        return FileResponse(static_dir / "fw" / "memory.html")

    # 知识图谱面板页面（FW 新栈第三入口，S7b）：与 /tasks /memory 同款伺服
    @app.get("/graph")
    def graph_page():
        return FileResponse(static_dir / "fw" / "graph.html")

    # 知识语料面板页面（FW 新栈第四入口，042）：与 /tasks /memory /graph 同款伺服
    @app.get("/notes")
    def notes_page():
        return FileResponse(static_dir / "fw" / "notes.html")

    # 前端静态文件挂根路径；check_dir=False 让本模块先于前端文件就位（测试友好）。
    # no-cache（每次 revalidate，未变时 304 也快）：浏览器对无 Cache-Control 的
    # 静态资源做启发式缓存——新 HTML 配旧 JS 的错配曾让 send()/todo 面板静默失灵
    # （浏览器验收现场抓到：JS 与 DOM 版本错位、点发送无 POST）。开发期零构建链
    # 的正确姿势；上了内容哈希文件名再改回长缓存。
    class NoCacheStatic(StaticFiles):
        def file_response(self, *args, **kwargs):
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/", NoCacheStatic(directory=static_dir, html=True, check_dir=False), name="static")

    return app
