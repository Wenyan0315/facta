"""审计日志（S3 三件套之一）：工具调用 append-only 落盘。

写入点在 registry.execute 统一收口——工具执行的单一必经点，所有调用
自动落审，新工具零成本继承（素材库 #8「审计流事件化」的最小落地）。

与 Run 事件流的分工：Run 流给前端（内存、重启即失、可丢）；审计给人
（落盘、append-only、只增不改）。同一个事实的两个消费者。

分级记录（S3 裁定）：L0 只读工具结果截 100 字，L1 写类工具结果截 500
字——日志体积与可审计性的平衡；args 两级全记（工具参数都很小，真正
大的是结果：fetch_web 8000 字）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

READONLY_RESULT_MAX = 100   # L0 结果摘要长度
WRITE_RESULT_MAX = 500      # L1 结果摘要长度


class AuditLog:
    """append-only jsonl 审计：data/audit/audit-YYYYMMDD.jsonl 按天滚动。"""

    def __init__(self, base_dir: Path) -> None:
        self._dir = base_dir
        # S8a 起「多线程高频写」成真：多个会话的 worker 并发跑，工具调用都从
        # registry.execute 这一个口子落审。缓冲文件的一次 flush 可能拆成多个
        # write 系统调用，交错了就是一行 jsonl 被撕成两半——审计流不再可解析。
        self._lock = threading.Lock()

    def record(
        self,
        tool: str,
        args: dict,
        result: str,
        is_readonly: bool,
    ) -> None:
        """记一条工具调用。失败不炸调用方——审计是旁路，不该拖垮工具执行。"""
        try:
            limit = READONLY_RESULT_MAX if is_readonly else WRITE_RESULT_MAX
            event = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "tool": tool,
                "readonly": is_readonly,
                "args": args,
                "result": result if len(result) <= limit else result[:limit] + "…",
            }
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"audit-{datetime.now():%Y%m%d}.jsonl"
            with self._lock, open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass   # 审计写盘失败静默：工具本身的结果更重要，不能因审计炸了主流程

    def read(self, day: str | None = None) -> list[dict]:
        """读回审计（验收/测试用）。day 形如 '20260917'，缺省读最新一天。"""
        files = sorted(self._dir.glob("audit-*.jsonl")) if self._dir.is_dir() else []
        if not files:
            return []
        path = self._dir / f"audit-{day}.jsonl" if day else files[-1]
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
