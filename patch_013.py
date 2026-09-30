"""#13 一次性补丁：files.py 丁案（原子写）+ 丙案（覆盖变短警告），test_files.py 追加验收。

刻意用「精确子串替换 + 命中数校验」而不是整文件重写——本 issue 说的就是整文件重写
会静默截断。任一处 old 块没命中就报错退出，绝不写盘（宁可失败也不写半截）。
用完即删（不进 PR）。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path("src/facta/tools/files.py")
TEST = Path("tests/test_files.py")


def sub(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        print(f"[FAIL] {label}：期望命中 1 次，实际 {n} 次")
        idx = text.find(old.splitlines()[0] if old.splitlines() else old)
        if idx >= 0:
            print("  附近原文 repr：")
            print(repr(text[max(0, idx - 200): idx + 900]))
        sys.exit(1)
    print(f"[ok] {label}")
    return text.replace(old, new)


src = SRC.read_text(encoding="utf-8")
orig_src = src

# ① import：原子写要 os + uuid（uuid 防并发写同一目标互踩 tmp，P1-6 教训）
src = sub(src, "import difflib\nimport re\n", "import difflib\nimport os\nimport re\nimport uuid\n", "import")

# ② 模块 docstring 的栅栏清单补两条（自述式风格：改哪段写哪段）
src = sub(
    src,
    "  ④ 分级：read/search/list = L0；write_file = L1（审计强化）\n",
    "  ④ 分级：read/search/list = L0；write_file = L1（审计强化）\n"
    "  ⑤ 写盘原子（#13 丁案）：走同目录 tmp + os.replace（save_session / 面板保存同款），\n"
    "     写失败不留半截、旧内容不被中途截断\n"
    "  ⑥ 覆盖变短显式警告（#13 丙案）：read 窗口与 write 全量的不对称，不能靠模型心算\n",
    "docstring",
)

# ③ 写盘主体：丁案（原子写）+ 丙案（变短警告）
old_tail = '''        if len(diff) > MAX_DIFF_LINES:
            diff_note += f"\\n〔diff 共 {len(diff)} 行，已截断〕"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"写入失败：{e}"
    action = "覆盖" if overwritten else "新建"
    return f"已{action} {path}（{len(content)} 字）{diff_note}"
'''

new_tail = '''        if len(diff) > MAX_DIFF_LINES:
            diff_note += f"\\n〔diff 共 {len(diff)} 行，已截断〕"

    # 丙案（#13）：覆盖后文件变短 → 显式硬警告。read_file 默认只给 100 行窗口，write_file
    # 却是全量覆写——读一半就写会静默截断尾部（#10 修 web.py 时的实证事故），而 diff 自己
    # 也截断到 40 行，事后信号只剩这一处。**只警告不拒写**：正常删减代码不该被挡；「拒写」
    # 要动 write_file 的契约（甲/乙案），留给维护者单独裁定。
    shrink_note = ""
    if overwritten:
        old_lines, new_lines = len(old_text.splitlines()), len(content.splitlines())
        if new_lines < old_lines:
            shrink_note = (
                f"\\n⚠ 新内容比原文件少 {old_lines - new_lines} 行"
                f"（原 {old_lines} 行 → 新 {new_lines} 行）。若本次是「只读了窗口内一部分就全量重写」，"
                f"尾部已被截断——请用 read_file 的 offset/limit 分段读完整个文件，确认无误后整体重写。"
            )

    # 丁案（#13）：原子写——同目录 tmp + os.replace，与 save_session（ADR 040）、笔记面板
    # 保存（042）同款手法。此前直接 write_text 是「先截断再写」：进程在写盘中途被杀，旧内容
    # 当场消失、新内容半截，截断从此不可恢复（换个环境就是纯数据丢失）。同文件系统内换名是
    # 原子的——崩溃只可能看到旧版或新版，没有中间态。它不阻止截断，但把「静默丢失」降级成
    # 「可恢复」。tmp 名带 uuid（并发写同一目标不互踩）；后缀 .tmp 不匹配任何 *.py/*.md glob，
    # 半截临时文件不会混进清单或被打包。不做 fsync：与 store.py 同判据——威胁模型是「进程被
    # 杀」，内核 page cache 仍在，掉电持久化是另一档需求。
    tmp = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(content, encoding="utf-8")
        if overwritten:
            tmp.chmod(target.stat().st_mode)   # 保住原权限位：换名会把可执行脚本的 +x 写掉
        os.replace(tmp, target)
    except OSError as e:
        if tmp is not None:                    # 失败路径清尾巴（replace 没跑成，tmp 还留在盘上）
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return f"写入失败：{e}"
    action = "覆盖" if overwritten else "新建"
    return f"已{action} {path}（{len(content)} 字）{shrink_note}{diff_note}"
'''
src = sub(src, old_tail, new_tail, "write body")

# ④ write_file 的 description：把「变短时警告」写进工具自述（模型看得见的地方）
src = sub(
    src,
    'description="写入项目工作区文件（新建或覆盖）。覆盖已有文件时返回 diff 改动摘要。',
    'description="写入项目工作区文件（新建或覆盖）。覆盖已有文件时返回 diff 改动摘要；'
    '覆盖后内容比原文件变短会显式警告（防「只读了窗口内一部分就全量重写」截断尾部）。',
    "tool description",
)

assert src != orig_src
SRC.write_text(src, encoding="utf-8")
print(f"[ok] 已写回 {SRC}")

# ⑤ 测试追加（append——不重写既有 189 行，避免同一类事故发生在测试文件上）
test = TEST.read_text(encoding="utf-8")
test = sub(
    test,
    "- write_file 覆盖必返 diff（改了什么一眼可见）\n",
    "- write_file 覆盖必返 diff（改了什么一眼可见）\n"
    "- 写盘原子（tmp+os.replace）：失败不留半截、旧内容不丢（#13 丁案）；\n"
    "  覆盖变短显式警告（#13 丙案）\n",
    "test docstring",
)

test += '''

# ---------- #13：读窗口 / 全量覆写的不对称（截断事故的机制性防线） ----------

def test_read_file_flags_unread_tail(tmp_path):
    """丙案（读侧）：超窗口必须明确回报「共 N 行 + 还有 M 行未读」，不靠模型心算。"""
    (tmp_path / "long.py").write_text("\\n".join(f"line{i}" for i in range(150)) + "\\n", encoding="utf-8")

    out = _read_file("long.py", root=tmp_path)          # 默认窗口 100 行
    assert "共 150 行" in out
    assert "下方还有 50 行" in out and "offset=101" in out   # 硬提示 + 续读指路
    assert "line150" not in out                          # 尾部确实没返回（事故的输入侧）


def test_write_file_warns_when_content_shrinks(tmp_path):
    """丙案（写侧）：覆盖后变短 → 量化警告 + 指路（#13 事故形态：读 100 行、写回 100 行、丢 50 行）。"""
    (tmp_path / "long.py").write_text("\\n".join(f"line{i}" for i in range(150)) + "\\n", encoding="utf-8")
    partial = "\\n".join(f"line{i}" for i in range(100)) + "\\n"   # 「读一半就写」的产物

    out = _write_file("long.py", partial, root=tmp_path)
    assert "已覆盖" in out
    assert "少 50 行" in out and "read_file" in out       # 量化差额 + 指路怎么补读

    # 继续变短照样提醒（不是「首次覆盖才提醒」的偶然）
    shorter = "\\n".join(f"line{i}" for i in range(90)) + "\\n"
    assert "少 10 行" in _write_file("long.py", shorter, root=tmp_path)


