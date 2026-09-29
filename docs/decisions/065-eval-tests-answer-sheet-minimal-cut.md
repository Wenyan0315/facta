# 065 冻结评测 tests/ 最小切口隔离：只列 tests/test_frozen_eval.py（草案）

> 状态：已落地（2026-09-29 用户拍板最小切口，同日实现，见文末「实现」节）｜日期：2026-09-29
> 编号说明：064 已被 P0-7 枚举审查占用，本草案用下一个空号 065。
> 立项出处：[architecture.md 活清单](../architecture.md)「harness 源码对副本可见」条「同日补核」段，及 [063](063-eval-harness-answer-sheet-isolation.md)「实现」⑤。
> 一句话结论：**只把评测器自己的测试 `tests/test_frozen_eval.py` 列进案卷清单与污染检测，其余 `tests/` 不动——因为 i4 的题面要求读 `tests/test_sandbox.py`／`test_security.py` 搭脚手架。**

## 背景与动机

063 把整个 `evals/` 出考场后，i4 本轮轨迹不再读 `evals/frozen_eval.py`，改读 `tests/test_frozen_eval.py`（offset=240 limit=160，正是隔离与污染检测的测试）——`tests/` 既不在 `_ANSWER_SHEETS` 也不在 `_CONTAMINATION_MARKERS`，所以 `contaminated` 仍空，而它读到的是判分口径。

但 `tests/` 不能照抄 063 甲案一刀加 `"tests"`：[063「实现」⑤](063-eval-harness-answer-sheet-isolation.md) 已核题面逐字——i4 的 task 是「帮我给 sandbox_demo 搭个测试脚手架，按它 README 里『测试』那一节的要求来」，读 `tests/test_sandbox.py`／`test_security.py` 是照现有测试写法搭脚手架、题面驱动的正当行为（其 verify 反而与测试无关＝数 `data/notes/*.md` 是否仍 15 篇）。r5／r6 经核也不依赖 `tests/`。

⇒ 唯一站得住的最小切口是**只列 `tests/test_frozen_eval.py`**：它是评测器自己的测试，含隔离与污染检测的断言口径；i4 搭脚手架不需要参照它。

## 地面真值

1. `tests/test_frozen_eval.py` 是评测器自己的测试，`_ANSWER_SHEETS`／`_CONTAMINATION_MARKERS` 的断言口径全在里面，i4 已实机读到它 160 行。
2. i4 的 task 逐字要求读 `tests/test_sandbox.py`／`test_security.py` 搭脚手架 ⇒ 这两份及其余 `tests/` 是 i4 的合法工作对象，不能出局。
3. `_prepare_copy` 的删除循环已按 file/dir 分支处理（`victim.unlink(missing_ok=True)`），列一个文件进去即可，零机制改动。

## 裁定

在 `_ANSWER_SHEETS` 增加 `"tests/test_frozen_eval.py"`（文件级，不是 `"tests"`），在 `_CONTAMINATION_MARKERS` 增加 `"tests/test_frozen_eval.py"`；注释同步。测试同步钉住：真副本删掉该文件、其余 `tests/` 保留；污染检测对 `tests/test_frozen_eval.py` 命中、对 `tests/test_sandbox.py` 不误报。

## 非目标与边界

- 不列 `"tests"`（会破坏 i4 题面）。
- 不调整任何冻结题、verify、judge rubric；不改 `src/`。
- 清单是文件级而非目录级，`tests/` 将来若新增含判分口径的测试文件不会自动落网——这是最小切口为保住 i4 题面付出的显式代价，登记在案，不当作盲区。

## 判定标准

1. 真导出副本在 `_prepare_copy` 后 `tests/test_frozen_eval.py` 不存在、而 `tests/test_sandbox.py`／`tests/test_security.py` 仍存在。
2. `_contaminated` 对 `read_file: path=tests/test_frozen_eval.py` 命中；对 `read_file: path=tests/test_sandbox.py` 不误报。
3. 三门全绿（ruff / mypy / pytest）。

## 实现清单

1. `evals/frozen_eval.py`：`_ANSWER_SHEETS` 加 `"tests/test_frozen_eval.py"`，`_CONTAMINATION_MARKERS` 加 `"tests/test_frozen_eval.py"`，注释同步。
2. `tests/test_frozen_eval.py`：真副本测试加「该文件删除、其余 `tests/` 保留」断言；污染检测测试加 dirty/clean 各一条。
3. 回填 [063「实现」⑤](063-eval-harness-answer-sheet-isolation.md) 追加「已裁定并落地」+ [architecture.md](../architecture.md) 活清单「未修」→「已修（最小切口）」。
4. 一个 commit（照 `c163449` 风格 `fix(evals): …（ADR 065）`）。

## 实现（2026-09-29 落地后追加）

### 代码落地

- `evals/frozen_eval.py`：`_ANSWER_SHEETS` 末尾加 `"tests/test_frozen_eval.py"`（文件级，非 `"tests"`），`_CONTAMINATION_MARKERS` 末尾加 `"tests/test_frozen_eval.py"`；docstring :60-65 补 065 与 i4 实机读过两文件的行数；`_prepare_copy` 案卷段注释 :485 与新增清单上方注释同步。
- `tests/test_frozen_eval.py`：
  - `test_contaminated_flags_answer_sheet_reads`：dirty 列表新增 `"read_file: path=tests/test_frozen_eval.py"`，期望值新增 → `["evals/", "docs/decisions", "architecture.md", "tests/test_frozen_eval.py"]`，clean 列表新增 `"read_file: path=tests/test_sandbox.py"` 与 `"read_file: path=tests/test_security.py"`（065：i4 合法工作对象不误报）。
  - `test_real_copy_has_no_git_and_no_answer_sheets`：末尾追加三行——`assert not (wt / "tests" / "test_frozen_eval.py").exists()`，`assert (wt / "tests" / "test_sandbox.py").is_file()`，`assert (wt / "tests" / "test_security.py").is_file()`。
- `docs/decisions/063-eval-harness-answer-sheet-isolation.md`「实现」⑤ 末尾追加一条：`**2026-09-29 用户裁定最小切口并落地（[065](065-eval-tests-answer-sheet-minimal-cut.md)）**`，记录各加一条、真副本断言、污染检测 dirty/clean、三门。
- `docs/architecture.md` :284 活清单「结案同时挖出同型新通道（未修）」→「**（已修，最小切口）**」，补「已修」段，触发信号②改为「下次动这套隔离时按 065 最小切口提案」。

### 三门

- `.venv/bin/ruff check src tests evals`：`All checks passed!`
- `.venv/bin/mypy src`：`Success: no issues found in 59 source files`
- `.venv/bin/python -m pytest`：`707 passed, 2 skipped`（净 +0——改动是扩既有断言不新增测试，污染检测 dirty/clean 各加条目不增测试数）

### commit

`fix(evals): tests/ 最小切口隔离——只列 tests/test_frozen_eval.py（ADR 065）`（5 files changed，93 insertions(+)，7 deletions(-)）
