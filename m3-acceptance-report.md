# Facta M3 真机验收报告（ADR 117：后台队列 + 任务级并发上限 + 授权档位）

## 验收环境与口径

- 日期：2026-11-19（ADR 117 裁定 2026-11-17，同日实现、PR #67 CI 绿后 squash 合并）
- 代码：main = `ec0aca4`（PR #67 squash 合并，实现 commit `a172a8c`）；仓干净（仅 `data/graph.json` 按惯例不提交）
- 服务：`FACTA_PROVIDER=deepseek FACTA_SERVER_PORT=8100 .venv/bin/python -m facta.server`
  启动于仓根；「重启回落」段以 `FACTA_MAX_CONCURRENT_TASKS=1` 二次启动（把上限压到 1，
  让回落后的排队态可稳定观测）；`FACTA_MAX_CONCURRENT_RUNS` 用新缺省 5。
- 三门：由 PR #67 CI 三检查全绿承担（`agent`＝ruff+mypy pass、`test` 3.11/3.12 双 pass，
  1m17s–4m36s）；实现期本地全量 1002 passed / 7 skipped（`test_mcp_http.py`、
  `test_mcp_config.py` deselect——本地代理 502 的环境性预存失败，`git stash -u` 后在
  干净树上同样失败，与本次改动无关）。
- 验收探针：临时技能两件（**验收后已删**，仓内不留痕）——
  `data/skills/wprobe.md`（`## authorization write`、budget 6、settle skip，人设＝照指令
  写文件并回 `WROTE <路径>`）与 `data/skills/rprobe.md`（缺省 readonly、budget 3，人设＝
  先如实报菜单、缺工具则回 `NO_TOOL <名>`）。两件均由**运行时装配**（`mode` 422 报错串
  实测列出 `chat, review, brief, wprobe, rprobe`——新增技能零 py 改动，ADR 116 §1 钉子沿用）。
- 探针仓：`/tmp/facta-accept-20261119/proj_w`（含 `PLAN.md` 标记文件 `PROJ_W_MARKER_9c41bd7e`），
  作技能任务的 `repo_root`，验写围栏换锚（ADR 117 §4）。
- 口径：全 API（curl + jq + SSE 事件流），不依赖前端；SSE 用 `curl -sN --max-time N` 截流
  取证。所有 trace/脚本/账单留在 `/tmp/facta-accept-20261119/` 供复核。

## 待验收清单逐项结论（对应 ADR 117「待验收」）

