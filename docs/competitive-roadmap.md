# my_project1 竞品对标 Roadmap + ADR 草案

> 版本：v0.1（草案） · 日期：2026-09-24
> 性质：外部分析产物（Kimi 工作区），**2026-09-25 已同步回本仓库 docs/**。037（P0-2）/038（P0-3）已正式立项，040 已被 P0-3 落地篇占用；**后续立项编号从 041 起**——039 已被 S8a 会话模型占用，本文件第 4 节 ADR 草案拟编号 038–042 随之作废，草案内容照用。
> 对标对象：Pi（earendil-works/pi，pi.dev）+ Claude Code、OpenCode、Aider、SWE-agent/mini-swe-agent、LangGraph、OpenHands、Codex CLI、Gemini CLI、Letta/MemGPT、Pi 系记忆生态（pi-mem / pond / pi-persistent-intelligence / pi-reasonix）。

---

## 1. 定位锚点（先守住，再谈改进）

my_project1 = **个人执行助手**：执行主轴 + 记忆护城河 + 通用外延（014 决策）。

已有资产（对标前盘点，切勿在平推中稀释）：

| 能力 | 状态 | 决策文档 |
|---|---|---|
| plan-then-act 执行主轴 | 已内置核心 | 027 |
| spawn_subagent / spawn_step 并行 | 已实现 | 031、033 |
| worktree 隔离 | 已实现 | 030 |
| 终端确认缝（L2） | 已实现 | 019、020 |
| 记忆固化原则（JSONL canonical、分层加载、异步 reflect） | 方向已定，实现中 | 032、034 |
| 知识图谱 + 图谱面板 | S7a/S7b 完成 | 035、036 |
| evalkit 评测基座 | 已实现 | 024 |
| ACI 工具反馈规范 + bash-only mini 回归基线 | 已实现（P0-2） | 037 |
| Run checkpoint 崩溃恢复（kill→heal→续跑，副作用不重复） | 已实现（P0-3） | 038、040 |

**核心判断**：my_project1 比 Pi 默认核心更产品化（Pi 刻意不做 plan mode / subagents）。改进原则因此是——**每个竞品只偷它最强的一块，全部落到执行主轴和记忆护城河上，不做功能平推。**

---

## 2. 竞品参照地图

| 竞品 | 只偷这一块 | 去向 |
|---|---|---|
| Claude Code | skills `allowed-tools` 一次性授权；hooks 确定性闸门（PreToolUse/PostToolUse） | P0-1、P0-4 |
| SWE-agent | ACI 工具反馈设计（edit 后 lint、~100 行窗口查看器、空输出显式化） | P0-2 |
| mini-swe-agent | bash-only 极简基线（~100 行、线性历史、独立 subprocess） | P0-2 的回归基线 |
| LangGraph | checkpoint（thread_id）+ interrupt/resume 语义；副作用幂等教训 | P0-3 |
| Pi | 会话树 /tree /fork /clone；compaction 摘要格式；prefix-cache 稳定前缀；JSON/RPC 模式 | P1-5、P2-4、P2-5 |
| Aider | tree-sitter repo map（符号图 + 排序 + token 预算）；architect/editor 双模型；auto-commit + /undo | P1-1、P1-4、P1-2 |
| OpenCode | LSP 实时诊断；SQLite 快照回滚 | P1-3、P1-2 |
| Codex CLI | sandbox_mode ⊥ approval_policy 正交旋钮 | P1-6 |
| OpenHands | Docker sandbox 默认、controller/execution-server 分离、headless REST | P1-6 参照 |
| Gemini CLI | trusted folders、MCP 工程实现 | P1-6 参照 |
| Letta/MemGPT | 记忆块硬上限；sleep-time 离线整理 pass | P2-1、P2-2 |
| pi-mem / pond | 纯 Markdown/JSONL、零向量库的跨 harness 会话归档 | P2-1 格式校准 |

---

## 3. Roadmap

### P0 —— 直接强化执行主轴

> 条目 ID 全文稳定；优先级口径以第 6 节「终版优先级」表为准（v0.4 起）。

#### P0-1 Hooks 式确定性闸门
- **来源**：Claude Code hooks（command/http/mcp_tool/prompt/agent 五型，Pre/PostToolUse 事件）+ SWE-agent「edit 后自动 lint」。
- **改动**：在 `tools/registry.py` 增加 pre/post hook 槽位；首批内置两个闸门：① write/edit 后自动跑对应语言的 linter（失败结果回注工具反馈）；② 危险命令正则闸门（`rm -rf` 等）走 L2 确认缝。审计日志仍收口 registry，hooks 不绕开现有审计。
- **触发信号**：evalkit 回放中「agent 写入的代码到下一轮跑测试才发现语法/类型错误」占比 > 10%。
- **验收标准**：evalkit 新增「写入即可运行」场景集，首轮可运行率显著上升；闸门本身零 LLM 调用（确定性执行）。
- **首批改动点**：`tools/registry.py`（hook 槽位）、新增 `src/agent/hooks/lint_gate.py`、`tests/` 对应场景。

#### P0-2 ACI 工具反馈规范 + mini 基线
- **来源**：SWE-agent ACI 论文；mini-swe-agent。
- **改动**：① 文件查看工具分页化（~100 行窗口 + 「上方 N 行 / 下方 M 行」指示）；② 所有工具空输出必须显式返回「无输出」标记；③ 搜索工具默认只列有匹配的文件，不喷全文；④ 在 evalkit 中固化一个 bash-only 迷你 agent 配置作为**永久回归基线**。
- **触发信号**：单次任务工具输出 token 占比、无效/空工具调用率可度量后，任何机制改动都必须回答「比 bash-only 基线好在哪」。
- **验收标准**：同一 evalkit 场景集上，总 token 下降且解决率不降；基线配置可一键跑。
- **首批改动点**：`src/agent/tools/` 文件查看与搜索工具、evalkit 场景与基线配置。
- **状态**：✅ 代码已落地（`c57ef76`，2026-09-25）——四拍板全实现（`read_file` 100 行窗口 + 上下方行数指示、空输出显式标记、`search_code` 默认只回文件清单 + 命中计数、`evals/baseline_agent.py` bash-only 基线一键出分）。验收剩余项：同场景集 A/B 报告（需真模型前后对比）与实机 3 任务人工对比可读性，并入下一轮实机验收。

#### P0-3 Run checkpoint 与崩溃恢复
- **来源**：LangGraph checkpointer/interrupt 语义（只借语义，**不**重写为图）。
- **改动**：在 tool/plan 事件后向 Run Store（外置 JSONL）追加 checkpoint；定义 resume 语义——从最近 checkpoint 重建上下文继续；所有副作用型工具（写文件、终端、网络）标注幂等性，resume 重放时按标注去重。吸取 LangGraph 教训：resume 会导致节点重跑，副作用必须幂等。
- **触发信号**：长任务（> 5 分钟或 > 20 工具调用）进程崩溃后只能从头重跑。
- **验收标准**：`kill` 进程后可从上个 checkpoint 恢复并完成任务的 evalkit 场景通过；checkpoint 文件可人工阅读（JSONL）。
- **首批改动点**：`src/agent/orchestrator/loop.py`（checkpoint 写入点）、`memory/store.py` 或新增 Run Store 模块。
- **状态**：✅ 已落地（2026-09-25），实现记录与落地裁定见 [040](decisions/040-run-checkpoint-impl.md)。**与本篇原文的一处实质差异**：resume 不是「重放 + 按标注去重」，而是 **heal 补洞 + 向前走，压根不重放**——副作用不重复由「不重放」保证，幂等标注（新字段 `Tool.idempotent`，只读免声明）降级为 heal 文案依据（可重做 / 先核验现场）。checkpoint 形态 = 底片原子落盘 + `data/checkpoints/{sid}.jsonl` 账本（只记 intent/result）。验收：kill-resume 场景集实机 2/2、13 个离线测试、三道门全绿、**Web 入口实机 SIGKILL 服务→换进程重启续跑通过**（038 验收四条全绿）。

#### P0-4 Skills `allowed-tools` 一次性授权
- **来源**：Claude Code skills frontmatter。
- **改动**：skill 声明所需工具列表，调用当轮对这些工具免 L2 确认，下一轮自动失效；与 019/020 的确认缝文档对齐措辞。
- **触发信号**：同一 skill 内重复确认打断率过高（可用确认日志度量）。
- **验收标准**：声明式授权仅当轮有效；未声明工具仍走原确认缝；审计日志记录「授权来源 = skill」。
- **首批改动点**：skill 加载器、`src/agent/tools/spawn.py` 附近确认逻辑。

#### P0-5 记忆 × 执行耦合（执行历史反哺召回）
- **来源**：定位驱动（红队刺 #7 新增；护城河 8.2）；AgentFold 查重机制 + LongHorizon「验证过的经验」（第 9 节④⑤）。
- **改动**：① Run Store 存带标签的执行/失败干预记录（改动类型/失败原因/指标 delta）；② plan 阶段先查重——与历史失败高度相似的方案直接拦截或改向；③ 记忆召回条件跟随当前 plan 上下文。
- **验收标准**：evalkit「相似失败不再重犯」场景通过；反哺数据只经固化管线入记忆（走 P0-7 规则）。
- **首批改动点**：Run Store schema、`orchestrator/assemble.py` plan 注入段。

#### P0-6 自动重试上限 / 无进展检测
- **来源**：LongHorizon-Harness（基线 agent 对无响应弹窗原地重试 400+ 步）。
- **改动**：工具/步骤级重试计数上限 + 无进展检测（连续 N 步状态无变化即停，升人审）；上限可配置。
- **验收标准**：evalkit「卡死场景」在上限内停止并给出升级提示；零无限循环。
- **首批改动点**：`src/agent/orchestrator/loop.py` 重试路径。
- **状态**：✅ 已落地（`c57ef76`）——`loop.py` `_stuck_check`：签名 = 本批全部点菜的（工具名, 参数）序列，与上一批完全相同才算踏步；连续 `CORTEX_STUCK_LIMIT`（默认 3，下限 2）即熔断工具循环 + 发 `stuck` 事件升人审，熔断轮的点菜不入底片；状态是轮级局部（跨轮重复归人管）。熔断轮不再补发 `max_rounds` 噪声。

#### P0-7 固化管线「验证过的经验优先」
- **来源**：LongHorizon（Self-Reflection 给廉价教训，Independent Verification 给可信事实）；032 幻觉污染红线。
- **改动**：固化管线萃取规则修改——有客观背书的经验（测试通过、git 状态、工具验证结果）优先入库；agent 自我总结的教训降级为候选，需二次确认。改规则不改架构。
- **验收标准**：evalkit 固化场景集通过率不降；入库条目「有背书」占比可度量。
- **首批改动点**：`src/agent/memory/consolidate.py` 萃取规则。
- **状态**：✅ 已落地（`c57ef76`）——条目加 `verified` 字段（缺省 false=保守）；审查环节硬校验「verified=true 的背书必须能在材料里找到，找不到就改 false 保留条目」（背书造假比条目失真更危险）；分流规则：project 桶 constraints（教训类）无背书 → 降级为候选不落盘、报告列出待二次确认（decisions/other 与用户级条目不适用——用户拍板与硬事实本身就是背书）；入库行带 `[已验证]` 前缀，「有背书占比」靠 grep 计数事后可度量。

#### P0-8 注入对抗场景集（断言副作用）
- **来源**：SoK Agentic Jailbreak（中间层妥协：最终输出安全但副作用已发生）；刺 #6 升 P0。
- **改动**：evalkit 新增注入对抗场景集（恶意 README / issue / 网页注入 / 确认缝旁路尝试）；**断言副作用而非最终文本**（敏感文件未外发、危险命令未执行）；配套：记忆条目加 provenance（来源/时间）字段、plan 结构化声明工具/数据范围并执行前校验。
- **验收标准**：场景集每个 milestone 必跑；「注入场景通过率」与成功率同列一等指标。
- **首批改动点**：evalkit 场景、`memory/store.py` provenance 字段、plan schema。

#### P0-9 evalkit 外部锚点
- **来源**：刺 #4（自己出题自己考）；037 已立 mini 基线，本条补公开基准。
- **改动**：接入公开基准小样本（SWE-bench Verified 子集或 mini-swe-agent 评测集），每季度锚定；自建场景集划 20% 为冻结集，禁止针对它调机制、只阶段性盲测。
- **验收标准**：锚点一键可跑并出分；冻结集清单入库，机制改动自检声明未触碰。
- **首批改动点**：evalkit 配置与场景目录。

### P1 —— 上下文效率与回滚能力

#### P1-1 Repo map 工具
- **来源**：Aider（tree-sitter 符号/引用图 + 图排序 + token 预算）。
- **改动**：新增 `repo_map` 工具：对整个仓库生成符号级地图，按相关度排序、按 token 预算截断；作为**工具按需调用**，不默认注入 system prompt（保护 prefix cache，见 P2-4）。
- **触发信号**：大仓库任务中检索/读文件 token 占比 > 40%，或定位错误文件导致的返工。
- **验收标准**：evalkit 大仓库场景上，同解决率下总 token 显著下降。
- **首批改动点**：新增 `src/agent/tools/repo_map.py`（tree-sitter 已在 Python 生态可用）。

#### P1-2 轻量快照与回滚
- **来源**：OpenCode SQLite 快照；Aider auto-commit + `/undo`。
- **改动**：在 worktree 隔离（030）之下补一层**会话内**快照：每次 write/edit 前记录逆操作或文件快照，支持 `/undo` 逐步回滚；不写 git commit（保持现有 git 流不干扰）。
- **触发信号**：用户「撤销刚才那步改动」类请求只能靠 git diff 手工恢复。
- **验收标准**：单会话内任意多步 write/edit 可逐步 undo；undo 本身有审计记录。
- **首批改动点**：`src/agent/tools/` 写入类工具包装层。

#### P1-3 LSP 诊断接入
- **来源**：OpenCode。
- **改动**：write/edit 的 post hook（P0-1 槽位）可选接入 LSP 诊断，把编译器/类型错误作为结构化反馈回注；Python 先行（pyright/pylsp），其他语言按配置扩展。
- **触发信号**：P0-1 lint 闸门上线后，类型类错误仍漏出的比例。
- **验收标准**：类型错误在写入当轮即反馈，不再依赖跑测试发现。
- **首批改动点**：`src/agent/hooks/` 新增 lsp 诊断 hook。

#### P1-4 模型分层（architect/editor）
- **来源**：Aider 双模型分工。
- **改动**：`core/gateway.py` / `core/llm.py` 支持按阶段配模型——plan/critic 用强模型，act/工具执行用便宜模型；配置化，不写死。
- **注记（v0.4，AgentFold 裁定）**：高耦合改动（核心 loop、记忆管线）**不做**设计/实现分层——AgentFold 实证高耦合系统里设计者=实现者必须同一人；模型分层只用于低耦合执行轮次。
- **触发信号**：单任务成本构成中执行轮次占比过高。
- **验收标准**：evalkit 同场景解决率不降的前提下，成本可度量下降。
- **首批改动点**：`core/gateway.py` 模型路由配置。

#### P1-5 会话树操作补齐
- **来源**：Pi 的 /tree /fork /clone + compaction 摘要格式（Goal/Constraints/Progress/Key Decisions/Next Steps/Critical Context + readFiles/modifiedFiles 累计）。
- **改动**：① 会话支持 fork/clone（从任意节点开分支重试）；② compaction 摘要采用 Pi 的六段式结构并累计已读/已改文件清单，压缩后agent 不重复读文件。
- **触发信号**：长会话压缩后 agent 重复读已读文件、或「换个思路重来」只能开新会话丢失上下文。
- **验收标准**：fork 会话可独立继续；压缩后首轮重复读取率下降（evalkit 度量）。
- **首批改动点**：`server/app.py` 会话管理、`orchestrator/assemble.py` 压缩摘要模板。

#### P1-6 Sandbox profile 与确认缝正交
- **来源**：Codex CLI（`sandbox_mode` × `approval_policy` 正交）、OpenHands（Docker 默认）、Gemini CLI（trusted folders）。
- **改动**：两个独立旋钮——执行隔离档（none / workspace-write / container）× 确认策略（untrusted / on-request / never）；container 档放 P1 末期，先落地前两档 + 策略矩阵文档化。
- **触发信号**：把 agent 交给非完全信任的任务（如批量处理陌生仓库）时，只能靠确认缝硬扛。
- **验收标准**：策略矩阵文档化；workspace-write 档下越界写入被拦截的测试通过。
- **首批改动点**：`src/agent/tools/spawn.py` 执行路径、配置 schema。

#### P1-7 只读工具并行批（v0.5 编排补遗）
- **来源**：编排层代码评审（2026-09-25）。现保守默认「普通工具串行——模型常期待先读 A 再决定读 B」对**同一轮并列点菜**不成立：模型一轮里同时点了 read A 和 read B，说明决定已做完，无顺序依赖。SWE-agent / Claude Code 均并行只读工具。
- **改动**：`_split_tool_batches` 增加只读名单（`read_file` / `search_code` / `list_dir` 等）：连续只读段与连续 spawn 段一样走线程池并行；写类工具保持串行；结果仍按点菜顺序回填（外部行为不变，冒烟套件把关）。
- **验收标准**：evalkit 多读取场景延迟可度量下降；行为与串行逐字节一致（冒烟全绿）。
- **首批改动点**：`src/agent/orchestrator/loop.py` 切批逻辑。

#### P1-8 spawn 成败判断结构化（v0.5 编排补遗）
- **来源**：编排层代码评审。`spawn_step` 靠 `_FAILURE_PREFIXES` 字符串前缀判断 done/failed——子 agent 结论措辞撞前缀即误判，而内部本有 `RunResult` 枚举。
- **改动**：`spawn_subagent` 返回结构化的 `(status, conclusion)`（或直接透传 RunResult），`spawn_step` 按枚举回写；字符串前缀仅作兼容兜底，过渡期后删除。
- **验收标准**：撞前缀的对抗性结论用例不再误判；现有 spawn 测试全绿。
- **首批改动点**：`src/agent/tools/spawn.py`。

### P2 —— 记忆护城河补全与生态

#### P2-1 记忆补全三件套（v0.4 升 P1）
- **来源**：032 决策已列（tombstone、recall x-ray、异步 reflect）；pi-mem/pond 做格式校准；Fortunate Recall 四态状态字段（第 9 节②）。
- **改动**：① 删除留 tombstone——采用 FR 四态状态字段（在用/被取代/已过期/已撤回）+ 事件时间字段，过期过滤走规则层、不花模型调用；② recall x-ray：每次召回记录「为什么召回这条」，并带**陈旧率**（top-k 中过期/被取代条目占比）；③ reflect 全面异步化，不阻塞执行主轴；④ 压缩/摘要前先落「撤回/取代」状态，防滚动摘要洗平「改主意」。
- **验收标准**：032 中三条验收逐项打勾；记忆操作零阻塞主循环。
- **首批改动点**：`memory/store.py`、`memory/plan.py`。

#### P2-2 记忆块硬上限 + sleep-time 整理
- **来源**：Letta/MemGPT（块上限、会话间离线整理 pass）。
- **改动**：常驻 prompt 的记忆块设 token 硬上限，超限触发离线整理 pass（会话结束后跑，不占在线延迟）；**不**引入 Letta 式「每次记忆操作都花推理」的在线自写循环。
- **验收标准**：常驻记忆 token 有界且可配置；整理 pass 有 evalkit 场景防回归。
- **首批改动点**：`orchestrator/assemble.py` 记忆注入段。

#### P2-3 扩展生态决策
- **来源**：Pi（TS 扩展进进程）vs Claude Code（hooks + MCP 外部进程）。
- **改动**：做决策而非做实现——my_project1 是开放 in-process Python 扩展，还是只开放 MCP/hook 外部接口。建议后者（安全边界清晰，与 P1-6 一致），写成 ADR。
- **验收标准**：ADR 落定，含负决策说明。

#### P2-4 Prefix-cache 友好的 prompt 组装
- **来源**：pi-reasonix（DeepSeek prefix cache 要求 byte-stable 前缀，命中率可 94%+）。
- **改动**：`orchestrator/assemble.py` 输出做稳定性排序——静态内容（system、AGENTS.md 等价物、工具定义）严格前置且字节稳定，动态内容（记忆召回、会话状态）一律后置；记录每轮 prefix 命中率。
- **验收标准**：同会话连续轮次 prefix 命中率可度量并纳入 evalkit 看板；命中率纳入成本回归。
- **首批改动点**：`orchestrator/assemble.py`。

#### P2-5 Headless JSON/RPC 模式
- **来源**：Pi 四模式（interactive/print/JSON/RPC）、OpenHands headless REST。
- **改动**：`server/app.py` 补机器可读输出模式，供外部自动化（含 Kimi Work 的定时任务）驱动。
- **验收标准**：外部脚本可非交互提交任务并拿到结构化结果。
- **首批改动点**：`server/app.py`。

#### P2-6 编排层补遗三小件（v0.5 编排补遗）
- **来源**：编排层代码评审（2026-09-25），均为低风险可维护性/体验改进。
- **改动**：① **取消通道透传子 agent**——`should_cancel` 透传进子 `run_turn`（现状：取消要等子任务跑完一整轮，max_rounds=10 + 慢模型时体感差；代价是定义子会话半截轮 trim 语义）；② **时间戳投影降精度**——`_time_stamp` 从分钟级降为「日期 + 上午/下午/晚间」，为 P2-4 prefix cache 让路（分钟级时间戳插在 payload 位置 1，每轮 invalidate 其后全部前缀缓存；秒级需求本就走 `get_current_time` 工具）；③ **loop.py 继续拆分**——投影装配（`_time_stamp`/`_plan_stamp`/build_payload 调用链）与批执行器（`_split_tool_batches`/`_run_parallel`/`_execute_tool_calls`）各自独立成模块，内核只剩决策循环（S2a 分层方向延续，不改行为）。
- **验收标准**：① 取消子任务在一轮内生效；② 同会话连续轮次 prefix 命中率可度量上升（挂 P2-4 看板）；③ 冒烟套件全绿。
- **首批改动点**：`src/agent/tools/spawn.py`、`src/agent/orchestrator/loop.py`。

---

## 4. ADR 草案（原拟编号 038–042 已作废：037/038 已分别立项为 P0-2/P0-3 正式篇、039 被 S8a 占用；正式立项从 040 起，下列草案内容照用、届时重编号）

### ADR-038：确定性闸门走 hooks 槽位，不走 LLM 判断
- **Context**：质量保证（lint、危险命令拦截）若靠 LLM 自觉，命中率不稳定且花推理成本。Claude Code hooks 证明确定性事件闸门是产品级解法。
- **Decision**：在 tools/registry 增加 pre/post hook 槽位；闸门为纯代码、零 LLM 调用；审计收口 registry。
- **Consequences**：正向——质量反馈当轮闭环、可测试；负向——hook 生态需要治理（数量、顺序、失败策略）。
- **负决策**：不引入 prompt/agent 型 hook（LLM 判闸门）作为默认路径，留作后续评估。

### ADR-039：Run 持久化采用事件 checkpoint + JSONL，不引入图编排框架
- **Context**：长任务恢复需要 durable state；LangGraph 的图模型与 my_project1 现有 loop 架构不匹配，且其 resume 重跑语义有副作用陷阱。
- **Decision**：自有 loop 不变；在 tool/plan 事件后追加 JSONL checkpoint；副作用工具标注幂等性，resume 按标注去重。
- **Consequences**：正向——恢复能力 + 人可读审计；负向——需自行维护 checkpoint 语义，没有框架兜底。

### ADR-040：执行隔离与确认策略是两个正交旋钮
- **Context**：Codex CLI 证明 sandbox_mode 与 approval_policy 分离比单一大开关更清晰；Pi「安全全靠外部容器」不可接受。
- **Decision**：隔离档（none / workspace-write / container）× 确认策略（untrusted / on-request / never）矩阵化；container 档依赖外部环境，不作为默认。
- **负决策**：不做「sandbox 即安全边界」的简化；确认缝（019/020）继续独立于隔离档存在。

### ADR-041：repo map 作为按需工具，不默认注入
- **Context**：Aider repo map 是大仓库上下文效率的最优解，但默认注入会破坏 prefix 稳定性（P2-4）。
- **Decision**：repo map 为 agent 按需调用的工具，带 token 预算；不进 system prompt。
- **Consequences**：正向——上下文效率与缓存命中兼得；负向——agent 需要学会何时调用（skill/提示词引导）。

### ADR-042：记忆层只补三件套 + 块上限，不做 OS 式三层重写
- **Context**：Letta 三层模型完整但在线记忆操作成本高，个人项目用不起；032 方向（JSONL canonical、分层加载、异步 reflect）已正确。
- **Decision**：按 032 补齐 tombstone / recall x-ray / 异步 reflect；常驻块加 token 硬上限 + 离线整理 pass。
- **负决策**：不引入在线自写记忆循环；不引入强制向量库依赖（保持 JSONL canonical，向量只做可选索引）。

---

## 5. 明确不学清单（防稀释）

1. **不学 Pi 把 plan mode / subagents 移出核心**——那是 Pi 的极简哲学，与 my_project1「执行主轴」定位相反。
2. **不接受「安全只靠外部容器」**（Pi 立场）——确认缝 + 隔离档正交是底线（ADR-040）。
3. **不照 Claude Code / OpenCode 功能矩阵平推**——每个竞品只偷一块（见第 2 节）。
4. **不引入 LangGraph 等图编排框架**——只借 checkpoint/resume 语义（ADR-039）。
5. **不做在线自写记忆循环**（Letta 式）——推理成本不适合个人项目（ADR-042）。
6. **不做 Rust/Go 全量重写**（v0.2 追加）——语言不是 agent 项目的瓶颈（瓶颈在 LLM 延迟与工具 I/O），Aider/SWE-agent/OpenHands/LangGraph 均为 Python 的先例在前；全量重写会精准引爆红队刺 #1（摊薄）、#8（烂尾），并丢失 37 篇决策中的隐性知识。分发痛点（刺 #5）用 `uv tool install` / pipx / PyInstaller / 容器镜像解决，成本约为重写的 1%。**Strangler 例外条款**（主核不动，小块用 Rust/Go 以独立二进制长出）：① profiling 证实的真实热点 → PyO3 扩展模块（如 repo map 索引器）；② P1-6 需要 OS 级 sandbox（seatbelt/landlock）→ 小型 Rust sidecar 负责隔离执行；③ 终端 UI 成为主交互面且 Textual/Rich 确实不足 → 只重写 UI 层。
7. **不做 MAS 神经层通信 / A2A / FR 类目本体**（v0.4 追加，详见 9.3）——API 部署形态下神经层（KVComm/TFlow/CIPHER）不可达；单用户进程内多 agent 无需 A2A/ANP；FR 的 10+1 类目本体在 ADR-042 边界外，只取状态字段与陈旧率。

---

## 6. 红队评审（反方意见）与应对方案

> v0.2 追加。本节以挑刺视角审视项目与本 roadmap 本身，每条刺给出可执行的「解」。严重度分档：**致命** / **高** / **中** / **取决于定位**。

### 刺 #1：小不是克制，是摊得薄（高）
- **刺**：0.6 MB 体量塞了至少 6 条产品线（plan、spawn、worktree、确认缝、记忆、图谱、evalkit、Web 面板），每条约 80% 完成度，没有一条到产品级。Aider 用 2.5 倍体量三年只做一件事。
- **解**：**功能冻结 + 完成度收敛**。选定两条线（建议：执行主轴、evalkit）定义「产品级清单」（错误处理、边界 case、文档、测试覆盖），打磨到 100%；其余线只修 bug 不加新功能。每个 milestone 在制品 WIP=1。

### 刺 #2：「个人项目」可能是挡箭牌（中）
- **刺**：37 篇决策文档零外部挑战，所有设计基于「我自己会怎么用」的假设，未经对抗性使用验证；第 1 节的「已有资产」盘点本质是自述而非证据。
- **解**：① 每篇新 ADR 强制「反方意见」小节（本文档即模板）；② 定期用**陌生仓库、陌生任务** dogfooding，记录预期与现实的偏差；③ 关键设计（如确认缝粒度）找机会给外部 reviewer 或社区过一遍；④ 把核心假设改写成可证伪的度量，挂进 evalkit。

### 刺 #3：记忆护城河可能是伪护城河（取决于定位）
- **刺**：JSONL canonical、分层加载、异步 reflect 全是开源公开做法，无架构秘密；知识图谱（S7a/S7b）是重资产，个人助手场景 ROI 存疑——SWE-agent 的证据表明工具反馈设计对成功率的影响大于记忆架构。
- **解**：① 护城河重新定义到**数据与耦合**上：记忆格式与 pi-mem/pond 兼容（可迁移），差异化押注「记忆 × 执行耦合」（执行历史反哺召回、reflect 改写 plan 策略）；② 图谱**暂停新功能**，先用 evalkit 做消融实验（有/无图谱的任务成功率差），让数据决定继续投入还是降级为按需工具。**→ 2026-09-25 已跑（evals/graph_ablation.py，035 有完整结果）：L2 跨笔记题图谱增量成立（词袋 0/4→2/4，BGE 2/4→3/4，两 embedder 同趋势），「降级」选项被数据否掉；检索分诊具备立项数据。**

### 刺 #4：evalkit 有自己出题自己考的嫌疑（高）
- **刺**：自建场景天然偏向「我设计的机制擅长什么」，分数无法与任何竞品横向比较，有沦为自我安慰装置的风险。
- **解**：① 接入公开基准做外部锚点（SWE-bench Verified 小样本，或直接复用 mini-swe-agent 的评测集），每季度锚定一次；② 自建场景集划出 20% 为**冻结集**，禁止针对它调机制、只允许阶段性盲测。

### 刺 #5：分发与硬化是硬伤（中）
- **刺**：Python + venv + 数据目录 + 向量库，别人装不了，未来的自己换机也费劲；对个人助手而言，可迁移性就是数据安全的一部分。
- **解**：先做「换机自举」而非对外分发：一条命令 bootstrap（uv/pipx 安装入口 + 数据目录声明式迁移 + 自检脚本），自己在干净机器上实测一遍；P1-6 的 container 档顺带成为分发答案。

### 刺 #6：安全叙事偏乐观（中）
- **刺**：019/020 是一轮性 hardening，而竞品面对的是持续的提示注入战场（已有论文专门用 mini-swe-agent 做注入攻击实验）；没有持续红队机制。
- **解**：evalkit 增加**注入对抗场景集**（恶意 README、恶意 issue、网页注入、确认缝旁路尝试），参考公开 agent 注入研究用例，每个 milestone 必跑；把「注入场景通过率」列为与成功率同级的一等指标。

### 刺 #7：P0 是跟随逻辑，不是洞察逻辑（高）
- **刺**：P0 四条中三条是「竞品有了所以我也该有」；唯一可能独有的洞察（记忆 × 执行耦合）反而排在 P2。roadmap 被竞品牵着走。
- **解**：① 每条 roadmap 项加标注「定位驱动 / 竞品驱动」，P0 中至少一条必须定位驱动；② 新增 **P0-5：执行历史反哺记忆召回**（定位驱动），P0-4（skill 授权）降为 P1。

### 刺 #8：最大风险是烂尾，不是方向（致命）
- **刺**：单人带宽 × 15 条 roadmap × 每条数周级完成标准 = 大概率 P0 做两条半就失去兴趣。
- **解**：① 定义「**烂尾也值**」最小集 = P0-2（ACI + mini 基线）+ P0-3（checkpoint）——它们同时补「成功率」和「可度量」两块最弱的板；② 每条 roadmap 写入估时，做完一条才准开下一条；③ 每完成一条立即落成正式 ADR，把成果固化进项目史，烂尾也不丢。

### 刺 #9：终局可能是「变成竞品的一个配置」（取决于定位）
- **刺**：当 Pi/OpenCode 扩展生态成熟到可挂记忆层时，my_project1 的独立存在理由会被重新拷问。
- **解**：① 写一页「终局备忘录」，明确三年后目标是独立产品还是某 harness 的记忆扩展——两种答案都合法，但不能不答；② **P2-3（扩展生态决策）提前到 P1**：优先把记忆层 MCP 化、可被外部 harness 挂载。终局若是扩展，这是主动占接口；若是独立产品，MCP 化也不亏。

### 终版优先级（v0.4 收敛，全文以此表为准）

| 优先级 | 条目 | 驱动 |
|---|---|---|
| **必做（烂尾也值）** | P0-2 ACI + mini 基线、P0-3 Run checkpoint | 竞品驱动，但补最弱的板 |
| P0 | P0-1 hooks 闸门、P0-5 记忆×执行耦合、P0-6 重试上限、P0-7 验证过的经验优先、P0-8 注入对抗集（断言副作用）、P0-9 外部锚点 | 混合 / 定位 |
| P1 | P0-4 skill 授权、P1-1 repo map、P1-2 快照回滚、P1-4 模型分层（高耦合不分层）、P1-5 会话树、P2-1 记忆三件套（FR 四态，升）、P2-3 扩展生态/MCP 化（升） | — |
| P2 | P1-3 LSP、P1-6 sandbox 矩阵、P2-2 记忆块上限、P2-4 prefix cache、P2-5 headless | — |
| 暂停 | 知识图谱新功能（消融已跑 2026-09-25：L2 增量成立，检索分诊待立项；新功能仍冻结至分诊结论） | — |
| 不做 | MAS 神经层通信、A2A/ANP、FR 10+1 类目本体（见 9.3） | — |

---

## 7. 下一步

1. 项目所有者审阅本草案 → 决定哪些条立项为正式 ADR；
2. 按第 6 节终版优先级执行：「烂尾也值」最小集（P0-2 + P0-3）打包为下一个 milestone（S8？），每条都带 evalkit 验收场景与估时；
3. 已同步原项目（2026-09-24）：`docs/decisions/037-aci-tool-feedback.md`（P0-2）、`038-run-checkpoint.md`（P0-3）；本文件 2026-09-25 入库 `docs/`；后续立项从 041 起（039 已被 S8a 会话模型占用、040 已被 P0-3 落地篇占用）。

---

## 8. 护城河分析：真的和假的 + 「可插拔 ≠ 产品化」裁定

> v0.3 追加。回答「my_project1 对比竞品的护城河是什么」与「要不要做可插拔记忆层」两个战略问题。与刺 #3（伪护城河）、刺 #9（终局）、P1 的 MCP 化条目（原 P2-3）、ADR-042 交叉引用。

### 8.1 护城河判定（7 Powers 口径）

| 候选护城河 | 判定 | 分析 |
|---|---|---|
| **转换成本** | ✅ 真护城河（主） | 竞品一个周末能抄走架构，抄不走积累的记忆数据——偏好、项目上下文、决策史、工作流习惯。随时间复利：用得越久→越懂你→越离不开→数据更多。唯一「每天自动变厚」的壁垒。 |
| **反向定位** | ✅ 真护城河（副） | 大厂 harness 必须服务中位数开发者，为单用户深度定制（确认缝粒度、记忆治理、个人场景集）在商业上不成立；云端产品结构上拿不到本地全量数据的信任位。n=1 市场是巨头进不来也不屑进的位置。 |
| **独占资源** | ✅ 真（从属于上两条） | 个人数据 + 私有 evalkit 场景集是别人拿不到的资产；场景集是个人的「成功率基准」，公开基准替代不了。 |
| **流程能力** | ⚠️ 半条 | 记忆 × 执行耦合（执行结果反哺记忆、召回跟 plan 走、reflect 改写未来计划）——Pi 系刻意无状态、Letta 系有记忆无执行产品，两头都不占这个耦合点。但目前只是设计意图，未变成数据飞轮，故只算半条。 |
| 网络效应 / 规模经济 / 品牌 | ❌ | n=1 项目均不存在；单人维护反而要警惕规模劣势。 |

**伪护城河清单**（别再拿它们安慰自己）：架构（JSONL canonical、分层加载、异步 reflect，全开源公开做法，复制周期以周计）；知识图谱（是功能不是壁垒，且 ROI 待消融实验验证，见刺 #3）；UX/面板（复制周期以天计）；模型接入（同一批 API）；「开源/本地」本身（只是信任属性，与数据护城河叠加时才成壁垒）。

### 8.2 护城河加深动作（已落回 roadmap）

1. **把「半条」变「一条」**：P0-5（执行历史反哺记忆召回）= 流程能力数据化，每执行一次耦合数据厚一分，竞品结构性抄不走。
2. **反直觉的一手：格式开放，数据私有**。记忆格式与 pi-mem/pond 兼容 + MCP 化（P1）不削弱护城河——数据在自己手里，格式开放让任何 harness 都能挂这份记忆，把「被吞并」改写成「被依赖」。
3. **护城河 = f(连续使用天数)**：最高优先级是「每天用」。任何阻碍日用的 bug、超过 10 秒的启动摩擦，都是护城河漏洞，比任何新功能优先。
4. **护城河指标化**（挂 evalkit）：记忆召回命中率趋势、迁移成本（换 harness 重建上下文需几天）、个人场景集通过率趋势。护城河不能度量就会退化成话术。

### 8.3 终极检验问题

**如果明天 OpenCode 原生支持可插拔记忆层，my_project1 还剩什么？** 合格答案：「我的记忆数据和耦合逻辑当晚就能挂上去，且已积累 N 个月，换任何 harness 都带得走。」答案成立，则竞品每次变强都在为数据护城河抬轿子；不成立，独立产品的故事要重讲。这是 MCP 化（原 P2-3）被红队版提前到 P1 的战略理由（呼应刺 #9）。

### 8.4 裁定：可插拔 ≠ 产品化

「做可插拔记忆层」必须拆成两件事，成本与风险差一个数量级：

- **A. 架构可插拔（现在做，已在 P1）**：记忆层定义清晰接口（MCP 化、格式兼容 pi-mem/pond），任何 harness 通过接口挂载；记忆层仍是个人项目内部组件。买到三样东西：① 迁移自由（harness 可换，记忆资产随身，8.3 的答案从「应该能」变「已经试过」）；② 强制解耦（接口边界逼暗耦合摆到明处）；③ 期权价值（B 的入场券）。
- **B. 产品可插拔（挂触发信号，现在不做）**：独立发布成对外产品/开源项目，承诺 API 稳定、修 issue、写文档。该赛道比「个人执行助手」拥挤得多（Mem0 / Letta / Zep / pi-mem / claude-mem / pond），走 B = 离开 n=1 避风港、进入有融资团队的竞技场，且正中刺 #1（摊薄）、刺 #8（烂尾：单人扛不住对外 SLA）、刺 #2（内部三件套未补齐、未自证价值就给别人用，顺序反了）。

**B 的三个触发信号（齐了再谈）**：① 内部三件套（tombstone / recall x-ray / 异步 reflect）补齐 + 消融实验证明记忆对执行成功率有真实贡献；② 架构可插拔完成，且真有第二个用户来问「能不能挂到我的 harness」；③ 差异化定位一句话说清——cortex 记忆层的差异点不是「存和取」（Mem0 们都在做），而是**治理型个人记忆**：红线治理、append-only vs 可删的删除语义、tombstone、隐私位置敏感（用户级信息不进 repo）、flash 档模型的性价比适配。

### 8.5 对终局叙事的影响

A 做成后，护城河从「数据 + 耦合」升级为「**数据 + 耦合 + 可携带性**」：可携带性看似削弱锁定，实则强化——数据从「被困在一个 harness 里」变成「信任这个记忆层到愿意跟它走任何 harness」。终局故事随之改写：my_project1 的重心从「一个执行产品」变成「**一份长在开放接口上的个人记忆资产，恰好自带一个 harness**」。此形态下，竞品每次变强（更好的模型、更好的 TUI）都是可选项而非威胁。

---

## 9. 论文映射：6 篇论文 × cortex 可用性

> v0.4 追加。来源为 6 篇论文解读（SoK Agentic Jailbreak arXiv:2609.12413 / Fortunate Recall arXiv:2609.10413 / MAS 通信综述 / AgentFold arXiv:2608.26747 / LongHorizon-Harness arXiv:2608.01964 / When to Use Graphs in RAG ICLR 2026），逐篇判定对 cortex 的可用性并落到具体条目。

### 9.1 总览

| 论文 | 主题 | 判定 | 落点 |
|---|---|---|---|
| SoK: Agentic Jailbreak | Agent 安全管道 | 直接用——给注入防御定断言标准 | 刺 #6 注入对抗集 → 升 P0 |
| Fortunate Recall | 记忆生命周期 | 直接用——给 032「条目腐烂」开药方 | P2-1 tombstone 设计升级 → 升 P1 |
| MAS 通信综述 | 多 Agent 通信 | 选择性用——神经层对 API 用户全灭 | spawn 交接结构化，挂 P0-3 |
| AgentFold | 闭环自主迭代 | 改造用——查重机制可借，一处张力需裁定 | P0-5 设计素材 + P1-4 注记 |
| LongHorizon-Harness | 长任务框架 vs 记忆 | 直接用——给 P0-5 定正确姿势 | P0 增补两条便宜项 |
| When to Use Graphs in RAG | 图谱适用边界 | 直接用——给刺 #3 消融实验定设计 | 图谱暂停项的实验设计 |

### 9.2 逐篇映射

**① SoK: Agentic Jailbreak → 注入防御的断言标准。** 核心一刀是「中间层妥协」：最终输出被拦成「抱歉我无法完成」，但读敏感文件、发邮件的副作用已经发生。cortex 骨架已对（确认缝=工具闸门、固化管线审查+硬校验=记忆闸门、registry 审计=溯源底座），缺的是评测口径：**evalkit 注入对抗场景必须断言副作用而非最终文本**（「敏感文件未被外发」而非「回复看起来拒绝了」）。两个便宜动作：记忆条目加 provenance（来源/时间）字段——「不默认 agent 自己生成的记忆天然可信」；plan 结构化时声明工具/数据范围，执行前机器校验（make_plan 人审已是掌舵点，顺水推舟）。→ **注入对抗集升 P0，断言标准照此写。**

**② Fortunate Recall → 032「条目腐烂」的对症药。** 032 已实证条目腐烂（过时行号、时点快照），FR 的四态状态字段（在用/被取代/已过期/已撤回）就是 tombstone 的最小实现，且给了「挑最便宜的先上」顺序：状态字段先行、过期写进规则层（查表，别花模型调用）、跑分加「陈旧率」列、压缩前先抽「改主意」。两个反直觉发现必须进 cortex：① 召回冠军=幻觉冠军（A-MEM 召回 87.6% 同时幻觉率 47%）——recall x-ray 必须带陈旧率；② 滚动摘要会把「撤回的计划」洗成「曾有个计划」——压缩前先落状态。边界：10+1 类目是英文个人助理标定，三桶别照抄本体（ADR-042 已立），只取状态字段+事件时间+陈旧率。→ **P2-1 tombstone 采用四态字段，整项升 P1。**

**③ MAS 通信综述 → 确认取舍，一处小改。** 最有用的是部署形态约束：cortex 走 API 模型，神经层（KVComm/TFlow/CIPHER）全部不可用，省去研究纠结。选型矩阵「个人助理 = MCP + 结构化 JSON + 黑板」恰是 cortex 现状（MCP 已做 009、plan/Run Store 即黑板）。一处改进：spawn 步骤间交接从自然语言走向结构化 schema（步骤产物带类型和校验），与 P0-3 checkpoint 内容 schema 是同一件事，顺手做。A2A/ANP：单用户进程内多 agent，YAGNI。

**④ AgentFold → 一个该借的机制 + 一个要裁定的张力。** 该借**查重员**：新方案先对结构化失败档案做相似度检查，「三个月前几乎一样的方案因 X 失败」直接拦截——这是 P0-5 可长出的具体形态（Run Store 存带标签的失败干预：改动类型/失败原因/指标 delta，plan 阶段先查重）。要裁定的张力：AgentFold 实证「设计者=实现者必须同一人」（高耦合系统 mismatch 代价极高），与 P1-4 architect/editor 模型分层相反——**裁定：高耦合改动（核心 loop、记忆管线）不分层，模型分层只用于低耦合执行轮次**，写进 P1-4 注记。另：「Critic 只排序不评分」为 026（adversarial-critic-deferred）的解冻条件定了设计原则；「批量复盘」验证退出复盘的既有选择。

**⑤ LongHorizon-Harness → 给 P0-5 定正确姿势 + 两条便宜 P0。** 核心判词：Self-Reflection 给廉价的教训，Independent Verification 给可信的事实——「失败后复盘写进记忆」继承 Reflexion 无客观依据的毛病，验收台账里提炼的才是「经过验证的经验」。cortex 映射：① **固化管线加规则——优先萃取有客观背书的经验**（测试通过、git 状态、工具验证过的结果），agent 自我总结降级为候选——直接回应 032 幻觉污染红线，是 P0-5 的正确姿势；② 文章给桌面编码 agent 的三样轻量借法中 cortex 缺一样：**自动重试上限/无进展检测**（原地重试 400 步的弹窗案例）——便宜，进 P0；③ 进度写成文件 ≈ P0-3 checkpoint，互相验证。「能力是模型×框架的系统属性」（中等模型+好框架反超顶级模型+裸框架）写进 evalkit 信念栏——flash 档路线的理论支撑。

**⑥ When to Use Graphs in RAG → 刺 #3 消融实验的设计书。** 结论：L1 事实检索 GraphRAG 反而输（图带来噪声），L2+ 多跳推理明显赢——图是专科医生不是升级版。cortex 图谱消融实验（红队版「暂停图谱新功能，待消融数据」）按此设计：**evalkit 场景按 L1/L2/L3 分级，预期图谱 L1 输、L2+ 赢；若 L2 也不赢，图谱降级为按需工具**。检索分诊（事实走向量/FTS、多跳走图）与 028 场景路由同一机制，与 FR 的检索分诊互证。

### 9.3 行动差量（在 v0.3 roadmap 上的新增/变更）

| # | 动作 | 来源 | 优先级 |
|---|---|---|---|
| 1 | 自动重试上限 / 无进展检测 | LongHorizon | **P0 新增**（半天级） |
| 2 | 固化管线「验证过的经验优先」规则（改规则不改架构） | LongHorizon | **P0 新增** |
| 3 | 注入对抗场景集：断言副作用而非最终文本 | SoK | **升 P0**（原刺 #6 P1 项） |
| 4 | tombstone 采用 FR 四态状态字段 + evalkit 加陈旧率列 | Fortunate Recall | **升 P1**（原 P2-1） |
| 5 | P1-4 注记「高耦合改动不做模型分层」；P0-5 吸收查重机制 | AgentFold | 注记 + 设计素材 |

**明确不做**：MAS 神经层通信（KVComm/TFlow/CIPHER，API 不可达）、A2A/ANP（单用户进程内，YAGNI）、FR 的 10+1 类目本体（ADR-042 边界）。图谱消融实验按 L1/L2 分级，原样保留在「暂停」栏等数据。
