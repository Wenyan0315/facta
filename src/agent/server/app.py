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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

from agent.knowledge.extract import sync_graph
from agent.knowledge.graph import GRAPH_LOCK
from agent.memory.consolidate import CATEGORIES
from agent.memory.learned import delete_line, read_learned, update_line
from agent.memory.store import Session
from agent.orchestrator.assemble import AppContext, settle_session
from agent.orchestrator.loop import RunResult, run_turn
from agent.paths import GRAPH_PATH, LEARNED_DIR, NOTES_DIR
from agent.server.run_store import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    Run,
    RunStore,
)
from agent.server.sse import encode_heartbeat, encode_sse

# run_turn 的 on_event 类型 → Run 事件类型（统一用点分层命名，前端按 type 路由）
_EVENT_MAP = {
    "tool_started": "tool.started",
    "tool_result": "tool.result",
    "max_rounds": "max_rounds",
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
    """记忆条目编辑的请求体（记忆面板 v1）：只改正文，日期归程序管。"""

    content: str


def _run_worker(ctx: AppContext, run: Run, user_text: str) -> None:
    """后台线程：跑一轮 run_turn，把事实灌进 Run Store，收尾时推终态。

    S8a 起 worker 自带会话生命周期：进场 load、出场 save，中间独占。
    「独占」不是靠锁而是靠准入——RunStore.create_if_idle 保证同一会话
    同时只有一个 in-flight Run，所以这里的 load/改/save 不会与别人交错。
    agent 也是每轮现造（ctx.build_agent）：人设由工厂保证，且 learned/ 与
    用户记忆的快照因此每轮都是新的——常驻 agent 会让本轮刚固化的记忆
    要等重启才进 prompt。

    收尾（settle_session：补标题 → 增量固化 → 落盘）放在 finally，且必须先于
    run.finish：Web 壳是常驻进程，没有 CLI 的退出保存钩子，不收尾就在服务被杀
    时丢掉这一整轮对话；而 finish 一推终态就释放准入，同会话的下一轮可能立刻
    load——save 落在 finish 之后，新一轮就读到旧状态（lost update）。
    """
    run.status = STATUS_RUNNING
    run.emit("run.started", {})
    status = STATUS_FAILED
    session: Session | None = None
    try:
        session = ctx.store.load(run.session_id)
        result, reply = run_turn(
            session,
            user_text,
            agent=ctx.build_agent(session),
            llm=ctx.llm,
            summarizer=ctx.internal_llm,
            on_text=lambda text: run.emit("text.delta", {"delta": text}),
            on_event=lambda type_, data: run.emit(_EVENT_MAP.get(type_, type_), data),
            should_cancel=lambda: run.cancel_requested,
            on_confirm=run.request_confirm,
        )
        # 终态判定：RunResult 枚举替代 None 二义性（S4 评审 #3）
        if result is RunResult.COMPLETED and reply is not None:
            run.preview = (reply.content or "")[:300]   # 任务视图的交付摘要
            status = STATUS_COMPLETED
        elif result is RunResult.CANCELLED:
            status = STATUS_CANCELLED
    except Exception as exc:   # 防御性兜底：run_turn 已捕获 LLMUnavailableError，这里是意外
        run.emit("error", {"message": str(exc)})
    finally:
        if session is not None:
            try:
                run.emit("run.settling", {})   # 收尾可能几秒（标题/固化都要调 LLM），别让用户以为是卡死
                settle_session(session, run.session_id, ctx.store, ctx.internal_llm)
            except Exception as exc:   # 收尾失败不吞终态：告知用户，流照常收口
                run.emit("error", {"message": f"会话收尾失败：{exc}"})
        run.finish(status)


def create_app(ctx: AppContext, store: RunStore | None = None) -> FastAPI:
    # 并发上限（S8a）：默认 3——「长任务在跑，我另开一段对话问点别的」是真实
    # 需求，而不是要上多进程（architecture.md 待讨论区「多进程演进」的关键修正）。
    store = store or RunStore(max_in_flight=int(os.environ.get("CORTEX_MAX_CONCURRENT_RUNS", "3")))
    app = FastAPI(title="Personal Agent")
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

    # 记忆面板（021「护城河可视化」）：learned 三桶的读/改/删——固化管线的
    # 产出不再是黑箱。行号定位协议见 memory/learned.py 模块注释。
    @app.get("/api/learned")
    def learned_list():
        out = []
        for category in CATEGORIES:
            for entry in read_learned(LEARNED_DIR / f"{category}.md"):
                out.append({
                    "category": category,
                    "line": entry.line,
                    "date": entry.date,
                    "content": entry.content,
                })
        return out

    @app.put("/api/learned/{category}/{line}")
    def learned_update(category: str, line: int, body: LearnedUpdateRequest):
        if category not in CATEGORIES:
            raise HTTPException(400, "未知记忆类别")
        if not body.content.strip():
            raise HTTPException(400, "内容不能为空")
        path = LEARNED_DIR / f"{category}.md"
        if not path.is_file():
            raise HTTPException(404, "该类别暂无记忆")
        try:
            update_line(path, line, body.content.strip())
        except IndexError:
            raise HTTPException(404, "条目不存在") from None
        return {"ok": True}

    @app.delete("/api/learned/{category}/{line}")
    def learned_delete(category: str, line: int):
        if category not in CATEGORIES:
            raise HTTPException(400, "未知记忆类别")
        path = LEARNED_DIR / f"{category}.md"
        if not path.is_file():
            raise HTTPException(404, "该类别暂无记忆")
        try:
            delete_line(path, line)
        except IndexError:
            raise HTTPException(404, "条目不存在") from None
        return {"ok": True}

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