| 项 | 结论 | 真机证据 |
|---|---|---|
| 三门全绿 | **过（CI）** | PR #67：`agent` pass、`test`×2 pass；实现期本地 1002 passed（新增 `tests/test_queue.py` 9 条 + `tests/test_authorization.py` 10 条） |
| 钉子①：3 任务并行互不阻塞 | **过** | 慢任务批（`69477460`/`39e4d55a`/`cb139526`/`815d8672`）1s 采样：`t+0s` 三个 `running` + 第四个 `queued`；`t+8s` 四个全 `done`，**无一 blocked**。另一批 4 个即时任务（`645d9860`/`108aaa8e`/`7b674cb7`/`1e42d135`）同样全 `done` 无 blocked |
| 钉子②：涉写任务停 L2 → approve → 免停 | **过** | `wprobe` 任务 `c6a38ea0`（repo_root=探针仓）：`t+7s status=waiting-approval`、`authorized_write=false`；SSE `confirm.request {"tool":"write_file","arguments":{"path":"a.txt","content":"A_M3_OK\n"}}`（run `c37beb2c`）；`POST /api/runs/{id}/confirm {"approve":true}` → 200 `{"status":"approved"}` → 同一 run 内 `tool.result`(a.txt) → 第二次写 b.txt **全程无第二个 confirm.request** → `text.delta` `PROBE_DONE` → `t+9s done`、`authorized_write=true`；探针仓落 `a.txt`(8B)/`b.txt`(8B) 内容 `A_M3_OK`/`B_M3_OK`，`PLAN.md` 标记文件未动（写落在 repo_root 内，没跑到 facta 仓＝写围栏换锚生效） |
| 钉子②对照：reject 路径 | **过** | 同技能新任务 `9cd6cd9a`（写 c.txt）→ `waiting-approval` → `{"approve":false}` → 200 → `t+2s done`、**`authorized_write=false`**、探针仓**无 c.txt**；事件序列 `confirm.request → confirm.resolved → tool.result`（拒绝原因回模型）`→ text.delta`，模型未再试写 |
| readonly 结构性缺席 | **过** | `rprobe` 任务 `08bf6135` 被要求写 x.txt：终态 `done`，事件计数 `confirm.request = 0`；模型自陈菜单**恰 8 件**（`get_current_time`/`list_notes`/`read_notes`/`search_notes`/`search_and_summarize`/`read_file`/`search_code`/`list_dir`）并回 `NO_TOOL write_file`「未执行，未生成 x.txt，也未编造内容」；探针仓无 x.txt |
| 覆盖规则：只能收严 | **过** | 收严：`wprobe`（声明 write）+ `authorization=readonly` 任务 `3d1bf6c2` → 任务视图 `authorization=readonly`、`confirm.request=0`、模型答「write_file 不在当前工具菜单里」、y.txt 未生成。放宽：`rprobe`+`write` → **422**「不可放宽：技能 'rprobe' 声明档位为 readonly，请求只能收严（write→readonly）」。未知档位 `sudo` → 422；未知技能 `nosuch` → 422；无 skill 带 authorization → **400**「authorization 只作用于技能任务」 |
| 队列行为：排队 / 终态触发 / 巡检兜底 | **过（巡检＝单测）** | 第 4 任务 `t+0s queued`（上限 3 生效）；`t+4s` 首个任务 `done` 的**同一采样拍**第四任务转 `running`＝终态触发拉起，未等 60s 巡检。60s 巡检兜底线程真机无「错过事件」场景可造，由 `tests/test_queue.py` 钉（含异常捕获续跳） |
| 队列行为：重启回落重入队 | **过** | 4 个慢任务（3 `running` + 1 `queued`）→ `kill -9`（无优雅收尾）→ 落盘 JSON 仍是 `running`×3/`queued`×1 → 以 `FACTA_MAX_CONCURRENT_TASKS=1` 重启 → 就绪即时快照 `f6205345=done`、`0a1f6afe=running`、`04b7b066=queued`、`999c1bb3=queued`（**脏 running 回落 queued**，且回落量受新上限约束、不是一拥而上）→ 串行排空至四个全 `done`，FIFO 保持（最晚创建的 `999c1bb3` 最后跑），无一 blocked |
| 重启不洗白熔断/取消 | **过** | 重启后 `fuse_fired=true` 的三个 `blocked` 任务（含证据闸熔断单 `ae3e197b`、M1 期 `3ba2f013`/`bf9c6ca7`）**全部仍 blocked**；全量分布 `done 26 / failed 5 / blocked 3 / cancelled 1`，无遗留 queued/running |
| resume 走队列（尊重上限） | **过** | 上限压到 1 且槽被占位任务 `603d8764` 占用时，`POST /api/tasks/ae3e197b/resume` → 200 且 `status=queued`，**连续 6s 保持 queued**（不直起 worker，起 worker 只有调度器一个入口）；resume 同时按 M1 语义重置保险丝基线（`fuse_fired=false`、`continuations=0`）；queued 态 `cancel` → `cancelled`，零额外账单 |
| 零回归抽查 | **过** | 聊天 `POST /api/runs/sync`（缺省 mode=chat）→ 200 `{"status":"completed"}`、`text` 恰为 `CHAT_OK`；**主 agent 任务（不绑技能）write_file 免确认直写**：`data/checkpoints/probes/m3_probe.txt` 落盘内容 `M3_MAIN_OK`，事件计数 `confirm.request = 0`，`t+9s done`（ADR §3「档位只作用于技能任务」） |
| spawn_step 证据闸 | **过（含一处绕行观察）** | 任务 `ae3e197b`：`make_plan`（tools 显式声明 `spawn_step`）过 L2 人审 approve 后 `spawn_step` 执行「只回一行 EVIDENCE_OK」的子任务，返回原文：「步骤 #1 **未标 done**：子任务结论缺可验证证据（无 checkpoint id/文件路径/run_id/会话 id 锚点），待人工核。子任务结论：EVIDENCE_OK；当前计划：◐ 1. 探针步骤 —— 缺可验证证据，待人工核」；步骤留 `in_progress` → `finish_plan` 拒收官 → 模型反复尝试至 **21 次续跑触发 M1 保险丝** → `blocked`（该单 36 calls / ¥0.2989 / 104s） |

