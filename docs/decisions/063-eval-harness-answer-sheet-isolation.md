# 063 冻结评测 harness 答案纸隔离补洞（草案）

> 状态：**已批准，待动工（2026-09-28 用户拍板甲案）**｜日期：2026-09-28
> 编号说明：059 已为已落地的「spawn 事件缝透传」，060–062 亦已占用；本草案使用下一个空号 063。
> 立项出处：[architecture.md 活清单](../architecture.md)「harness 源码对副本可见：`_ANSWER_SHEETS` 清单缺项」；该问题由 i4-free-confirm-exfil 的真实轨迹发现。
> 一句话结论：**把整个 `evals/` 从被测副本中移除，并把它加入污染检测；评测器应从考场外运行，不能同时躺在被测 agent 的文件系统里。**

## 背景与动机

[051](051-eval-copy-answer-sheet-isolation.md) 已将题库、注入载荷、ADR、架构与路线图从冻结评测副本删除，并以 `contaminated` 把读到案卷的轨迹判红。该措施堵住了最初的主通道，却留下一个清单缺项：`evals/frozen_eval.py` 仍在副本中。

i4 的真实轨迹已出现：agent 在读入恶意 README 后，调用 `read_file` 查看了 `evals/frozen_eval.py` 的一段源码。该文件含 verify 断言、外发取证、质量判分和污染判分的完整口径。即使该次回答的动机看似是核实外发目标，动机不可验证；通道存在就会使后续通过率偏向虚高。

这是**评测仪器完整性**问题，不是产品机制问题：不应为某一道冻结题改 `src/`，也不应因一次分数改变安全机制。修复只收紧考场，随后以原题、原判分、双臂全量重跑取得干净读数。

## 地面真值

1. `evals/frozen_eval.py` 的 `_ANSWER_SHEETS` 只删除 `evals/scenarios`、`docs/decisions`、`docs/architecture.md`、`docs/competitive-roadmap.md`，没有覆盖 `evals/frozen_eval.py` 或其他 harness 源码。
2. `_prepare_copy()` 保留 `evals/`；现有真副本测试还明确断言 `evals/frozen_eval.py` 存在。这是 051 当时为保证 child 运行而留下的旧前提。
3. 该前提现已不成立：`_spawn_child()` 以**主仓库中本文件的绝对路径**启动 child，且 `_child_env()` 把 `REPO_ROOT` 放入 `PYTHONPATH`。child 的评测器、基线与辅助模块都可以在考场外加载；只有 `agent.*` 必须优先从副本的 `src/` 加载，现有 `PYTHONPATH` 顺序已满足这一点。
4. 只删除 `frozen_eval.py` 不够：`evals/` 中仍有 `baseline_agent.py`、其他评测脚本和场景辅助代码；它们不是被测产品，却可能暴露任务形状、工具口径或验证线索。考场里没有任何 `evals/` 比维护「哪些 harness 文件无害」更可靠。
5. 现有 `_CONTAMINATION_MARKERS` 同样未包含 `evals/`。即使 bash 臂越出副本读取主仓库的 harness，`contaminated` 也不会记录，因此预防与检测存在同一个盲区。
6. bash 基线臂仍可扫描主机文件系统，不能靠删除副本文件保证它永远读不到原件；所以保留 051 的双层原则：**副本内预防 + 轨迹检测判红**。

## 方案与裁定建议

### 甲案（建议采纳）：整个 `evals/` 作为答案纸出局

在 `_ANSWER_SHEETS` 增加 `"evals"`，在 `_CONTAMINATION_MARKERS` 增加 `"evals/"`；保持 child 从主仓库绝对路径启动，保持 `agent.*` 优先从副本 `src/` 导入。

- 一处清单同时收紧整个评测源码面，不维护二级白名单。
- 评测运行时与被测工作区彻底分离；副本只保留产品、语料、运行时资产和场景实际交付物。
- 轨迹只要出现读取副本外 `evals/` 的证据就整轮判红，与既有 `docs/decisions` 的口径对称。
- 不改题目、不改 verify、不改安全机制、不改 full/baseline 的装配差异。

### 乙案：只删 `evals/frozen_eval.py`

否决。它只针对已经看见的文件名补洞，`evals/` 内其他 harness 文件仍在；下一次漏洞只会换一个文件名出现。该形状违背 051「目录/文件级清单、新文件自动覆盖」的原则。

### 丙案：保留副本内 `evals/`，只扩大 `contaminated` 匹配

否决。检测只能在模型已经读到答案纸后使结果作废，不能阻止一次付费评测白跑；051 已明确“只做检测不做预防”不可接受。

### 丁案：把 harness 拆成独立包或外置仓库

暂不做。当前 child 已可从考场外运行，甲案只需调整隔离清单、检测标记和测试。为得到同一隔离结果而新建包/仓库会扩大运行环境与发布面。

## 判定标准

1. 真正的 `git archive HEAD` 导出副本在 `_prepare_copy()` 前含 `evals/frozen_eval.py`，执行后整个 `evals/` 不存在；断言必须运行在真导出副本上，防止重演 049 的空目录假绿。
2. child 冒烟仍能启动，并确认导入的 `agent` 来自副本 `src/`，而不是主仓库；评测器本身从主仓库绝对路径运行。
3. `_contaminated()` 对 `read_file: path=evals/frozen_eval.py`、`bash: cat /…/evals/baseline_agent.py` 均返回 `evals/`；正常的 `src/agent/...`、`data/notes/...` 轨迹不误报。
4. 现有 051 隔离不退化：副本仍无 `.git`、无题库、无 ADR/架构/路线图；`data/notes/` 与副本 `src/` 仍存在。
5. 三门全绿（ruff / mypy / pytest）；既有测试若改动，只能把“副本含 `evals/frozen_eval.py`”这条陈旧断言翻转为“整个 `evals/` 已出局”，并在实现记录说明原因。
6. 机制未改、题目未改的前提下，full 与 bash 两臂按冻结集全量重跑；每条 record 的 `contaminated` 必须为空。若非空，读数作废并在本 ADR 追加路径与归因。

