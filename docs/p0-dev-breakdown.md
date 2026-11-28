# Facta 2.0 P0 开发拆单

> 状态=待评审 · 2026-11-28 · 依据：[Spec 1 任务工作区绑定](p0-spec-workspace-binding.md)、[Spec 2 产物保留与安全合入](p0-spec-artifact-merge.md)（同轮并行撰写，本文只拆任务、不复述 spec 内容）与 [M5 落地计划的开工顺序](m5-task-delivery-plan.md#开工顺序和发布范围)。贯穿验收场景：**用户选定自己的 Git 仓库，委派修复一个失败测试，从任务卡核验 diff 和测试结果，最后明确批准合入。** 测试编号 B*/M* 与两 spec 测试矩阵同编号对位；E* 为本文在第五节补的集成测试。

## 一、拆单总览

| ID | 名称 | 规格归属 | 依赖 | 涉及文件 | 测试落点 | PR 粒度 |
|---|---|---|---|---|---|---|
| S1-1 | 绑定三元组解析与校验 | Spec 1 | — | [task.py](../src/facta/orchestrator/task.py#L85)、[app.py](../src/facta/server/app.py) 创建入口 | test_workspace_binding.py B1-B3、B12 | 独立 PR |
| S1-2 | worktree 原语绑定化 | Spec 1 | S1-1 | [worktree.py](../src/facta/tools/worktree.py#L43) | B4（原语层） | 独立 PR |
| S1-3 | 任务沙箱开箱/收口接绑定 | Spec 1 | S1-1→S1-2 | [app.py](../src/facta/server/app.py#L730) | B4、B5（父侧） | 独立 PR |
| S1-4 | 父子继承 + fan-out 只读拦截 | Spec 1 | S1-2、S1-3 | [spawn.py](../src/facta/tools/spawn.py#L77)、[worktree.py](../src/facta/tools/worktree.py#L82) | B5（子侧）、B6-B8 | 独立 PR |
| S1-5 | 恢复路径与旧任务兼容 | Spec 1 | S1-2 | [app.py](../src/facta/server/app.py#L1521)、[task.py](../src/facta/orchestrator/task.py#L178) | B9-B11、B13 | 独立 PR |
| S1-6 | 任务卡工作区/目标分支/基线/执行方式 | Spec 1 | S1-1、S1-3 | [TaskCard.jsx](../frontend/src/tasks/TaskCard.jsx)、[App.jsx](../frontend/src/tasks/App.jsx) | 视图断言并入 B2/B6 + 前端构建 | 可与 S1-3/S1-5 合 PR |
| S2-1 | 产物四段检查 | Spec 2 | S1-1（merge-base 回落可先行） | [worktree.py](../src/facta/tools/worktree.py#L108)、[app.py](../src/facta/server/app.py#L758) | test_artifact_merge.py M1-M4、M13 | 独立 PR |
| S2-2 | 合入四态化 | Spec 2 | S2-1、S1-2 | [worktree.py](../src/facta/tools/worktree.py#L120) | M5-M7 | 独立 PR |
| S2-3 | settle 结构化 + 处置幂等 + 重启恢复 | Spec 2 | S2-2 | [app.py](../src/facta/server/app.py#L1513) | M8、M11、M12 + E3 | 独立 PR |
| S2-4 | 验证证据绑定候选版本 | Spec 2 | S2-1 | [app.py](../src/facta/server/app.py#L808)、[task.py](../src/facta/orchestrator/task.py#L146) | M9、M10 | 独立 PR |
| S2-5 | 任务卡三态分列 + diff 查看 | Spec 2 | S2-3、S2-4 | [TaskCard.jsx](../frontend/src/tasks/TaskCard.jsx) | M10 面板断言 + 前端构建 | 可与 S2-3 合 PR |
| S2-6 | 增强项：merge-tree 试合、合并前完整 diff+测试展示 | Spec 2（不阻塞） | S2-2 | [worktree.py](../src/facta/tools/worktree.py)、settle 预览 | 2.8 增强项（抽样比对，不阻塞） | 独立 PR，随尾 |

**PR 粒度说明。** 每条任务一个 PR 可收口：改动点、验收判据、测试编号一一对应，单测全绿即合。唯二例外：S1-6（纯前端呈现，随后端字段走）与 S2-5（纯前端呈现，随 S2-3 结构化响应走）可与相邻后端任务合 PR。依赖链：S1-1→S1-2→S1-3；S1-4 依赖 S1-2/S1-3；S1-5 依赖 S1-2；S2-1 依赖 S1-1 的基线契约（merge-base 回落可先行）；S2-2 依赖 S2-1+S1-2；S2-3 依赖 S2-2；S2-4 依赖 S2-1；S2-5 依赖 S2-3/S2-4；S2-6 不阻塞任何任务。

## 二、Spec 1 拆单

### S1-1 绑定三元组解析与校验

- **目标。** 任务创建时把 repo_root / target_branch / baseline（基线提交）三元组解析校验并钉死为不可变绑定，非 Git 路径拒绝建任务。
- **改动点。**
  - [Task](../src/facta/orchestrator/task.py#L85) 新增 `target_branch`、`baseline`、`worktree_branch` 三个字段（缺省空串=旧任务兼容）；[to_dict](../src/facta/orchestrator/task.py#L146) 显式挑字段序列化补三项；[TaskStore](../src/facta/orchestrator/task.py#L178) 落盘随 data/tasks/<id>.json 持久。
  - 任务创建入口（[app.py](../src/facta/server/app.py) 任务 POST 端点）：校验 repo_root 是 Git 仓库（`git rev-parse --is-inside-work-tree` 级别校验），非 Git 路径返回明确错误；target_branch 缺省取仓库当前分支，baseline 取 `rev-parse` 当时 HEAD 全 SHA，创建后不再变。
- **验收判据。** ① 非 Git 路径建任务被拒且原因明确；② 合法仓库建任务后三字段落盘，目标分支之后前进不影响任务基线（钉死语义）；③ 任务响应含三字段，旧任务 JSON 无字段时读取为缺省值不崩。
- **测试。** B1-B3、B12。

### S1-2 worktree 原语绑定化

- **目标。** worktree 全部 git 操作锚定绑定 repo_root（目录仍住 Facta 仓 data/worktrees，分支/提交进用户仓库），分支名以 Task.worktree_branch 为真值源，废除目录名反推。
- **改动点。** 全在 [worktree.py](../src/facta/tools/worktree.py)：
  - [`_git`](../src/facta/tools/worktree.py#L43)：默认锚从 Facta 主仓改为绑定 repo_root（各函数显式传入，保留函数体内解析名字的 monkeypatch 友好纪律）。
  - [`create_worktree`](../src/facta/tools/worktree.py#L67)：签名带绑定三元组，从绑定 repo_root 的 baseline 切出 worktree_branch（生成规则仍 agent/<uuid8>，但分支名返回给调用方存进 Task.worktree_branch，不由 `wt.name` 反推）。
  - [`commit_and_merge_back`](../src/facta/tools/worktree.py#L120) / [`discard_worktree`](../src/facta/tools/worktree.py#L158) / [`cleanup_stale_worktrees`](../src/facta/tools/worktree.py#L167)：分支名从参数/字段取（废除 `f"agent/{wt.name}"` 反推），merge/discard/branch 操作在绑定 repo_root 上执行，合入目标为绑定 target_branch。
  - [`registry_in_worktree`](../src/facta/tools/worktree.py#L82) 保持共享哲学不变（file/terminal 五件重锚沙箱目录）。
- **验收判据。** ① 绑定仓库 B 的任务，其 `agent/*` 分支与提交出现在仓库 B 的 refs（`git -C B` 可查），Facta 主仓 refs 零污染；② worktree 目录改名后 merge/discard 仍按字段分支名生效（反推已废）；③ 合入落点是绑定 target_branch，不是 Facta 主仓当前分支。
- **测试。** B4（原语层）。

### S1-3 任务沙箱开箱/收口接绑定

- **目标。** 任务生命周期的沙箱开箱、收口与 worker 全部消费绑定三元组；skill 任务不套沙箱但如实登记执行方式。
- **改动点。**
  - [`_ensure_task_worktree`](../src/facta/server/app.py#L730)：create_worktree 传绑定参数；worktree 目录缺失的分支移交 S1-5 恢复判定钩子；建失败落 blocked 的现有语义不变。
  - [`_task_sandbox_closeout`](../src/facta/server/app.py#L758)：收口判定接绑定（分支名/目标分支取自字段）；空沙箱判定升级为四段检查属 S2-1，本任务只接线不断言判定内容。
  - [`_task_worker`](../src/facta/server/app.py#L808)：沙箱 registry（registry_in_worktree）锚绑定 worktree；skill 路径不套沙箱（现状 `(None, True)`），并在任务视图登记「执行方式」供 S1-6 呈现。
- **验收判据。** ① chat 任务沙箱目录在 data/worktrees 但 git 元数据归属绑定仓库；② skill 任务无 worktree 字段、任务视图标记真实执行方式（未隔离）；③ 沙箱创建失败仍落 blocked + blocked_reason（回归不破）。
- **测试。** B4、B5（父侧）。

### S1-4 父子继承 + fan-out 只读拦截

- **目标。** 子 Agent 沙箱继承父任务工作区绑定；fan-out 默认只读，显式请求写工具的子任务不执行、聚合结果标「已拦截」。
- **改动点。**
  - [spawn.py](../src/facta/tools/spawn.py)：新增 `_WRITE_TOOLS = {"write_file", "run_command"}` 显式排除清单；[工具子集计算](../src/facta/tools/spawn.py#L225)（现 `全量 - _FORBIDDEN`）在 fan-out 路径再减 `_WRITE_TOOLS`。
  - [`fan_out_subagents`](../src/facta/tools/spawn.py#L277)：子任务默认工具集只读；显式 tools 请求含写工具的子任务不进入执行，聚合行标「已拦截」，其余子任务照常并行聚合（MAX_FANOUT=8 上限不变）。
  - 父子 registry 继承任务沙箱：spawn 的 worktree 模式与子沙箱一并锚父任务绑定 repo_root（[registry_in_worktree](../src/facta/tools/worktree.py#L82) 继承链，含 scope_check 透传）。
- **验收判据。** ① fan-out 默认子任务工具集不含 write_file/run_command；② 显式请求写工具的子任务零执行、聚合结果该行标「已拦截」、兄弟子任务不受影响；③ 子沙箱改动只落父任务绑定仓库的工作区。
- **测试。** B5（子侧）、B6、B7、B8。

### S1-5 恢复路径与旧任务兼容

- **目标。** 缺失 worktree 但分支在时给恢复选项（重建挂回/直接合入/丢弃）；绑定失效与目标已前进如实呈现；旧任务读取兼容、不补造绑定。
- **改动点。**
  - [settle 端点与恢复入口](../src/facta/server/app.py#L1521)：worktree 目录缺失→按 Task.worktree_branch 在绑定 repo_root 找分支，返回三选项；分支也不存在→明确失败原因；绑定失效（repo_root 不再是 Git 仓、分支被删）→「绑定失效」状态+原因；目标已前进（baseline 与 target_branch HEAD 不同）→ 呈现前进差异提示。
  - [TaskStore 读取](../src/facta/orchestrator/task.py#L178)：旧任务无三字段按缺省读取，不伪造绑定；合入回落 merge-base 的契约由 S2-1/S2-2 消费（本任务保证回落路径可达）。
- **验收判据。** ① 手工删 worktree 目录后恢复入口给三选项，重建挂回可续收口、直接合入走合入路径、丢弃零残留；② 旧任务 JSON 可读、面板不崩、响应无伪造绑定字段；③ 目标分支前进时呈现差异而非静默按旧基线合入。
- **测试。** B9-B11、B13。

### S1-6 任务卡显示真实工作区/目标分支/基线/执行方式

- **目标。** 任务卡如实显示实际工作区、目标分支、基线、执行方式（沙箱隔离 / skill 直接执行）。
- **改动点。**
  - [TaskCard.jsx](../frontend/src/tasks/TaskCard.jsx)：新增绑定信息区块（repo_root、target_branch、baseline 短 SHA、worktree_branch）与执行方式徽标（由 skill 字段派生：空=沙箱隔离，非空=未套沙箱直接执行）。
  - [App.jsx](../frontend/src/tasks/App.jsx)：透传新字段；后端供给已由 S1-1/S1-3 落定，本任务纯前端呈现。
- **验收判据。** ① 任务卡显示绑定仓库路径、目标分支、基线短 SHA、执行方式；② skill 任务显示「未套沙箱/直接在绑定仓库执行」；③ 旧任务字段缺省显示「未绑定（旧任务）」，无空指针。
- **测试。** 视图断言并入 B2/B6 + 前端构建校验。

## 三、Spec 2 拆单

### S2-1 产物四段检查

- **目标。** 以基线四段识别完整产物（已提交未合入 / 暂存 / 未暂存 / 未跟踪，任一非空=有产物），修正空沙箱判定——已提交产物不再被误清。
- **改动点。**
  - [`worktree_changes`](../src/facta/tools/worktree.py#L108) 升级为四段：baseline..HEAD 已提交未合入 + `diff --cached` 暂存 + `diff` 未暂存 + `ls-files --others` 未跟踪；旧任务无 baseline 回落 `merge-base(target_branch, HEAD)`；展示串沿用「错误也返回字符串」纪律，内部四段结构化供 S2-2/S2-5 复用。
  - [`_task_sandbox_closeout`](../src/facta/server/app.py#L758)：空沙箱判定改用四段结果——仅已提交产物的任务不再被判空清理；GET worktree 端点（[L1513](../src/facta/server/app.py#L1513)）展示同源四段。
- **验收判据。** ① 仅已提交未合入（工作区干净）判「有产物」、沙箱保留；② 暂存/未暂存/未跟踪任一非空判「有产物」；③ 真·无任何改动的空沙箱照旧清理（回归）；④ 旧任务走 merge-base 回落仍识别产物。
- **测试。** M1-M4、M13。

### S2-2 合入四态化

- **目标。** commit_and_merge_back 按需 commit、合入前置检查、冲突 abort 保主树，返回四态 merged/empty/conflict/failed。
- **改动点。** 全在 [`commit_and_merge_back`](../src/facta/tools/worktree.py#L120)：
  - 仅 `status --porcelain` 非空时 add+commit（已提交产物不重复 commit、不产空提交）。
  - 合入前置检查：目标工作区干净、无 MERGE_HEAD；不满足直接返回 failed + 原因，零改动发生。
  - merge 失败 → `rev-parse --verify MERGE_HEAD` 判冲突 → `merge --abort` 保主树干净、候选分支保留可重试。
  - 返回四态 merged/empty/conflict/failed：内部 (status, detail) 结构，工具面字符串不变（spawn 反馈环纪律不破）；merge 目标=绑定 target_branch（S1-2 供给）。
- **验收判据。** ① merged：目标分支含候选提交、worktree 清理、分支删；② empty：四段无产物时返回 empty，不 commit 不 merge，按无改动清理；③ conflict：主树干净无 MERGE_HEAD 残留、候选分支仍在可重试；④ failed：错误串含原因；目标工作区脏/合并中被前置拒绝且目标零变化；⑤ 已提交产物合入不产生多余空 commit。
- **测试。** M5-M7（合入含改动的断言在 M1-M4）。

### S2-3 settle 端点结构化 + 处置幂等 + 重启恢复

- **目标。** settle 端点返回结构化四态；处置幂等落盘，重复请求同处置只应用一次，重启恢复同一处置。
- **改动点。**
  - [`settle_task_worktree`](../src/facta/server/app.py#L1521)：响应结构化（status/detail/cleared + 四段产物），消化 S2-2 的 (status, detail)；处置（merge/discard/恢复选项裁决）落 disposition 并随任务持久化（[TaskStore](../src/facta/orchestrator/task.py#L178) 落盘路径）；同 action 重复请求返回原处置结果不重复应用；重启后按已落盘 disposition 续做/不重复合入。
  - 合入成功后清理、清理失败标「清理失败」并提供单独重试入口（清理是独立动作，不回滚合入结果）。
  - GET worktree（[L1513](../src/facta/server/app.py#L1513)）同步结构化（四段 + 处置状态）；响应字段向后兼容，旧前端调用不破。
- **验收判据。** ① 连点两次合入只发生一次 merge（幂等）；② 重启后重放同一请求返回原处置、不重复合入/不误丢弃；③ 合入成功但清理失败→状态「清理失败」、单独重试可成功；④ 旧客户端读响应不报错。
- **测试。** M8、M11、M12 + E3。

### S2-4 验证证据与候选版本关联

- **目标。** 验证结果逐检查绑定候选 SHA；候选一变预览即失效，需重新核验。
- **改动点。**
  - 验证记录登记（[`_task_worker`](../src/facta/server/app.py#L808) 轮次收口 / [to_dict](../src/facta/orchestrator/task.py#L146) 呈现）：每条检查记录检查名、结果（通过/失败/未运行）、候选 SHA（任务沙箱当次 HEAD）。
  - settle/预览比对当前候选 SHA 与验证记录 SHA，不一致→「预览失效，需重新核验」；检查失败、未运行如实展示，汇总标失败/跳过/未覆盖项。
- **验收判据。** ① 验证后候选再变（新 commit/新改动）→预览标失效并要求重新核验；② 每条验证结果可定位检查、结果、候选 SHA 三元；③ 失败/未运行项如实标注不粉饰。
- **测试。** M9、M10。

### S2-5 任务卡三态分列 + diff 查看

- **目标。** 任务卡运行状态/检查结果/合入状态三态分列，可查看 diff。
- **改动点。**
  - [TaskCard.jsx](../frontend/src/tasks/TaskCard.jsx)：三态分列——运行状态沿用现有 STATUS_ZH 徽标；检查结果列消费 S2-4 验证记录；合入状态列消费 S2-3 四态+处置（含清理失败重试按钮）。
  - diff 查看：消费 GET worktree 结构化四段，文件级 diffstat + 可展开内容；预览失效时按钮态提示重新核验。
- **验收判据。** ① 三态独立显示不混淆（运行完成 ≠ 已合入）；② diff 可展开逐文件查看；③ 预览失效时合入按钮给出重新核验提示而非可点。
- **测试。** M10 面板断言 + 前端构建校验。

### S2-6 增强项（不阻塞）

- **目标。** merge-tree 试合预判冲突、合并前完整 diff+测试结果展示。
- **改动点。** [worktree.py](../src/facta/tools/worktree.py) 增 `git merge-tree` 试合（纯预判不落地）；settle 预览端点补完整 diff + 最近一次测试结果摘要。
- **验收判据。** merge-tree 预判与实际 merge 结论一致（抽样比对）；预览含完整 diff + 测试摘要。**不阻塞 P0 发布门槛**，随尾合入。
- **测试。** 无矩阵编号（2.8 增强项，抽样比对断言），不阻塞。

## 四、验收场景走查

端到端场景：**用户选定自己的 Git 仓库，委派修复一个失败测试，从任务卡核验 diff 和测试结果，最后明确批准合入。** 评审人可逐步核对拆单完整性。

| 步 | 用户动作 | 系统行为 | 支撑任务 | 验证方式 |
|---|---|---|---|---|
| 1 | 选定自己的 Git 仓库与目标分支，发起任务 | 解析校验绑定三元组：repo_root 是 Git 仓、target_branch/baseline 创建时钉死；非 Git 路径拒绝 | S1-1 | B1、B2；E1 步 1 |
| 2 | 委派「修复一个失败测试」 | Task 落绑定字段；沙箱开箱：目录在 data/worktrees、分支从绑定 repo_root 基线切出、worktree_branch 字段登记 | S1-1、S1-2、S1-3 | B4、B5（父侧）；E1 步 2 |
| 3 | （Agent 自主执行）跑失败测试、改代码；可能 fan-out 检索/验证 | 文件/终端工具锚沙箱（继承绑定）；fan-out 默认只读，显式请求写工具的子任务不执行、聚合标「已拦截」 | S1-2、S1-4 | B6-B8；E1 步 3 |
| 4 | 任务结束，等待核验 | 沙箱收口按四段检查判产物：修复已提交也判「有产物」保留待处置；真·空沙箱才清理 | S2-1 | M1-M4、M13；E1 步 4 |
| 5 | 打开任务卡核验 diff 与测试结果 | 显示实际工作区/目标分支/基线/执行方式；运行状态/检查结果/合入状态三态分列；diff 可展开；测试结果逐检查绑定候选 SHA | S1-6、S2-1、S2-4、S2-5 | M9、M10 + B2/B6 视图断言；E1 步 5 |
| 6 | （异常分支）核验前候选又改动 / 目标分支前进 | 预览标「已失效，需重新核验」；目标前进如实呈现差异，不静默按旧基线合入 | S2-4、S1-5 | M9、B11 |
| 7 | 明确批准合入 | 前置检查（目标干净、无 MERGE_HEAD）→ 按需 commit → merge；冲突则 abort 保主树、候选分支保留，返回四态 | S2-2 | M5-M7（合入含改动见 M1-M4）；E1 步 6 |
| 8 | （重复点击 / 重启后重放）批准后收尾 | 处置幂等：同处置只应用一次、重启恢复同一处置；合入成功后清理，清理失败单独重试 | S2-3 | M8、M11、M12；E1、E3 |

## 五、测试与回归

**新增测试文件。**

| 文件 | 覆盖 | 编号 |
|---|---|---|
| tests/test_workspace_binding.py | Spec 1 六组：绑定校验 / 原语绑定化 / 沙箱开箱收口 / 父子与 fan-out 拦截 / 恢复路径 / 任务卡字段 | B1-B13 |
| tests/test_artifact_merge.py | Spec 2 九组：四段检查 / 空沙箱判定 / 四态 / 前置检查 / 按需 commit / settle 幂等 / 证据绑定 / 三态字段 / 增强项 | M1-M13 |

**端到端集成（E*）。** 落 tests/test_artifact_merge.py 集成段，沿用 [test_worktree.py](../tests/test_worktree.py) 的真 Git 仓库惯例（tmp_path init + commit，mock git 无意义——协议面就是命令行）：

| 编号 | 场景 |
|---|---|
| E1 | 主验收场景全链路：临时仓库造一个失败测试→ScriptedLLM 委派修复→任务卡核验 diff+测试结果→批准合入→目标分支含修复 |
| E2 | 两个仓库并行任务互不干扰（分支/提交各进各的绑定仓库） |
| E3 | 重启恢复：处置落盘后重建 app，重放 settle 返回原处置、不重复合入 |

**复用回归。** [test_worktree.py](../tests/test_worktree.py)（原语生命周期）、[test_spawn.py](../tests/test_spawn.py) / [test_parallel_spawn.py](../tests/test_parallel_spawn.py)（工具子集与并行）、[test_task_feed.py](../tests/test_task_feed.py) / [test_task.py](../tests/test_task.py)（任务字段与事件）、[test_browser.py](../tests/test_browser.py)（前端面板）。既有断言不弱化：空沙箱清理、确认缝合回、spawn 反馈环字符串纪律都要保持全绿（S2-2 的「工具面字符串不变」即为此设）。

**三门与构建。** 每个 PR 过 ruff、mypy、pytest 三门 + 前端构建校验。既有惯例照抄：test_mcp_http / test_mcp_config 在本地沙箱代理下 502 环境红、以 CI 为准，不计本轮回归失败。

## 六、后续顺序

按 [M5 落地计划开工顺序表](m5-task-delivery-plan.md#开工顺序和发布范围)如实转述，**本轮只交付两份 P0 spec 与本拆单，实施按下表顺序下一轮开工**：

| 顺序 | spec | 依赖与交付判断（转述） |
|---|---|---|
| 3 · P1 | 审批恢复与任务推送 | SSE 游标可独立修复、可先于审批 spec 实施。审批恢复沿用工作区和操作归属，优先覆盖真实任务中的首次写入 |
| 4 · P1 | 并发账单与任务预算 | 可以独立开发。实时成本熔断以实际调用归因为前提 |
| 5 | 真实仓库试用 | P0 通过后开始。把恢复审批和实时预算加入发布说明时，分别补齐对应 spec 的验收 |

**大结果治理。** 超长测试日志、结构化 MCP 结果、重启后回查先入条件测试；出现窗口超限、重复工具调用或回放明显变慢等触发信号时，按 M5 条件件 c 与 d 立项（见[工具结果与并行回归](m5-task-delivery-plan.md#工具结果与并行回归)），本轮不展开。
