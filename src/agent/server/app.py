"""FastAPI 壳：把内核 run_turn 与装配 ctx 接上 HTTP（S2b）。

分层（课件「Loop 生产事实，Gateway 传输事实」）：
- 路由      收发包（create/subscribe/cancel）
- 后台线程  把 run_turn 的 on_text/on_event 接进 Run Store，跑一轮
- Run Store 给事实编号、留存、可重放（承载状态机 + 取消标志）

内核 run_turn 一行不改——它产语义事实，本层负责事实的传输与生命周期。
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.memory.consolidate import consolidate
from agent.memory.store import (
    archive_session,
    derive_title,
    list_archived_sessions,
    restore_session,
    save_session,
)
from agent.orchestrator.assemble import MEMORY_PATH, AppContext
from agent.orchestrator.loop import run_turn
from agent.paths import LEARNED_DIR, SESSIONS_DIR
from agent.server.run_store import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
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


class CreateRunRequest(BaseModel):
    """创建 Run 的请求体。系统边界处用 JSON Schema 校验（与工具同一纪律）。"""

    text: str


def _run_worker(ctx: AppContext, run, user_text: str) -> None:
    """后台线程：跑一轮 run_turn，把事实灌进 Run Store，收尾时推终态。

    每轮落盘（finally，先于终态哨兵）：Web 壳是常驻进程，没有 CLI 的退出
    保存钩子——不落盘，服务被杀/崩溃就丢掉上次归档以来的全部对话（强杀
    丢数据边界的 web 版，真实使用踩中：会话只在点归档类操作时才写盘）。
    """
    run.status = STATUS_RUNNING
    run.emit("run.started", {})
    status = STATUS_FAILED
    try:
        reply = run_turn(
            ctx.session,
            user_text,
            llm=ctx.llm,
            registry=ctx.registry,
            summarizer=ctx.internal_llm,
            on_text=lambda text: run.emit("text.delta", {"delta": text}),
            on_event=lambda type_, data: run.emit(_EVENT_MAP.get(type_, type_), data),
            should_cancel=lambda: run.cancel_requested,
        )
        # 终态判定：有最终回答 → completed；否则看是否因取消 → cancelled / failed
        if reply is not None:
            run.preview = (reply.content or "")[:300]   # 任务视图的交付摘要
            status = STATUS_COMPLETED
        elif run.cancel_requested:
            status = STATUS_CANCELLED
    except Exception as exc:   # 防御性兜底：run_turn 已捕获 LLMUnavailableError，这里是意外
        run.emit("error", {"message": str(exc)})
    finally:
        try:
            save_session(ctx.session, MEMORY_PATH)
        except Exception as exc:   # 落盘失败不吞终态：告知用户，流照常收口
            run.emit("error", {"message": f"会话落盘失败：{exc}"})
        run.finish(status)


def _archive_current(ctx: AppContext) -> None:
    """切出当前会话：save → 复盘 → 归档 → 清空内存（原地 clear，不 rebind）。

    与 CLI /new 同一序列。原地 clear 不 rebind——search_history 工具的闭包抓的
    是 ctx.session.messages 这个列表对象本身（列表身份陷阱，S1 反面教材正用）。
    """
    save_session(ctx.session, MEMORY_PATH)
    consolidate(ctx.session, ctx.internal_llm, LEARNED_DIR, since=0)   # v1 简化：全量复盘
    archive_session(MEMORY_PATH, SESSIONS_DIR)
    ctx.session.messages.clear()
    ctx.session.summary = None
    ctx.session.summarized_upto = 1


def _switch_session(ctx: AppContext, archive_path) -> None:
    """切回历史会话：切出当前 → 切入目标（move 语义）→ 内存原地换血。"""
    _archive_current(ctx)
    restored = restore_session(archive_path, MEMORY_PATH)   # 归档写回 active 后消失
    ctx.session.messages.extend(restored.messages)
    ctx.session.summary = restored.summary
    ctx.session.summarized_upto = restored.summarized_upto


def create_app(ctx: AppContext, store: RunStore | None = None) -> FastAPI:
    store = store or RunStore()
    app = FastAPI(title="Personal Agent")
    static_dir = Path(__file__).parent / "static"

    @app.post("/api/runs", status_code=202)
    def create_run(body: CreateRunRequest):
        run = store.create_if_idle(title=body.text[:60])
        if run is None:
            raise HTTPException(409, "已有任务在运行，请稍候再发")
        # daemon 线程：服务退出时不留阻塞；请求线程立即 202 返回
        threading.Thread(target=_run_worker, args=(ctx, run, body.text), daemon=True).start()
        return {"run_id": run.run_id}

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
            for ev in list(run.events):
                if ev.seq > last:
                    yield encode_sse(run.run_id, ev)
                    last = ev.seq   # 更新哨兵，防实时段重复发

            # 实时读：阻塞 + 心跳保活；None 哨兵 = Run 结束
            q = run.subscribe()
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

    # 会话列表与切回（S2 会话列表原料 = store.list_archived_sessions）
    @app.get("/api/sessions")
    def list_sessions():
        return [
            {"name": path.name, "title": title}
            for path, title in list_archived_sessions(SESSIONS_DIR)
        ]

    @app.post("/api/sessions/new")
    def new_session():
        if store.active_run() is not None:
            raise HTTPException(409, "有任务在运行，无法开新会话")
        _archive_current(ctx)
        return {"ok": True}

    @app.post("/api/sessions/{name}/switch")
    def switch_session(name: str):
        if store.active_run() is not None:
            raise HTTPException(409, "有任务在运行，无法切换会话")
        archive_path = SESSIONS_DIR / name
        if not archive_path.is_file():
            raise HTTPException(404, "会话不存在")
        _switch_session(ctx, archive_path)
        return {"title": derive_title(ctx.session)}

    # 历史消息：当前 active 会话的 user/assistant 轮（system=人设、tool=中间产物，
    # 不进对话回放；空 content 的纯点菜轮跳过——历史回放只讲故事线）
    @app.get("/api/messages")
    def messages():
        return [
            {"role": m.role, "content": m.content}
            for m in ctx.session.messages
            if m.role in ("user", "assistant") and m.content
        ]

    # 任务列表：Run Store 全量（新的在前），任务视图原料
    @app.get("/api/runs")
    def runs():
        return [
            {"run_id": r.run_id, "status": r.status, "title": r.title, "preview": r.preview}
            for r in store.list_runs()
        ]

    # 任务视图（S2 双视图的另一半，v1 最小版：运行记录+状态+交付摘要）
    @app.get("/tasks")
    def tasks():
        return FileResponse(static_dir / "tasks.html")

    # 前端静态文件挂根路径；check_dir=False 让本模块先于前端文件就位（测试友好）
    app.mount("/", StaticFiles(directory=static_dir, html=True, check_dir=False), name="static")

    return app
