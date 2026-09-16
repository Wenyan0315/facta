"""个人待办存储（2026-09-17，场景「个人助理」；语义裁定见 014 产品定位）。

与 session.json 的分工：待办是**跨会话资产**（不随会话归档走），
独立文件 data/todos.json（运行时数据，gitignore 同纪律）。

并发：Web UI 勾销与 agent 工具添加可能同时发生——store 级互斥锁
罩住「load→改→save」整个序列，读路径也过锁（简单优先：无锁读会
看到撕裂状态，锁竞争在个人工具量级不存在）。

id 自增（max+1）：勾销要引用，时间戳 id 对模型不友好（念不全）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

TODO_VERSION = 1


@dataclass
class Todo:
    """一条待办：id 引用键（勾销用）、done 状态、时间戳程序加。"""

    id: int
    text: str
    done: bool = False
    created: str = ""
    done_at: str | None = None


class TodoStore:
    """文件持久化的待办仓库：进程内单实例（装配层构造，注入 ToolContext/API）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> list[Todo]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            return []   # 损坏文件当空仓（待办非关键数据，大声崩会拖垮整个入口）
        return [Todo(**d) for d in raw.get("todos", [])]

    def _save(self, todos: list[Todo]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {"version": TODO_VERSION, "todos": [asdict(t) for t in todos]},
                ensure_ascii=False, indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    def add(self, text: str) -> Todo:
        with self._lock:
            todos = self._load()
            next_id = max((t.id for t in todos), default=0) + 1
            todo = Todo(id=next_id, text=text, created=datetime.now().isoformat(timespec="seconds"))
            todos.append(todo)
            self._save(todos)
            return todo

    def complete(self, todo_id: int) -> Todo | None:
        """勾销：找到并标记返回该条；id 不存在返回 None（工具层转成可自纠提示）。"""
        with self._lock:
            todos = self._load()
            for t in todos:
                if t.id == todo_id:
                    if not t.done:   # 重复勾销幂等：不覆盖首次完成时间
                        t.done = True
                        t.done_at = datetime.now().isoformat(timespec="seconds")
                        self._save(todos)
                    return t
            return None

    def delete(self, todo_id: int) -> Todo | None:
        """删除：这条不该存在（写错了/不想要了），与勾销（做完了，保留历史）语义分离。

        id 空洞不复用：删 #2 后下一条仍是 #3——模型/用户记忆里的旧编号
        不指向错条目（id 是引用键，复用=悬垂引用）。
        """
        with self._lock:
            todos = self._load()
            for i, t in enumerate(todos):
                if t.id == todo_id:
                    del todos[i]
                    self._save(todos)
                    return t
            return None

    def update_text(self, todo_id: int, text: str) -> Todo | None:
        """修改待办文本（错字/补充信息）；状态与时间戳不动。"""
        with self._lock:
            todos = self._load()
            for t in todos:
                if t.id == todo_id:
                    t.text = text
                    self._save(todos)
                    return t
            return None

    def list(self, only_pending: bool = False) -> list[Todo]:
        with self._lock:
            todos = self._load()
        return [t for t in todos if not (only_pending and t.done)]