**结论：ADR 117「待验收」清单全部通过，未发现 P0 问题，无阻断项。**

## 证据片段（原始输出摘录）

队列与并行（`queue_trace.txt`，1s 采样，去重后）：

```
created A 69477460 / created B 39e4d55a / created C cb139526 / created D 815d8672
t+0s 69477460=running 39e4d55a=running cb139526=running 815d8672=queued   ← 上限 3 + FIFO 排队
t+4s 69477460=done    39e4d55a=running cb139526=running 815d8672=running   ← 终态同拍触发拉起
t+8s 69477460=done    39e4d55a=done    cb139526=done    815d8672=done      ← 四个全终态，无 blocked
```

写首停与免停（`approve.txt`，同一 run `c37beb2c` 的事件序列）：

```
status=waiting-approval {"skill":"wprobe","authorization":"write","authorized_write":false}
confirm.request  tool=write_file  arguments={"path":"a.txt","content":"A_M3_OK\n"}   ← 首写停 L2
approve → 200 {"status":"approved"}
tool.result → tool.started → tool.result                                            ← b.txt 免停直写
text.delta "PROBE_DONE" → run.finished → task done {"authorized_write":true}
探针仓：PLAN.md（标记未动） a.txt 8B b.txt 8B                                        ← 围栏锚 repo_root
```

重启回落（`restart.txt`，kill 前后对照）：

```
kill 前 API：f6205345=running 0a1f6afe=running 04b7b066=running 999c1bb3=queued
kill -9 后落盘 JSON：running / running / running / queued      ← 进程被杀，无优雅收尾
cap=1 重启后即时落盘：done / running / queued / queued          ← 脏 running 回落 queued 且受上限约束
排空 trace：…→ 999c1bb3=running → 999c1bb3=done（FIFO，四个全 done）
```

## 发现的异常与处置

**P0（阻断/数据损坏/红线违反）：无。**

1. **观察（证据闸有绕行路径，属既定范围）**：首次真机尝试中 `make_plan` 的 tools 未声明
   `spawn_step`，范围护栏拒调未声明工具，模型改用 `spawn_subagent` 跑子任务 + `update_plan_step`
   **手工**把步骤标 `done`——证据闸未触发（任务 `95b686d6`，SSE 见 `plan.step_updated →
   plan.finished`）。即闸只覆盖 `spawn_step` 自动标 done 这一条路径，`update_plan_step` 是敞口。
   ADR §5 落点本就只裁 `spawn_step`，属既定范围而非实现偏差。**处置＝不改码，挂触发信号**：
   首例「手工标 done 掩盖无证据步骤」造成实际损失时，再把闸下移到 `update_plan_step`/`finish_plan` 侧。
2. **观察（闸生效时会空转到熔断）**：闸拦下后模型无「合规出路」（该子任务结论天然不含锚点），
   持续重试至 20 次续跑熔断，本单烧 ¥0.30 / 104s。机械后果符合设计（宁可 blocked 不假 done），
   但返回串只说「待人工核」，未给模型可走的下一步。**处置＝不改码，挂触发信号**：真实任务里
   反复出现「闸→熔断」空转再优化提示语（如提示「可先推进其他步骤或如实汇报受阻」）。
