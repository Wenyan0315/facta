# Facta 2.0 P0 Spec：任务工作区绑定

> 状态=待评审 · 日期 2026-11-28 · 依赖：无（P0 第一份）· 关联：[研发落地计划](m5-task-delivery-plan.md) · [外部评审](m5-external-review.md) · [ADR 114](decisions/114-facta-2-roadmap.md) · [ADR 117](decisions/117-m3-multitask-and-authorization.md) · Spec 2 产物保留与安全合入（另文）

一句话：任务创建时把「在哪个仓库、合到哪个分支、相对哪个基线」钉成绑定三元组；执行、恢复、子任务、合入共用这一份绑定，所有 git 命令锚定用户仓库，Facta 主仓不再参与任何产物。

## 一、现状

### 已落地能力

- **Task 与落盘。** Task 一等实体 + TaskStore（内存快路径 + data/tasks/<id>.json 持久真值源），`repo_root` 已登记但 M1 明写「只登记不围栏」（[Task](../src/facta/orchestrator/task.py#L85)、[repo_root](../src/facta/orchestrator/task.py#L97)、[TaskStore.create](../src/facta/orchestrator/task.py#L208)）。
- **任务沙箱。** chat 任务默认沙箱隔离、skill 任务不套沙箱、续跑复用 task.worktree、收口走 GET/POST `/api/tasks/{task_id}/worktree`（merge/discard，queued/running 409）（[_ensure_task_worktree](../src/facta/server/app.py#L730)、[_task_sandbox_closeout](../src/facta/server/app.py#L758)、[_task_worker](../src/facta/server/app.py#L808)、[收口端点](../src/facta/server/app.py#L1513)）。
- **worktree 原语。** `create_worktree()` 无参，从 Facta 主仓当前 HEAD 切分支 `agent/<uuid8>`，目录 data/worktrees/<uuid>；`registry_in_worktree` 把 file/terminal 五件重锚进沙箱（audit、scope_check 一并继承）；`cleanup_stale_worktrees(keep=)` 启动回收（[worktree.py](../src/facta/tools/worktree.py#L67)、[registry_in_worktree](../src/facta/tools/worktree.py#L82)、[cleanup_stale_worktrees](../src/facta/tools/worktree.py#L167)）。
- **子 agent。** spawn 三件 + 计划三件 + sync_graph + 历史两件进 `_FORBIDDEN` 硬禁单；`spawn_subagent(worktree=True)` 建独立沙箱；`fan_out_subagents` MAX_FANOUT=8 并行聚合，刻意无 worktree 模式，定位检索/整理/验证杂活（[spawn.py](../src/facta/tools/spawn.py#L77)、[spawn_subagent](../src/facta/tools/spawn.py#L181)、[fan_out_subagents](../src/facta/tools/spawn.py#L277)）。

### 已复现问题

外部评审已复现并定位（[外部评审](m5-external-review.md#p0-工作区绑定和产物收口)）：

1. **错仓。** 用户选仓库 A，worktree 却从 Facta 工作仓 B 建立（`_git` 默认 cwd=WORKSPACE_ROOT、`create_worktree()` 无参，[worktree.py](../src/facta/tools/worktree.py#L43)），产物落 B，合入接口返回成功也进 B。
2. **父子落点不一致。** 父任务已重绑 worktree，子 Agent 仍经原注册表把文件写回原工作区——继承链在 spawn 路径上断开。
3. **产物无从找回。** worktree 目录缺失时静默清字段起新沙箱（[_ensure_task_worktree](../src/facta/server/app.py#L741)），旧改动无恢复入口。
4. **fan-out 可写。** 默认工具集=全量减禁单（[spawn.py](../src/facta/tools/spawn.py#L226)），复现中 fan-out 执行了写类工具；若程序侧静默摘除写工具，模型会以为写成功。

## 二、行为契约

### 2.1 绑定三元组（创建即校验）

- 创建任务时必须确定并校验绑定三元组，执行、恢复、子任务、合入共用这一份绑定：
  - **repo_root**：用户选定项目的 Git 根。必须以 `git rev-parse --show-toplevel` 校验；路径不存在或非 Git 目录时创建失败，返回明确原因，不得静默回落 Facta 工作区。
  - **target_branch**：显式指定，或缺省取绑定仓库创建时的当前检出分支；必须存在，不存在时创建失败并给明确原因。
  - **baseline**（基线）：创建时 target_branch 的 HEAD 全 SHA。必须钉死，不跟随分支头——此后 target_branch 前进不得改写 baseline。
- 三元组解析失败时任务不得创建（创建失败是明确失败态，不得落成能跑的任务再事后补救）。

### 2.2 字段与真值源

- Task 新增 `target_branch`、`baseline`、`worktree_branch` 三个字段（`repo_root`、`worktree` 已有）。
- 分支名以 `worktree_branch` 字段为真值源；必须废除从目录名反推 `agent/<wt.name>` 的隐含约定（现 [commit_and_merge_back](../src/facta/tools/worktree.py#L120)、[discard_worktree](../src/facta/tools/worktree.py#L158)）。
- 落盘快照（asdict）与 API 视图必须携带新字段——落盘自动跟上，响应按显式挑字段纪律补齐（[to_dict](../src/facta/orchestrator/task.py#L146)）。

### 2.3 worktree 落点与 Git 锚定

- worktree 目录位置不变：Facta 仓 data/worktrees/<uuid>（集中清理面，[paths.py](../src/facta/paths.py#L99)）。
- 所有 git 命令必须锚定绑定 repo_root：worktree 是绑定仓库的工作树，分支、提交都进用户仓库，不得再碰 Facta 主仓。`_git` 默认 cwd=WORKSPACE_ROOT 的行为废除，cwd 一律显式传绑定仓库根。
- 分支名沿用 `agent/<uuid8>`，命名美化不阻塞本 spec。
- worktree 分支必须从 baseline 切出（`git worktree add <dir> -b agent/<uuid8> <baseline>`）——沙箱里的改动相对钉死基线，预览才可复现（见分歧二）。
- 合回目标 = 绑定 target_branch，废除「合到当前分支」的隐含语义。

### 2.4 父子继承

- 子 agent 的 registry 必须继承父任务当前工作区：`registry_in_worktree` 机制沿用（file/terminal 五件重锚沙箱，其余共享，audit 与 scope_check 一并继承）。
- `spawn_subagent(worktree=True)` 的沙箱必须建在绑定仓库、分支进绑定仓库、合回目标 = 绑定 target_branch；`spawn_step` 同款。
- 子任务工具集仍强制过 `_FORBIDDEN` 禁单（[spawn.py](../src/facta/tools/spawn.py#L77)），递归防护不松。

### 2.5 fan-out 只读默认与写拦截

- fan-out 条目可用工具 = 父任务允许 ∩（全量 − `_FORBIDDEN` − `_WRITE_TOOLS`）。`_WRITE_TOOLS = {"write_file", "run_command"}` 模块级硬编码，程序侧纪律与 `_FORBIDDEN` 同款，不得依赖模型自觉。
- 子任务显式请求写工具（派发 `tools` 点名 `_WRITE_TOOLS` 成员，或条目执行中调用写工具）时，该请求不得执行，也**不得静默裁剪**（静默降级 = 模型以为写成功）：该条子任务收束，聚合结果该条必须标「已拦截：fan-out 不允许写工具，请改用 spawn_subagent(worktree=True) 或任务沙箱」，错误串回灌主 agent 自纠。
- 拦截粒度 = 单条子任务：同批其余条目照常执行与聚合，整个调用不得因一条写请求拒绝（见分歧三）。
- fan-out 定位不变：检索/整理/验证类杂活的并行位，不做 worktree 模式；需要写入的子任务走 `spawn_subagent(worktree=True)` 或任务沙箱。

### 2.6 恢复与失效

- 当 task.worktree 目录缺失但 `worktree_branch` 分支仍在：产物可找回。必须给用户恢复选项——①重建 worktree 挂回分支 ②直接合入分支 ③丢弃。裁决前不得起新沙箱、不得清绑定字段。「直接合入」的预览与执行语义归 Spec 2。
- 当分支也不在：如实报「产物已丢失」（含分支名与最后已知路径），此后才得起新沙箱续跑。
- 当绑定失效（绑定仓库不再是 Git 仓、target_branch 消失）：明确失败态（blocked + 原因），不得硬跑。
- 当 target_branch HEAD ≠ baseline（目标已前进）：标记「目标已前进」；合入前必须过 Spec 2 的预览有效性核对，不得直接套用旧预览。
- 当记录与 Git 元数据冲突（如 `worktree_branch` 与目录实际检出分支不一致）：保留现场，不得自动清理，呈现冲突供人工裁决。
- 启动回收 [cleanup_stale_worktrees](../src/facta/tools/worktree.py#L167) 不得清理被任务认领（非终态或待收口）的沙箱，也不得清理上述「保留现场」目录（keep 语义沿用）。

### 2.7 skill 任务

- skill 任务仍不套沙箱（文件锚 repo_root，现状不变，[_ensure_task_worktree](../src/facta/server/app.py#L730)）。
- 绑定三元组照常记录——恢复、合入、产物检查与 chat 任务同源。
- 任务面板必须显示真实执行方式：skill 任务标「无隔离：直接在仓库工作」，不得用「沙箱」字样笼统带过。

### 2.8 双仓并行

- 任务 T1 绑仓库 A、T2 绑仓库 B 并发执行时，沙箱、分支、子 agent 写入、合入目标必须各自独立互不串（含父子：T1 的子 agent 不得写入 B 或 Facta 仓）。
- 验收以工具输出或文件证据验证实际落点，不以界面声明为准。

### 2.9 任务卡呈现

- 任务卡必须显示：实际工作区路径、目标分支、基线短 SHA、执行方式（沙箱/无隔离）。
- 现有合并/丢弃收口动作沿用，动作目标以绑定为准（[TaskCard.jsx](../frontend/src/tasks/TaskCard.jsx#L157)）。

## 三、状态与数据变更

- **Task 字段**：新增 `target_branch: str`、`baseline: str`、`worktree_branch: str`（缺省空串 = 无绑定，即存量任务）。
- **请求面**：CreateTaskRequest 增 `target_branch: str | None`（缺省 = 绑定仓库当前检出分支）；POST /api/tasks 在创建前完成三元组校验，失败 → 4xx + 明确原因（现请求体见 [CreateTaskRequest](../src/facta/server/app.py#L301)、[create_task](../src/facta/server/app.py#L1363)）。
- **worktree 原语签名**：`create_worktree` 必须显式接收绑定（repo_root、分支名、基线）；`commit_and_merge_back` / `discard_worktree` 的分支真值源改为传入的 `worktree_branch`，合回目标改为 `target_branch`；`_git` 的 cwd 一律显式传绑定仓库根。调用方（任务沙箱、spawn、收口接口）同步接线。
- **视图与收口面**：`_task_view` 暴露新字段与执行方式；worktree 收口接口的 action 面扩恢复选项（枚举在实施第 5 步定，不预膨胀）。
- **ADR 修订**：ADR 117 §4「主 agent 任务维持 WORKSPACE_ROOT 锚点」的表述需以补注修订为「任务工作区锚点由绑定三元组决定（chat 任务 = 任务沙箱，skill 任务 = repo_root）」；该节技能文件围栏换锚语义不变。补注回写，不改历史裁定原文。

## 四、兼容规则

- **存量任务**：无绑定字段的任务读取兼容（字段缺省空串），不补造绑定——历史不可考，不得事后猜仓库/分支/基线。
- **存量任务合入**：走旧语义（原 worktree/分支路径）；Spec 2 的产物检查对无基线任务回落 merge-base 并标注「无基线，回落 merge-base」。
- **维持现状**：分支命名 `agent/<uuid8>`、worktree 目录集中存放、fan-out 只读杂活定位、skill 任务不套沙箱——均不变。
- **加法变更**：新字段与新响应键是加法，旧前端/旧 API 消费方不受影响。

## 五、失败处理

| 失败场景 | 表现 | 处置 |
|---|---|---|
| repo_root 非 Git 目录或不存在 | 创建失败，原因含 rev-parse 错误信息 | 拒建任务，用户改路径后重建 |
| target_branch 不存在 | 创建失败，原因指明缺失分支 | 拒建任务，用户改分支后重建 |
| 任务沙箱创建失败（git 报错） | 落 blocked，原因 = 错误串 | 现状语义沿用，resume 重试 |
| worktree 目录缺失、分支还在 | 不硬跑，呈现「产物在分支 X」 | 给恢复选项：重建挂回 / 直接合入 / 丢弃 |
| worktree 目录缺失、分支也不在 | 明示「产物已丢失」 | 起新沙箱续跑，事件留痕 |
| 绑定仓库非 Git / target_branch 消失 | 明确失败态（blocked + 原因） | 不硬跑；修复绑定后 resume |
| target_branch HEAD ≠ baseline | 标「目标已前进」 | 合入前过 Spec 2 预览有效性核对 |
| 记录与 Git 元数据冲突 | 呈现冲突（字段 vs 实际分支） | 保留现场，不自动清理，人工裁决 |
| fan-out 条目请求写工具 | 该条不执行，聚合标「已拦截…」 | 错误串回灌主 agent 自纠，其余条目照跑 |

## 六、测试矩阵

用临时 Git 仓库 + 脚本模型（ScriptedLLM）做成可重复回归，沿 [tests/test_worktree.py](../tests/test_worktree.py) 惯例（tmp_path init + monkeypatch worktree 模块根）；落点新增 tests/test_workspace_binding.py。

| 编号 | 场景 | 通过条件 | 测试落点 |
|---|---|---|---|
| B1 | 绑定校验：repo_root 非 Git | 创建失败，原因明确（非 Git / rev-parse 信息） | tests/test_workspace_binding.py |
| B2 | 三元组记录 | target_branch/baseline 记录正确；baseline = 创建时 HEAD 全 SHA；随后分支头前进，baseline 不变 | 同上 |
| B3 | target_branch 解析 | 缺省 = 当前检出分支；显式指定不存在的分支 → 创建失败 | 同上 |
| B4 | worktree/分支落点 | 沙箱分支与提交进绑定仓库（`agent/<uuid8>` 可查），Facta 主仓无新分支/新提交 | 同上 |
| B5 | 双仓并行互不串 | T1 绑 A、T2 绑 B 并发：沙箱、分支、写入、合入落点各自正确，父子写入不越界（文件证据核验） | 同上 |
| B6 | 父子继承 | spawn_subagent(worktree=True) 写入落绑定仓库沙箱，合回目标 = 绑定 target_branch | 同上 |
| B7 | fan-out 默认只读 | 缺省条目工具集不含 write_file/run_command（程序侧判定） | 同上 |
| B8 | fan-out 显式写拦截 | 请求写工具的条目不执行、聚合标「已拦截…」固定回执；同批其余条目照跑有结论 | 同上 |
| B9 | 恢复选项 | 删 worktree 目录留分支：呈现三选项；重建挂回后原产物可找回 | 同上 |
| B10 | 绑定失效 | target_branch 删除 / 仓库转非 Git：明确失败态，不启动执行 | 同上 |
| B11 | 目标已前进 | target_branch 加新提交：呈现「目标已前进」，合入入口标注需重新核对 | 同上 |
| B12 | 旧任务兼容 | 无绑定字段存量 JSON 读取正常、不补造绑定、合入走旧语义 | 同上 |
| B13 | 元数据冲突 | worktree_branch 与目录实际分支不一致：保留现场不清理、呈现冲突 | 同上 |

任务卡呈现（2.9）以 `_task_view` 字段断言并入 B2/B6 落点；前端真机展示归 [tests/test_browser.py](../tests/test_browser.py) 惯例，不在本矩阵扩项。

## 七、实施顺序

1. **绑定解析与字段**（无依赖）。Task 三字段 + to_dict + CreateTaskRequest.target_branch + POST /api/tasks 三元组校验（rev-parse、分支存在、baseline=HEAD 全 SHA）+ 存量读取兼容。涉及 [task.py](../src/facta/orchestrator/task.py)、[app.py](../src/facta/server/app.py)。测试：B1-B3、B12。
2. **worktree 原语绑定化**（依赖 1）。create_worktree / commit_and_merge_back / discard_worktree 接收绑定参数（repo_root、worktree_branch、target_branch、baseline），`_git` 显式 cwd，废除目录名反推分支。涉及 [worktree.py](../src/facta/tools/worktree.py)。测试：B4 原语层。
3. **任务沙箱接线**（依赖 2）。_ensure_task_worktree 按绑定建沙箱（分支从 baseline 切）、收口端点走绑定分支与 target_branch，_task_sandbox_closeout 语义不变。涉及 [app.py](../src/facta/server/app.py)。测试：B4、B5 父侧。
4. **父子继承与 fan-out 拦截**（依赖 2、3）。spawn_subagent(worktree=True) / spawn_step 绑定化；fan-out 默认只读 + `_WRITE_TOOLS` 单条拦截回执。涉及 [spawn.py](../src/facta/tools/spawn.py)、[worktree.py](../src/facta/tools/worktree.py)。测试：B5 子侧、B6-B8。
5. **恢复路径**（依赖 3）。worktree 缺失分支在 → 恢复选项三选一（收口接口扩 action + 任务卡入口）；分支不在 → 产物已丢失再起沙箱；绑定失效 / 目标已前进 / 元数据冲突呈现。涉及 [app.py](../src/facta/server/app.py)、[task.py](../src/facta/orchestrator/task.py)。测试：B9-B11、B13。
6. **任务卡展示**（依赖 1，完整依赖 3-5）。_task_view 暴露工作区路径 / 目标分支 / 短 SHA / 执行方式；TaskCard 渲染，skill 任务「无隔离」标注。涉及 [app.py](../src/facta/server/app.py)、[TaskCard.jsx](../frontend/src/tasks/TaskCard.jsx)。测试：视图字段断言并入 B2/B6。
7. **ADR 117 §4 修订 + 全矩阵收口**（依赖 1-6）。§4 锚点表述补注修订；tests/test_workspace_binding.py 全矩阵过绿。涉及 [ADR 117](decisions/117-m3-multitask-and-authorization.md)、tests/test_workspace_binding.py。测试：B1-B13 全量。

## 八、设计分歧与推荐

1. **绑定存 Task 字段 vs 独立 binding 表** → 推荐 Task 字段。一任务一绑定，读写收口在 TaskStore 一处；独立表引入第二真值源与关联一致性问题，是过度设计。
2. **基线钉死 vs 跟随分支头** → 推荐钉死。预览可复现：任务任何时刻都能回答「相对哪个点的改动」；目标前进成为显式事件（「目标已前进」标记 + Spec 2 重新核对），而不是静默漂移让预览与合入对象悄悄换位。
3. **fan-out 写工具拦截粒度：整个调用拒绝 vs 单条子任务拦截 + 回执** → 推荐单条拦截。其余子任务照跑，反馈环不误伤；整调用拒绝会把一批只读杂活陪葬，主 agent 还得整体重派。固定回执串 + 错误串回灌满足「不静默裁剪」的可见性要求。
4. **worktree 目录 Facta 集中存放 vs 用户仓库内** → 推荐维持集中。清理/回收面单一（cleanup_stale_worktrees 一处）；正确性由「git 命令锚定绑定仓库」保证，与目录物理位置无关。搬进用户仓库会污染用户目录，且回收逻辑散进各仓库。
