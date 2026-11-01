# M1 可控长任务——实机验收总报告

- 日期：2026-11-01
- 依据：ADR [115](docs/decisions/115-m1-controllable-long-tasks.md)（补注一/二/三已回写）
- 三门：ruff / mypy / pytest 全绿（各 PR CI 绿后合并，main=d5415d7）

## 1. 验收钉子对照（114 钉子）

| 钉子 | 状态 | 凭据 |
|---|---|---|
| 中途 steering 改向一次成功 | **通过**（实测 2/2 生效） | §2.1 |
| 任务级账单可查 | **通过** | §2.4 `GET /api/tasks/{id}/billing` |
| 一个 ≥1h 真实任务无人值守跑完 | **未达成**（六轮尝试，用户裁决收束；机制层全通） | §5 |

## 2. 四条链路验收明细

### 2.1 steering 打断（纠偏不杀 Run）
- `POST /api/runs/{rid}/steer` 两次实测均在同 Run 下一次模型调用生效（`steer.applied` 事件落账）。
- 收尾段到达的 steer 有兜底（起新 Run 接住 / `steer.dropped` 告知），测试钉住。

### 2.2 自动续跑
- 实测 1 任务 4 Run / 3 续跑：同 Session 起新 Run + `[自动续跑]` 注入消息，计划契约驱动。
- 判定顺序（`_task_verdict` 修复后）：任务级取消 > `fuse_fired` > Run 级取消 > failed/stuck > waiting-steer > 续跑 > done。

### 2.3 硬保险丝（双场景实测通过）
- **场景 A（续跑上限）**：cap=1 → 熔断落 `blocked` → `resume` 重置基线 → 任务跑到 done。
- **场景 B（时长熔断 × L2 挂起）**：60s 上限 + 确认挂起不放行 → 看守线程 30s 一跳 request_cancel 解挂 → 熔断落 `blocked`（非 cancelled）→ resume + 放行 → done。
- L2 确认挂起期间熔断不失效——看守线程设计实证成立。

### 2.4 任务分账
- per-Run 切片（两快照差值）+ `usage_approx` 并发标注 + 任务聚合 + billing 端点，实测 per-Run 明细/合计/approx 标记齐全。

## 3. 实机发现与处置（12 条已验证）

| # | 发现 | 定性 | 处置 |
|---|---|---|---|
| ① | 模型行为瑕疵（续跑注入消息理解偏差） | 非 bug | 不改码，goal 文案自纠 |
| ② | finish_plan 归档洗白 failed 步骤 → 误判 done（**真 bug**） | 中 | 按 ADR 修实现（用户裁决）：`_verdict_plan` 回落 `archive[-1]`，PR #58 合并 |
| ③ | `fuse_expired` off-by-one：`>=` 实际只执行 MAX-1 次续跑 | 中 | 修掉 off-by-one（用户裁决）：改 `>`，语义=恰 MAX 次续跑，PR #60 合并 |
| ④ | L2 挂起期间熔断 → Run cancelled 落任务 cancelled（shadow bug） | 中 | 分支顺序改「任务级取消 > fuse_fired > Run 级取消」，PR #60 合并 |

另：gateway 巡检 8 条（我独立复核 2 中 6 低，含 `_should_retry` 鸭子判型过宽、熔断快失败账本隐身两条「中」）+ llm.py 4 疑点（全低危/非现网路径）——**均未修，登记台账**。

## 4. 附带实机发现（非 bug，记录口径）

- make_plan 硬上限 10 步（模型自纠，非 bug）。
- provider 注册表无 `deepseek-chat` 键（`deepseek` = model deepseek-chat）。
- deepseek-flash 耐力不足：两轮长任务中途弃跑（批量 skipped / 批量 failed + finish_plan）；deepseek 稳定性好一档但重活前也会跳步（第五轮批量 skipped 假收官）。判据「含 failed → failed」两次正确落 failed = 发现②修复的正面实证。
- spawn_step 会把无动态证据的子步骤自动标 done（第四轮 #1-#3 曾被误标，父 agent 核 note 才发现）——记为疑点，是否算 bug 留裁决。
- 模型对「轮次预算见底」缺认知：不知道可收轮靠自动续跑接新预算（第 4 轮弃跑诱因之一，属人机契约文档缺口而非代码 bug）。

## 5. 长任务六轮记（≥1h 无人值守钉）

