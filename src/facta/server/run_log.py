"""Run 明细持久化（M5 承诺件 a）：data/tasks/runs/ 下的挂账式落盘。

每个 Run 两件（延续 TaskStore 的 data/tasks/ 挂账口径）：
    <run_id>.json     元快照（原子写，TaskStore.save 同款 tmp + os.replace）
    <run_id>.jsonl    事件流（append-only，一行一事件，emit 即落盘）

存在意义：Run 明细此前只活在服务进程内（README「已知边界」表第一行）——
重启后任务卡下钻是空的。落盘后 RunStore.get/list_runs 从这里重建只读 Run，
RunDetail 的计划面板、确认卡的断线重放全部照旧工作。

写入时机（与「模型可见即已记录」的事件哲学一致）：
    Run 创建   元快照初版（title/session/task 归属就位）
    emit       事件行追加（先落盘再广播——客户端看见的事实必然已在盘上）
    finish     元快照终版（status/preview/reply_text 就位）

边界：单进程本地盘。多实例部署换外置存储（与 RunStore/TaskStore 同一取舍）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from facta.server.run_store import RunEvent

logger = logging.getLogger(__name__)


def _valid_id(run_id: str) -> bool:
    """run_id 是 HTTP 路径进来的外部输入：只认 uuid hex（TaskStore._path 同款界碑，
    防 `../../` 路径逃逸）。非法 id 读侧当不存在（404），写侧只可能来自程序 bug。"""
    return bool(run_id) and all(c in "0123456789abcdef" for c in run_id)


class RunLog:
    """Run 明细的落盘面。root 下每 Run 一对文件；读写都以 run_id 校验打头。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _meta_path(self, run_id: str) -> Path:
        if not _valid_id(run_id):
            raise ValueError(f"非法 run_id：{run_id}")
        return self.root / f"{run_id}.json"

    def _events_path(self, run_id: str) -> Path:
        if not _valid_id(run_id):
            raise ValueError(f"非法 run_id：{run_id}")
        return self.root / f"{run_id}.jsonl"

    def write_meta(self, run: Any) -> None:
        """元快照原子落盘（run 用鸭子类型：只读 Run 的展示/归属字段）。

        字段显式挑（与 API to_dict 同纪律：内部线程/锁字段绝不进盘）。失败
        只告警不上抛：落盘是旁路，磁盘故障不该把正在跑的 Run 陪葬——告警
        是如实通道，主事件流（内存）照常。
        """
        meta = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "task_id": run.task_id,
            "status": run.status,
            "title": run.title,
            "preview": run.preview,
            "reply_text": run.reply_text,
            "created_at": run.created_at,
        }
        try:
            path = self._meta_path(run.run_id)
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, ValueError):
            logger.warning("Run 元快照落盘失败（run_id=%s）", run.run_id, exc_info=True)

    def append_event(self, run_id: str, event: RunEvent) -> None:
        """事件行追加（append-only）。失败只告警不上抛，理由同 write_meta。"""
        line = json.dumps(
            {"seq": event.seq, "type": event.type, "data": event.data}, ensure_ascii=False
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self._events_path(run_id), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except (OSError, ValueError):
            logger.warning("Run 事件落盘失败（run_id=%s seq=%s）", run_id, event.seq, exc_info=True)

    def load(self, run_id: str) -> tuple[dict, list[RunEvent]] | None:
        """读回 (元快照, 事件序列)；不存在/非法 id 给 None（调用方 404）。

        事件行损坏（崩溃截断的半行）跳过并告警——最后半行丢掉，之前的
        明细照常可看，不因一行脏数据放弃整个 Run。
        """
        try:
            meta_path = self._meta_path(run_id)
            events_path = self._events_path(run_id)
        except ValueError:
            return None
        if not meta_path.is_file():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        events: list[RunEvent] = []
        if events_path.is_file():
            for raw in events_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    events.append(RunEvent(seq=int(d["seq"]), type=d["type"], data=d.get("data") or {}))
                except (ValueError, KeyError, TypeError):
                    logger.warning("Run 事件行损坏，跳过（run_id=%s）", run_id)
        return meta, events

    def list_metas(self) -> list[dict]:
        """全部元快照（/api/runs 列表合并用；事件按需另读，不为列清单全量扫 jsonl）。

        ponytail: O(N) 扫盘，个人规模（百级 Run）够用；升级路径与
        RunStore/TaskStore 同一触发信号——多实例部署换外置存储。
        """
        if not self.root.is_dir():
            return []
        out: list[dict] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                logger.warning("Run 元快照损坏，跳过（%s）", p.name)
        return out