def test_write_file_no_shrink_warning_otherwise(tmp_path):
    """正对照：变长 / 等长 / 新建都不误报（警告只在真变短时出现，正常删减不被挡）。"""
    (tmp_path / "a.py").write_text("x\\ny\\n", encoding="utf-8")

    assert "⚠" not in _write_file("a.py", "x\\ny\\nz\\n", root=tmp_path)      # 变长
    assert "⚠" not in _write_file("a.py", "x\\ny\\nz\\n", root=tmp_path)      # 等长（同内容 → 根本不写）
    assert "⚠" not in _write_file("new.py", "x\\n", root=tmp_path)            # 新建


def test_write_file_is_atomic_on_failure(tmp_path, monkeypatch):
    """丁案：写盘失败（replace 没跑成）时旧内容原样还在、半截 tmp 不留尾巴。"""
    target = tmp_path / "a.py"
    target.write_text("old1\\nold2\\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("模拟写盘中途被杀")

    monkeypatch.setattr("facta.tools.files.os.replace", boom)
    out = _write_file("a.py", "new\\n", root=tmp_path)

    assert "写入失败" in out
    assert target.read_text(encoding="utf-8") == "old1\\nold2\\n"   # 旧内容没被截断（可恢复）
    assert [p.name for p in tmp_path.iterdir()] == ["a.py"]          # 半截 tmp 已清走


def test_write_file_leaves_no_tmp_residue(tmp_path):
    """丁案正向：成功路径换名走，无 *.tmp 残留，磁盘内容与提交一致。"""
    out = _write_file("a.py", "print('v1')\\n", root=tmp_path)

    assert "已新建" in out and "写入失败" not in out
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "print('v1')\\n"
    assert [p.name for p in tmp_path.iterdir()] == ["a.py"]


def test_write_file_keeps_exec_bit(tmp_path):
    """丁案细节：换名不得把原文件权限位写掉（可执行脚本的 +x 要保住）。"""
    target = tmp_path / "run.sh"
    target.write_text("#!/bin/sh\\necho v1\\n", encoding="utf-8")
    target.chmod(0o755)

    _write_file("run.sh", "#!/bin/sh\\necho v2\\n", root=tmp_path)

    assert target.stat().st_mode & 0o777 == 0o755
'''

TEST.write_text(test, encoding="utf-8")
print(f"[ok] 已追加测试到 {TEST}")