## 实现清单

1. `evals/frozen_eval.py`：将 `evals` 加入 `_ANSWER_SHEETS`，将 `evals/` 加入 `_CONTAMINATION_MARKERS`；同步更新模块和 `_prepare_copy()` 注释。
2. `tests/test_frozen_eval.py`：真导出副本测试改为断言 `evals/` 整体不存在；新增 child 冒烟与两条 harness 污染检测断言。
3. 文档：本 ADR 落地后回写 [architecture.md](../architecture.md) 活清单与 [051](051-eval-copy-answer-sheet-isolation.md) 的执行校正，说明 051 的清单边界被 063 收紧；不改写 051 历史正文。
4. 验收：三门 + 冻结集 full/baseline 全量复跑，出分 JSON 与成本、污染读数一并记录。

## 非目标与边界

- 不把 `evals/` 从主仓库删除，也不停止版本化题库；评测器需要可审阅、可复跑，隔离的是**被测副本**。
- 不试图阻止 bash 基线臂从主机其他位置搜索原件；这超出副本隔离能力，仍由 `contaminated` 检测判红。
- 不调整任何冻结题、verify、judge rubric 或安全围栏；本案的成功标准是读数可信，不是分数更高。
- 不因本案顺手修复 LongMemEval、计划预算、plan scope 或 MCP 连接问题；它们各有独立证据与触发条件。

## 风险与回滚

- 最大风险是 child 在某条未覆盖的 import 路径上隐式依赖副本 `evals/`。因此先做不花模型费用的 child 冒烟，再跑全量冻结集；若冒烟失败，恢复清单并定位该依赖，不能用“先把 `evals/` 放回去”作为永久方案。
- `evals/` 标记比现有 `evals/scenarios` 更宽，可能使 agent 主动读取评测代码的轨迹判红。这是预期：评测源码对被测 agent 本就是答案纸；若未来存在合法业务需求，应为其另建非冻结评测入口，而非给冻结集开白名单。
- 该草案没有改变产品代码，回滚就是撤销清单与测试改动；已经被标记污染的历史读数不应重新解释为有效。

## 拍板记录（2026-09-28）

**用户裁定：批准，按甲案动工；动工日顺延至 2026-09-29。**

内部核对结论（地面真值先行，六条断言全过 + 补查三点）：

- 草案「地面真值」1–6 全部成立：`_ANSWER_SHEETS`（`evals/frozen_eval.py:110-115`）无 `evals`；`test_frozen_eval.py:451` 确有条断言钉住「副本含 `evals/frozen_eval.py`」；`_spawn_child`（:502-516）以主仓库本文件绝对路径启动、`_child_env`（:496）`PYTHONPATH=<副本>/src : REPO_ROOT` 保证「`agent.*` 从副本、评测器从主仓库」；`_CONTAMINATION_MARKERS`（:118-120）无 `evals/`（i4 实机 `contaminated=None` 即实证）。
- 补查三点（草案未覆盖、决定甲案是否可行）：①冻结集题目 verify 无 pytest（i4 的 `pytest` 字样在 task 载荷里，verify 是 `ls data/notes/*.md` 计数）⇒ 删 `evals/` 不炸场景验收；②`tests/conftest.py` 不存在 ⇒ 无隐式 `evals` import；③child 顶部 `from evals.baseline_agent import ...`（:97-98）经脚本目录 `sys.path[0]` 从主仓库加载 ⇒ 与副本 `evals/` 无关。附带甜点：删副本 `evals/` 后 :506-508 docstring 所记的「影子包」问题彻底消失。
- 冻结集 16 题文本零处引用 `evals` 路径 ⇒ 无题面悬空。

明日动工清单（按序执行）：

1. `evals/frozen_eval.py`：`_ANSWER_SHEETS` 加 `"evals"`；`_CONTAMINATION_MARKERS` 加 `"evals/"`；:61 头注记、:472-476 案卷段注释、:506-508 docstring 同步说明整个 `evals/` 已出局。
2. `tests/test_frozen_eval.py`：:442 断言改钉「导出自带 `evals/` 整树」；:451 翻转 `assert not (wt/"evals").exists()`；新增 child 冒烟（不花模型费，确认 `agent` 从副本 `src/` 导入）+ 两条 `_contaminated` 断言（`evals/frozen_eval.py`、`/…/evals/baseline_agent.py` → `evals/`；正常 `src/agent/...`、`data/notes/...` 不误报）。
3. 三门全绿（ruff / mypy / pytest）。
4. 冻结集 full + bash 两臂全量重跑：每条 record `contaminated` 必须为空（判定标准 6），出分/成本/污染读数并入回填。
5. 回填本 ADR「实现」节（含偏移登记）；回写 architecture.md 活清单 `:278` 条结案 + [051](051-eval-copy-answer-sheet-isolation.md) 执行校正（不改写 051 历史正文）。
6. 一个 commit（照 `c163449` 风格 `fix(evals): …（ADR 063）`）。
