"""项目记忆归属（ADR 116 §3）：登记表 + 双模式 learned 目录 + 归属判定单一函数。

data/projects/projects.json 是归属判定的**唯一真值源**（单文件原子写）；条目：

    {"name": ..., "repo_root": ..., "memory_mode": "centralized"|"in-repo",
     "learned_path": ...}

learned_path 登记时按模式推导落盘、读时以盘上为准（不重推）。目录：
集中式 = data/projects/<name>/learned/（三桶文件名与行格式全不变，
read_learned/consolidate 全套复用）；就地式 = <repo_root>/.facta/learned/
（往工作仓写文件须显式同意——114 拍板，创建时 confirm_repo_write 必须为真）。

归属判定只有一个入口 resolve_learned_dir(repo_root)：①登记命中 → 该项目目录；
②repo_root == WORKSPACE_ROOT → 本仓全局 data/learned/（现状零迁移）；③其余 →
None（外部未登记仓，注入不给、固化回落全局——115/116 触发信号挂档）。
**禁止登记本仓**（repo_root == WORKSPACE_ROOT 的登记一律拒）：本仓恒走②，
避免同一份记忆两个读入口（评审问题 6 裁定）。

迁移（memory_mode 切换 + migrate=true）＝**复制**三桶到新址（045/064「记忆
不可逆」保守：不删源），登记表改模式；源目录留档后不再被任何代码读取。
迁移与 settle 并发写同目录的保护挂触发信号（M2 自用场景，撞首例再议锁）。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from facta.memory.consolidate import CATEGORIES
from facta.paths import LEARNED_DIR, PROJECTS_DIR, WORKSPACE_ROOT

MEMORY_MODES = ("centralized", "in-repo")


def _derive_learned_path(root: Path, name: str, repo_root: Path, memory_mode: str) -> Path:
    """learned 目录按模式推导（登记/迁移时落盘；读时以盘上为准）。"""
    if memory_mode == "in-repo":
        return repo_root / ".facta" / "learned"
    return root / name / "learned"


class ProjectsManifest:
    """登记表读写（data/projects/projects.json）。无缓存：每次调用重读盘——
    文件即真值源，避免内存快照与盘漂移（条目个位数，读盘开销可忽略）。
    原子写（tmp + os.replace）防半截 JSON，与 TaskStore 同款。
    """

    def __init__(self, root: Path | None = None) -> None:
        # 懒取 PROJECTS_DIR（函数体里读全局）：测试 monkeypatch 模块常量即可隔离
        self.root = root or PROJECTS_DIR

    @property
    def path(self) -> Path:
        return self.root / "projects.json"

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        return list(self._load().values())

    def get(self, name: str) -> dict | None:
        return self._load().get(name)

    def by_repo_root(self, repo_root: Path) -> dict | None:
        """按 repo_root 反查（归属判定用）。比较前统一 expanduser+resolve：
        登记与任务里的路径写法可能不同（~/x、相对路径），归属不能因写法漂移。"""
        target = repo_root.expanduser().resolve()
        for entry in self._load().values():
            if Path(entry["repo_root"]).expanduser().resolve() == target:
                return entry
        return None

    def register(
        self,
        name: str,
        repo_root: str,
        memory_mode: str = "centralized",
        confirm_repo_write: bool = False,
    ) -> dict:
        """登记新项目并建 learned 目录（登记即建目录）。

        ValueError = 输入不合法（名字穿越/路径不存在/登记本仓/模式未知/就地式
        未确认/名字或路径已登记）；调用方（server 层）按语义映射 400/409。
        """
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError(f"非法项目名：{name!r}")
        if memory_mode not in MEMORY_MODES:
            raise ValueError(f"memory_mode 须为 {MEMORY_MODES} 之一：{memory_mode!r}")
        root = Path(repo_root).expanduser()
        if not root.is_dir():
            raise ValueError(f"repo_root 不存在或不是目录：{repo_root}")
        root = root.resolve()
        if root == WORKSPACE_ROOT:
            raise ValueError("本仓（WORKSPACE_ROOT）禁止登记为项目：恒走全局记忆，双读入口会漂移")
        if memory_mode == "in-repo" and not confirm_repo_write:
            raise ValueError("就地式记忆会往工作仓写 .facta/ 目录，须显式确认（confirm_repo_write=true）")
        data = self._load()
        if name in data:
            raise ValueError(f"项目已登记：{name}")
        if any(Path(e["repo_root"]).expanduser().resolve() == root for e in data.values()):
            raise ValueError(f"该仓库已登记为其他项目：{repo_root}")
        learned = _derive_learned_path(self.root, name, root, memory_mode)
        learned.mkdir(parents=True, exist_ok=True)
        entry = {
            "name": name,
            "repo_root": str(root),
            "memory_mode": memory_mode,
            "learned_path": str(learned),
        }
        data[name] = entry
        self._save(data)
        return entry

    def update(
        self,
        name: str,
        memory_mode: str | None = None,
        migrate: bool = False,
        confirm_repo_write: bool = False,
    ) -> dict:
        """切换 memory_mode（可选 migrate=true 复制三桶到新址）。

        迁移 = 复制不删源（045/064 记忆不可逆保守）；不带 migrate 时只改指向，
        内容留在原目录（不再被读取）。
        """
        data = self._load()
        entry = data.get(name)
        if entry is None:
            raise ValueError(f"项目未登记：{name}")
        if memory_mode is not None and memory_mode not in MEMORY_MODES:
            raise ValueError(f"memory_mode 须为 {MEMORY_MODES} 之一：{memory_mode!r}")
        mode = memory_mode or entry["memory_mode"]
        repo_root = Path(entry["repo_root"])
        if mode == entry["memory_mode"]:
            return entry
        if mode == "in-repo" and not confirm_repo_write:
            raise ValueError("就地式记忆会往工作仓写 .facta/ 目录，须显式确认（confirm_repo_write=true）")
        new_learned = _derive_learned_path(self.root, name, repo_root, mode)
        if migrate:
            old_learned = Path(entry["learned_path"])
            new_learned.mkdir(parents=True, exist_ok=True)
            for category in CATEGORIES:
                src = old_learned / f"{category}.md"
                if src.is_file():
                    shutil.copy2(src, new_learned / f"{category}.md")
        entry = {**entry, "memory_mode": mode, "learned_path": str(new_learned)}
        data[name] = entry
        self._save(data)
        return entry


def resolve_learned_dir(repo_root: Path | None, own_dir: Path | None = None) -> Path | None:
    """归属判定（单一函数，优先级见模块头）→ 项目 learned 目录 / 本仓全局 / None。

    own_dir = 本仓全局 learned 的位置（缺省 paths.LEARNED_DIR）。测试注入点：
    消费方（assemble/app）各传自己命名空间里的 LEARNED_DIR，monkeypatch 消费方
    常量即可整链隔离——与 user_memory_path() 同款理由。repo_root=None 视为
    本仓（交互 chat 与无归属任务轮）。
    """
    own = own_dir if own_dir is not None else LEARNED_DIR
    root = repo_root or WORKSPACE_ROOT
    entry = ProjectsManifest().by_repo_root(root)
    if entry is not None:
        return Path(entry["learned_path"])
    return own if root.expanduser().resolve() == WORKSPACE_ROOT else None