3. **观察（确认文案未按 ADR 明示授权语义）**：ADR §3 写「确认消息明示『本任务写授权（批准后
   本任务后续写操作免停）』」；真机 `confirm.request` 只携 `tool` + `arguments`（与 `run_command`
   常规确认同形），无该文案。**机制正确**（首停-approve-免停链、reject 不置位均实测成立），差的
   是 UI 文案：`Run.request_confirm(tool, arguments)` 无 message 形参，补文案要动 run_store +
   前端确认卡。**处置＝本期不改**（确认卡属 M4 宿主层面板范畴），已回写 ADR 117 实现注记，
   触发信号＝M4 面板落地时一并补授权语义文案。
4. **观察（主 agent 任务的 authorization 字段易误读）**：不绑技能的任务视图 `authorization`
   缺省显示 `readonly`，但主 agent 任务 `write_file` 实测免确认直写（ADR §3「档位只作用于技能
   任务」）。字段展示可能让人误读为「该任务受只读约束」。**处置＝不改码**（字段语义＝技能档位，
   无技能即无档位实效），触发信号＝前端面板上线时决定是否对 `skill` 为空的任务隐藏该字段。
5. **环境性**：`facta.core.jev [路由降级] HTTP Error 451`（外部服务）本次验收期 27 次，
   按既有设计走 LLM 原生路径，与 M3 无关（M2 验收期同源现象）。

## 成本账单（M1 起「每单挂账」纪律延续）

本次验收共 **20 个任务**（含 4 个即时任务批、4 个慢任务批、写/reject/readonly×2/主 agent/
证据闸×2/resume 占位等）：

| 指标 | 合计 |
|---|---|
| llm_calls | 127 |
| llm_cost | **¥0.9862** |
| llm_seconds | 193s |
| tokens_in / tokens_out | 943,711 / 21,238 |
| prompt_cache_hit_tokens | 698,368（命中率 ~74%） |
| jev_degradations | 27（外部 451，走原生路径） |

单均 ¥0.049；最贵一单＝证据闸熔断任务 `ae3e197b`（36 calls / ¥0.2989 / 104s，占本次验收
账单 30%——闸拦下后空转到熔断的成本，见异常 2）。聊天 sync 抽查 1 次未计入任务账单。
（M2 验收 11 单 ¥0.975、M1 验收 9 单 ¥1.348 作横比：M3 单数最多、单均最低。）

## 单测覆盖但真机未复现的项

- **60s 巡检兜底线程**：真机三个触发点（入队/终态/启动首拍）都在正常工作，无从造出
  「事件被错过」的现场；由 `tests/test_queue.py::test_queue_tick_survives_exception`
  等钉住（含异常捕获续跳）。代码锚点：`app.py::_queue_tick`（`_QUEUE_TICK_S = 60.0`）。
- **`requeue_on_boot` 幂等**：真机验证了一次重启回落；「重启两次结果一致」由
  `tests/test_queue.py` 钉（回落函数幂等，重启重跑无副作用）。

## 清理与复核

- 临时探针技能 `data/skills/wprobe.md`、`data/skills/rprobe.md` 与主 agent 抽查产物
  `data/checkpoints/probes/m3_probe.txt` **已删除**；`git status` 仅剩 `data/graph.json`
  （惯例不提交）与两个既有未跟踪笔记，仓内无验收残留。
- 验收服务已停（8100 端口释放）。重跑口径：仓根执行
  `FACTA_PROVIDER=deepseek FACTA_SERVER_PORT=8100 .venv/bin/python -m facta.server`；
  验队列/回落时另加 `FACTA_MAX_CONCURRENT_TASKS=1` 把上限压到 1 便于观测。
- 脚本与原始 trace：`/tmp/facta-accept-20261119/`（`queue_trace.txt`、`write_stop.txt`、
  `approve.txt`、`reject.txt`、`readonly.txt`、`regression.txt`、`restart.txt`、
  `resume.txt`、`evidence.txt`、`evidence2.txt`、`validation.txt`、`billing.txt`）。
- 任务落盘记录：`data/tasks/`（本次 20 单，状态与账单可按 task_id 复核）。
