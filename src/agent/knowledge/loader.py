from pathlib import Path

def load_notes(notes_dir: Path | str = "data/notes") -> list[str]:
    directory = Path(notes_dir)          # 统一转成 Path（兼容传字符串）
    if not directory.exists():
        raise FileNotFoundError(f"笔记目录不存在：{directory.resolve()}")

    files = sorted(directory.glob("*.md"))   # 找到所有 .md，按文件名排序

    if not files:
        raise FileNotFoundError(f"笔记目录是空的：{directory.resolve()}")

    return [f.read_text(encoding="utf-8") for f in files]   # 逐个读成列表