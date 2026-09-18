"""笔记目录扫描：M7 起知识层的唯一入口——带文件名的扫描。

同步（sync.py）需要两样东西：文件名（写进 metadata.source 给人看）+ 原文（算内容指纹）。
旧 load_notes（只吐原文列表）已随 M7 同步上线退役删除——所有消费方都改走 sync。
"""

from pathlib import Path


def scan_notes(notes_dir: Path | str) -> list[tuple[str, str]]:
    """扫出目录下所有 .md 的 (文件名, 原文)。

    两道老防线保留，语义升级：
    - 目录不存在 → 报错（第一次跑还没建目录，立刻看到而不是静默空库）
    - 目录为空 → 报错（空目录 = 环境事故，绝不能被同步理解成「用户删光了
      笔记」而清空知识库——这是 sync.py 删除安全阀之外的第一道防线）
    """
    directory = Path(notes_dir)
    if not directory.exists():
        raise FileNotFoundError(f"笔记目录不存在：{directory.resolve()}")
    files = sorted(directory.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"笔记目录是空的：{directory.resolve()}")
    return [(f.name, f.read_text(encoding="utf-8")) for f in files]