| 轮 | 模型/编排 | 结果 | 墙钟 | 要点 |
|---|---|---|---|---|
| 1 | deepseek-flash | failed | 3.8 min | 批量 skipped + 一步 failed + finish_plan 弃跑 |
| 2 | deepseek-flash + 反弃跑 goal | failed | 39 min | 改批量全 failed 弃跑（预算未用满，模型耐力问题非系统 bug） |
| 3 | deepseek + spawn_step 编排 | **done** | 16.6 min | 10 步全 done、0 弃跑、62 条发现（9H/30M/23L）、¥2.477、153 llm_calls |
| 4 | deepseek + 逐条动态验证 62 条 | failed | 11.2 min | 探针通道装配缺失（见下），模型批量标 failed 弃跑，零动态证据 |
| 5 | deepseek + 修正装配重跑 | done（**假收官**） | 9.8 min | 探针通道实测全通；但模型 7 步批量 skipped 混过 done 判定，62 条仅判 2 |
| 6 | deepseek + steering 反跳步 | 中断 | — | step1-3 有真探针证据；监控中断连带服务停机，任务断在 2 Run/1 续跑 |

**收束决定（2026-11-01，用户裁决）**：不再追 ≥1h 钉子，收尾出报告。钉子未达成
如实记录——六轮失败无一例是调度机制故障（续跑/判据/保险丝/分账/L2 机械放行
全部按设计工作），失败模式收敛为「模型在重活步骤前弃跑/跳步」。62 条动态验证
改由普通 subagent 直接补完（与 Task 通道解耦）。详见 ADR 115 补注三。

**验证补完现状**：补完编排亦中断——9 支探针已遗留 `data/checkpoints/probes/`
（H1/H2/H3/H4/H5/H9、M11-M20 相关，可作后续起点），但判定章节未落盘。
62 条动态验证维持**未判定**，留作后续专项（不阻塞本次收尾）。

**第四轮失败根因（装配问题，非服务故障，四项均为 goal 文案可修）**：
1. make_plan tools 清单漏 `write_file` → 范围护栏拒写盘，探针落不了 /tmp；
2. 模型用 base64/管道跑探针 → shell 元字符撞 run_command 护栏（正解：pytest 文件 + `pytest /tmp/x.py -q`，pytest 在白名单）；
3. spawn_step 子 agent 未获 write_file/run_command 授权；
4. 模型遇轮次预算见底批量标 failed——正解是收轮留活跃计划、自动续跑接新预算（模型不知此活路）。

红线全守住：robustness_patrol.md 原文未动、仓库零改动、零探针残留。账单 llm_calls=56/$0.847。

**第五轮探明的硬边界（装配真值）**：① write_file 有工作区围栏写不了 /tmp → 探针写 `data/checkpoints/probes/`（gitignore）；② run_command 60 秒强杀 → 探针须 <30s 全 mock；③ write_note 对已有笔记必拒 → 追加走 `PUT /api/notes/{name}` 或写新笔记后合并；④ update_plan_step 终态不可逆，证据要在标 done 前核。探针通道本身实测全通（H3 真出 pytest 结果）。

（第六轮终态数据、验证统计、高危逐条结论——**待补**。）

## 6. 待用户裁决

1. `data/notes/robustness_patrol.md` 62 条发现（9 高危：H1 流式记账/熔断复位跳过、H2 PEP 479、H3 github_bot 孤儿进程、H4 同步端点线程池假死、H5 resume 幂等竞态、H6-H9 见原文）是否排期修——涉及 M2 及以后。
2. gateway/llm 12 条低中危登记项是否随 M2 顺手清。
3. **done 判定语义**：`skipped` 算终态（ADR 115 原文如此），第五轮被模型批量 skipped 混出假收官。是否加机械约束（如 skipped 占比超阈值不落 done、转 waiting-steer）？改 ADR 行为语义，留用户裁决。
4. spawn_step 无证据自动标 done（§4 疑点）是否算 bug。

## 7. 证据索引

- 任务快照：`data/tasks/*.json`（de4be3fc=第三轮 done；23e32340/b3b656ec=前两轮 failed）
- 会话底片：`data/memory/sessions/20261101-134939.json`（轮1）、`20261101-135954.json`（轮2）
- 巡检报告：`data/notes/robustness_patrol.md`（62 条 + 覆盖表 + 局限）
- 代码修复：PR #58（归档回落）、PR #59（gitignore）、PR #60（熔断两修复）、PR #61（ADR 补注三）
