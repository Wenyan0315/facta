# Facta 2.0 P0 Spec：产物保留与安全合入

> 状态=待评审 · 2026-11-28 · 依赖：[Spec 1 任务工作区绑定](p0-spec-workspace-binding.md) · 关联：[研发计划](m5-task-delivery-plan.md) · [外部评审](m5-external-review.md) · [ADR 114](decisions/114-facta-2-roadmap.md) · [ADR 117](decisions/117-m3-multitask-and-authorization.md)

**一句话结论：** 产物识别必须覆盖「已提交未合入」，合入必须先核前置、失败可抢救，处置必须幂等——这是默认 worktree 对外发布的第二道验收门槛，与 Spec 1 的绑定三元组（`repo_root`/`target_branch`/`baseline`，基线提交）配套生效。

## 一、现状

### 已落地能力

- worktree 原语齐备：创建（`agent/<uuid>` 分支隔离）、沙箱内工具重锚、改动摘要、合回、丢弃、启动回收（[worktree.py](../src/facta/tools/worktree.py)）。合回链路是 worktree 内 `add -A` + commit → Facta 主仓 `merge --no-edit` → 删分支清 worktree（[commit_and_merge_back](../src/facta/tools/worktree.py#L120)）。
- 任务沙箱开箱/收口已接线：worker 退出时无改动即清理（[_task_sandbox_closeout](../src/facta/server/app.py#L758)）；GET/POST `/api/tasks/{task_id}/worktree` 提供改动摘要与 merge/discard 收口，queued/running 状态返回 409（[app.py](../src/facta/server/app.py#L1513)）。
- 任务卡展示沙箱状态与收口按钮（[TaskCard.jsx](../frontend/src/tasks/TaskCard.jsx)）。
- 工具层「错误也返回字符串」的反馈环惯例成立，模型与用户可自纠。

### 已复现问题

- **已提交产物被丢弃（P0-2 根因）。** [worktree_changes](../src/facta/tools/worktree.py#L108) 只跑 `git status --short` + `diff HEAD --stat`，看不见「已提交但未合入」的改动；worktree 比基线多一个提交且工作区干净时改动检查返回空，收口走 [discard_worktree](../src/facta/tools/worktree.py#L158)（`remove --force` + `branch -D`），提交直接丢失（[外部评审](m5-external-review.md#保留已提交产物并隔离合并冲突)）。
- **合入失败污染主树。** merge 失败时 worktree 仍被清理（[worktree.py](../src/facta/tools/worktree.py#L148)），目标分支有冲突时主树保留冲突与 `MERGE_HEAD` 半合并态，候选只剩分支可手工抢救。
- **验证与候选脱钩、状态单一。** 测试/检查结果不关联候选版本，任务卡只有单一任务状态，运行结果、检查结论、合入与否混在一起。

## 二、行为契约

以下条款为规范性约定，用语「必须/不得/当…时」。

### 2.1 产物四段检查

`worktree_changes` 必须升级为四段检查，任一段非空即「有产物」：

1. **已提交未合入**：`git log --oneline baseline..HEAD` + `git diff baseline...HEAD --stat`；
2. **已暂存**；3. **未暂存**；4. **未跟踪**（由 `git status --porcelain` 解析）。

产物识别基线 = [Spec 1](p0-spec-workspace-binding.md) 绑定的 `baseline`（基线提交：创建时目标分支 HEAD 全 SHA，钉死不跟随）。无绑定的旧任务回落 `merge-base(目标分支头, HEAD)` 推断基线，并在返回中标注「基线为推断值」。「已提交但工作区干净」不得判为无产物。

### 2.2 按需 commit

仅当 `git status --porcelain` 非空时才 `add -A` + commit（防止 nothing-to-commit 失败）；已提交产物直接参与合入，不补空 commit。空产物（四段全空）才允许按无改动清理。

### 2.3 安全合入

- 合入目标 = Spec 1 绑定的 `target_branch`，在绑定 `repo_root` 上执行。
- **前置检查**：目标工作区干净（`status --porcelain` 为空）且不在合并中（无 `MERGE_HEAD`），否则明确失败、不动目标。
- merge 失败时用 `git rev-parse --verify MERGE_HEAD` 判定：
  - **是冲突**：`git merge --abort` 恢复目标干净，worktree 与候选分支都保留（可手工/重试抢救），返回 conflict 态 + 冲突文件清单（`diff --name-only --diff-filter=U`）；
  - **非冲突**（如 index.lock、超时）：返回 failed 态如实报错，候选保留。
- 合入成功：记录合入 commit，之后才删分支、清 worktree。
- 内部实现返回四态元组 `(status, detail)`：merged / empty / conflict / failed；工具面对模型/用户仍拼字符串，「错误也返回字符串」惯例不变。

### 2.4 验证关联

每项验证（测试/检查）必须记录：命令、结果（passed / failed / skipped / not_run）、执行时间、**对应候选版本 SHA**。候选变化（新 commit 或新工作区改动）→ 预览失效，必须重新核验后才能合入。运行失败或未执行的验证如实展示，不装绿。

### 2.5 任务卡三态分列

任务卡必须把三件事分别可见：**运行状态**（任务状态机）、**检查结果**（如「3 通过 / 1 失败 / 1 未覆盖」+ 明细，含 not_run）、**合入状态**（未合入 / 已合入 `<sha>` / 冲突 / 已丢弃）。可查看完整 diff。

### 2.6 幂等与重启恢复

合入/丢弃是一次「处置」（disposition），必须落盘：含候选 SHA、动作、结果。合入与丢弃互斥，先到先得。同一处置重复请求只应用一次：

- 已合入 → 返回原结果，不重复 merge；
- 候选已变 → 409 要求重新预览；
- 重复点击、重复 API 调用、重启后重发都归同一处置。

### 2.7 清理可重试

合入成功后才清理 worktree + 分支；清理失败 → 单独报「清理失败」态、可单独重试，产物已安全合入不受影响。

### 2.8 增强项（增强不阻塞 P0）

- 合入前在临时工作区 / `git merge-tree` 试合预演。真冲突路径已被「abort + 候选保留」覆盖，预演是体验优化。
- 合并前任务卡展示完整 diff + 测试结果汇总。

## 三、状态与数据变更

Task 新增字段（与 [Spec 1](p0-spec-workspace-binding.md) 的 `target_branch`/`baseline`/`worktree_branch` 衔接）：

| 字段 | 含义 |
|---|---|
| `candidate_sha`（或等价产物引用） | 当前候选版本 SHA，验证与处置的锚点 |
| `verifications` | 验证结果列表（命令、结果、执行时间、对应候选 SHA） |
| `merge_status` | none / merged / conflicted / discarded |
| `merge_commit` | 合入 commit SHA（merged 时） |
| `disposition` | 处置记录（候选 SHA、动作、结果），幂等依据 |

API 兼容形态：

- GET `/api/tasks/{id}/worktree` 返回四段结构，**保留 `changes` 字符串键兼容旧前端**（拼接四段摘要）。
- POST `/api/tasks/{id}/worktree`（settle）返回增加 `status`/`detail` 字段，**保留 `result`/`cleared` 键**（`result` 由 `(status, detail)` 拼出，语义不变）。
- 处置结果事件进任务事件流，复用现有 SSE，不新造推送通道。

## 四、兼容规则

- 无绑定的旧任务：基线回落 `merge-base(目标分支头, HEAD)`，响应中标注「基线为推断值」；合入目标按现行为执行并在界面说明。
- 存量任务读取兼容：新增字段缺省安全（`merge_status=none`、`verifications` 空），旧快照可查。
- 旧前端只读 `changes`/`result`/`cleared` 键时行为不回退；新字段按需读取。
- 工具层「错误也返回字符串」惯例不变；四态元组是内部契约，不外泄为新错误协议。
- 现有 queued/running 收口 409 语义保留。

## 五、失败处理

| 失败场景 | 表现 | 处置 |
|---|---|---|
| 目标工作区脏 | 合入前置失败，明确报错（列出脏文件） | 不动目标，候选保留，用户清理后重试 |
| 目标处于合并中（有 `MERGE_HEAD`） | 合入前置失败，明确报错 | 不动目标，候选保留 |
| merge 冲突 | conflict 态 + 冲突文件清单 | `merge --abort` 恢复目标干净；worktree 与候选分支保留，可手工或重试 |
| merge 非冲突失败（index.lock、超时等） | failed 态，错误串如实返回 | 候选保留可重试；目标未被改动 |
| 候选与预览不一致（新 commit 或新改动） | 409，预览失效 | 要求重新核验后再合入 |
| 合入成功后清理失败 | 「清理失败」态单独报 | 单独重试清理；产物已合入不受影响 |
| worktree/分支已不在（手工清理） | 如实报无产物/无法处置 | 不虚构结果，字段清空防假指针 |
| git 命令失败/超时 | 错误串如实返回 | 保守保留现场（宁可多留不可误删） |
| 收口时任务仍在跑 | 409（现状保留） | 等任务停后再收口 |

## 六、测试矩阵

临时 Git 仓库 + 脚本模型跑成可重复回归。测试落点：新增 `tests/test_artifact_merge.py`，复用 [tests/test_worktree.py](../tests/test_worktree.py) 的 tmp git 仓库 fixture 惯例（真 git、monkeypatch 模块根）。

| 编号 | 场景 | 通过条件 | 测试落点 |
|---|---|---|---|
| M1 | 已提交且工作区干净 | 四段①识别出产物，收口不丢提交，合入后产物在目标分支 | tests/test_artifact_merge.py |
| M2 | 已暂存改动 | ②段识别非空，收口合入含该改动 | tests/test_artifact_merge.py |
| M3 | 未暂存改动 | ③段识别非空，收口合入含该改动 | tests/test_artifact_merge.py |
| M4 | 未跟踪文件 | ④段识别非空，收口合入含该文件 | tests/test_artifact_merge.py |
| M5 | merge 冲突 | 主树无 `MERGE_HEAD` 且干净，候选分支保留，可重试合入 | tests/test_artifact_merge.py |
| M6 | 目标工作区脏 | 前置拒绝，目标状态零变化 | tests/test_artifact_merge.py |
| M7 | 目标合并中（有 `MERGE_HEAD`） | 前置拒绝，目标状态零变化 | tests/test_artifact_merge.py |
| M8 | 重复合入 | 只应用一次，第二次返回原结果 | tests/test_artifact_merge.py |
| M9 | 候选变化后合入 | 预览失效 409，要求重新核验 | tests/test_artifact_merge.py |
| M10 | 检查失败/未运行 | 面板如实显示 failed/not_run，不装绿 | tests/test_artifact_merge.py |
| M11 | 合入成功后清理失败 | 报「清理失败」态，单独重试成功，产物不受影响 | tests/test_artifact_merge.py |
| M12 | 重启后重发处置 | 恢复同一处置，不重复 merge/误丢弃 | tests/test_artifact_merge.py |
| M13 | 无绑定旧任务 | 基线按 merge-base 推断并标注「基线为推断值」 | tests/test_artifact_merge.py |

## 七、实施顺序

每步一个 PR 能收口：

1. **产物四段检查**：`worktree_changes` 四段化 + 无绑定回落推断基线。依赖 Spec 1「绑定三元组落 Task 字段」。
2. **合入四态化**：按需 commit、合入前置检查、conflict 判定 + `merge --abort`、内部 `(status, detail)` 四态元组、成功后才清理。依赖 Spec 1「worktree 原语锚定 repo_root/target_branch」+ 步骤 1。
3. **settle 端点与幂等处置**：处置记录落盘、重复请求归并、候选变化 409、SSE 事件。依赖步骤 2。
4. **验证关联**：验证记录钉候选 SHA、候选变化预览失效。依赖步骤 1 的候选 SHA 定义；可与步骤 3 并行。
5. **任务卡三态分列**：运行状态/检查结果/合入状态 + 完整 diff 查看 + 兼容键。依赖步骤 2–4 的 API 字段。
6. **增强项**：`git merge-tree` 试合预演、合并前 diff + 测试结果汇总。**增强不阻塞 P0**，可延后。

开发拆单文档（[p0-dev-breakdown.md](p0-dev-breakdown.md)，另人撰写）会细化本节。

## 八、设计分歧与推荐

1. **产物识别基线：绑定 baseline vs 目标分支头 → 推荐绑定 baseline。** 预览可复现：同一候选在任何时间识别出同一堆产物；目标前进是显式事件（Spec 1 标注「目标已前进」），对比目标头会让识别结果随目标漂移。
2. **合入方式：rebase / ff-only / git merge 默认 → 推荐维持 `git merge` 默认。** ff 可行则 ff，否则 merge commit；冲突 abort 保树。最小变更、历史可溯（merge commit 记录合入事件）；rebase 改写候选历史且冲突处理更险，ff-only 在目标前进时大面积无谓失败。
3. **合并前试合预演：P0 必做 vs 增强项 → 推荐增强不阻塞。** 真冲突路径已被「abort + 候选保留」兜住（不丢产物、主树干净），预演只是把冲突提前展示，属体验优化。
4. **幂等键：处置 ID vs 候选 SHA 对比 → 推荐双检。** 处置 ID 归档重复请求（重复点击/重复 API/重启重发归同一处置），候选 SHA 对比防跨版本误合（候选已变时 409）；单用其一分别漏掉重复请求归并或版本校验。
