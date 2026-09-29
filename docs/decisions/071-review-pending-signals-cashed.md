# ADR 071：评审挂信号兑现轮——四条边界同时收口

- 状态：已采纳
- 日期：2026-09-29
- 触发：评审文档 P1/P2 之外「刻意不做」与「挂信号」分流的兑现
- 关联：评审报告（内部文档）、069（评审修复轮）、070（开源兼容轮）

## 背景

069 修了 6 P1 + 3 P2，但评审报告与 ADR 069 自留了五条挂信号与四条刻意不做。这些不是「评审打勾没用」，是「修复需要独立决策/独立工作量」。本轮按 048「挂信号不是真不做——等真消费者」原则，四条真消费者出现的同期收口，一条仍挂信号。

## 决策

### ① FACTA_DATA_DIR：数据目录与代码目录分离（评审 § 产品边界）

`src/facta/paths.py` 把 `DATA_ROOT` 从硬编码 `WORKSPACE_ROOT / "data"` 改为 env 优先：
- 设 `FACTA_DATA_DIR` → `Path(env).resolve()`
- 未设 → 回 `WORKSPACE_ROOT / "data"`（开发态零变化）

**真消费者**：评审点名的「`pip install facta` 而非 `-e .`」——代码落在 site-packages/facta，`__file__.parents[2]` 跳到 site-packages 之外，`WORKSPACE_ROOT / "data"` 写到不可写位置且首次启动找不到 notes。设 `FACTA_DATA_DIR` 后用户可把数据放到任意可写位置。

围栏（`MEMORY_WRITE_FENCE`）相对路径 "data/notes/learned/graph.json" 在 `FACTA_DATA_DIR` 设后替换为绝对根，开发态（pip install -e）零变化。

### ② shutil.which 验源（评审 § P1-3 延伸，069 刻意不做）

`src/facta/tools/terminal.py` 给白名单命令加 `_basename_resolves_to_system(head)` 二级闸：
- `shutil.which(head)` 拿当前 PATH 顺序的真实路径
- `Path(real).resolve()` 解析 symlink
- 前缀不在 `_SAFE_SYSTEM_DIRS` → 触发"挡「PATCH 注入」"

`_SAFE_SYSTEM_DIRS` 含 `/usr/bin /bin /usr/sbin /sbin /usr/local/bin /opt/homebrew/bin` + 开发场景放宽 `<workspace>/.venv/bin`。开发本场景放宽。

挡的是「`~/.local/bin/echo`」——同 basename 抢在 `/bin/echo` 前面；不挡「`/bin/echo` 本身被改」——seatbelt 才是那种情形的硬隔离（048）。

### ③ SessionStore 跨进程 flock（评审 § P1-6 盲区）

`src/facta/memory/store.py` 加 `fcntl.flock` 文件锁：
- 永久持有 lockfile 的 fd（POSIX 上 flock 在 fd 关闭时自动释放，必须跨临界区持同一 fd）
- `create()` 临界区 = threading.Lock（已有）+ flock（新增）
- 跨进程（CLI + Web 并发 / 多 worker）互斥
- Windows 缺 fcntl → 降级回线程锁（已知边界，沙箱仅 macOS 同期挂信号）

### ④ learned 稳定 id（评审 § 产品边界「记忆编辑采用行号身份」）

`src/facta/memory/learned.py` 加 id 落盘协议：
- 每行尾追加 `<!--id:xxx-->` HTML 注释（8 字节 hex，sha256(date|content|seq)[:4]）
- 既有 `update_line / delete_line` 保留（向后兼容；面板 UI 不动）
- 新增 `update_line_by_id / delete_line_by_id`（并发互删场景下 id 不漂）
- 老文件没 id → `read_learned` 返回 `id=None`，旧契约照常工作

挂信号只剩「**前端切到 by-id**」——契约具备，UI 切换是产品级决定，等真用户提并发错位的痛点再切。

## 挂信号保留

评审原文：「记忆编辑采用行号身份」的后半段是「panel UI 改行号身份」——本轮把契约层补齐，UI 切换不强制同步。触发信号：用户报告「两个标签页互删错位」或「行号漂了」。

## 验证

- **测试**：746 passed, 2 skipped（基线 739，净 +7）
  - `test_path_injected_basename_requires_confirm`：tmp_path 造 `~/.local/bin/echo` → 触发确认
  - `test_whitelisted_basename_outside_system_dirs_requires_confirm`：裸 pytest / Trae 自带 ripgrep
  - `test_cross_process_create_same_second_gets_unique_ids`：3 个 multiprocessing 子进程并发 create
  - `test_id_round_trip / test_make_id_is_deterministic / test_update_by_id_preserves_id` + by-id 增改删
- **ruff**：All checks passed
- **mypy**：Success: no issues found in 59 source files

## 影响面

| 文件 | 改动 |
|---|---|
| [src/facta/paths.py](../../src/facta/paths.py) | DATA_ROOT env 优先 |
| [src/facta/tools/terminal.py](../../src/facta/tools/terminal.py) | shutil.which 验源 + _SAFE_SYSTEM_DIRS |
| [src/facta/memory/store.py](../../src/facta/memory/store.py) | fcntl.flock + lockfile fd 永持 |
| [src/facta/memory/learned.py](../../src/facta/memory/learned.py) | id 落盘协议 + by-id 增改删 |
| [tests/test_terminal.py](../../tests/test_terminal.py) | 1 条 PATH 注入 + 1 条白名单 outside system dirs |
| [tests/test_store.py](../../tests/test_store.py) | 1 条跨进程并发 create |
| [tests/test_learned.py](../../tests/test_learned.py) | 5 条 id 协议与并发场景 |

## 触发信号

- 评审原文「工作目录固定仓库根」余下条款（用户工作目录语义）→ 真用户提需求时
- PATH 注入层漏报（用户真实路径是 `/opt/homebrew/bin` 但前缀被新白名单挡了）→ 加新前缀
- flock 在 macOS 上 stale 锁（跨进程 create 永远阻塞）→ 加 LOCK_NB + 超时
- 面板 UI 端报「行号错位」→ 推进 id 协议切到 UI