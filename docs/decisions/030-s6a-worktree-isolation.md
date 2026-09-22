# 决策记录 · S6a worktree 隔离（2026-09-22）

> S6 第一站：子 agent 的文件系统沙箱。返回 [architecture.md](../architecture.md)

- **四拍板（开工设计草案定）**：
  - **P1 WORKSPACE_ROOT 注入化**：模块常量 → `ToolContext.workspace_root`。files/terminal 全族函数改 keyword-only `root` 参数（默认值引用 paths.py 真值源——与 notes_dir 禁默认的判据不同：主值是项目常量非环境相关，引用传播非第二真值源）；register 闭包捕获 ctx 值。代价：17 处测试 monkeypatch 改显式传参（反而更直白——显式参数优于魔法 patch）。
  - **P2 产物合回走确认缝**：子 agent 跑完 → `worktree_changes` 出 diff 摘要 → `confirm("merge_worktree", {...})` 人审 → 批准=commit+merge 回主分支，拒绝/无通道=整棵丢弃（保守默认与 L2 同哲学：没有眼睛就不动手）。与 make_plan 同一「人审掌舵」原则——计划是掌舵点，合回也是。
  - **P3 worktree 生命周期**：创建 `git worktree add data/worktrees/<uuid> -b agent/<uuid>`（分支名可追溯）；spawn 结束即清理（合回或丢弃后 remove+删分支）；启动扫残留回收（`cleanup_stale_worktrees`——「先杀进程再删文件」血案同款防线）。
  - **跨进程不进本轮**：概念三分法裁定——「并行改文件不互踩」worktree（文件系统视图隔离）+线程（IO-bound，GIL 不碍事）已解；「崩溃不连坐」才需要进程，实机未踩中。跨进程挂触发信号=「子 agent 崩溃连坐实踩」或 S8 常驻进程开工。这比多进程议题原文「S6 正题=spawn 跨进程」推迟一站，理由 YAGNI+渐进演化。

- **_worktree_registry（子 registry 构造）**：worktree 模式下子 agent 不共享主 registry，构造子实例——file/terminal 五件**重锚**（重新注册，闭包锚 worktree 目录），其余工具**原样搬运**（同一 Tool 对象注册进第二 registry，闭包锚主资源——知识库/待办/时钟共享是正确语义）。审计同源（`registry.audit` 透传），S3「单一必经点」与 S5a「确认缝收口不分叉」都保持。

- **工具子集语义不变**：禁止单（`_FORBIDDEN`）在 Agent.allowed_tools 层过滤，registry 全量注册无妨——worktree 模式的 effective_registry 换了实例但名字集合等价。

- **防御细节**：
  - `commit_and_merge_back` 的 commit 显式带 `-c user.name=cortex-agent -c user.email=agent@cortex.local`——CI runner 无全局 git 配置会 commit 失败（本地 macOS 有配置全绿掩盖）；且语义正确：spawn 的 commit 本就是 agent 干的，归属标注清楚。
  - merge 失败不删分支（`agent/<uuid>` 上改动可手工抢救），worktree 目录照清（错误串回灌反馈环）。
  - files.py 黑名单加 `data/worktrees`（防 search_code 的 rglob 扫进沙箱造成同文件双重命中+主 agent 读子沙箱）。
  - 空改动守卫：无改动不 commit 不 merge，直接清理（与 VectorStore 删除守卫同哲学）。

- **三个开发实踩（教训入档）**：
  1. **模块级默认参数固化 Path 对象，monkeypatch 无效**：`_git(*args, cwd=WORKSPACE_ROOT)` 的默认参数在模块加载时求值——测试 patch 模块名字后已固化的默认值不变，worktree 建到了**真项目**里，垃圾 commit（`test: 改动`+两个假文件）混进 main，靠 `git reset --hard` 回滚。修法：默认参数改 `None`+函数体内解析名字（patch 运行时生效）。同型坑：from-import 的名字绑定在消费模块，patch paths 不影响消费模块——patch 目标必须是消费方模块。
  2. **`git reset --hard` 误伤未提交改动**：回滚测试污染时忘了工作区还有 S6a 全部未提交改造，reset 后 tracked 改动全丢（untracked 的 worktree.py/test_worktree.py 幸存），靠记忆重放。教训：reset/checkout 类破坏性操作前必须先查 `git status` 确认无未保存工作。
  3. **CI 与本地的 git 环境差异**：CI runner 无全局 git 身份配置——产品侧 commit 显式带身份后环境无关（同款思路：依赖环境隐式配置的代码在 CI 裸机上都会炸）。

- **验收**：pytest 388 passed（+8 worktree：原语生命周期 4 + spawn 端到端 4——隔离合回/拒绝丢弃/无改动清理/非 worktree 回归）；**冒烟回归 4 条全过（S6 安全网第一次兑现——spawn 内部加了 worktree 分支，外部行为由冒烟套件自动把关）**；ruff/mypy/CI 三道门全绿。实机验收（真模型派 worktree 任务+浏览器看 merge_worktree 弹窗）排实机轮。

- **S6b 留档**：并行 spawn 形态（一组任务 vs 异步连续派发）开工时拍；S6c 真编排（plan 步骤驱动 spawn）站后。
