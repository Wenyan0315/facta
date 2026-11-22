# Facta M4 真机验收报告（ADR 118：宿主与发布 / Facta 2.0 收官）

> **一句话结论**：ADR 118「待验收」除「发布 tag+Release」（p11，按约定执行前须用户确认）外全部通过——钉子①委派全生命周期（确认 kind 链路、计划人审、steering、保险丝、resume、cancel）API 侧全链路取证完毕，VS Code 壳内微链路（面板内委派→确认→批准→账单→状态栏）真机取证完毕（§3.4）；Brief 双轨对照过且**把「write_file repo_root 不生效」从实现 bug 修正定性为 ADR 117 §4 的明裁行为**；五跨越自检表成稿；无阻断项，观察项四条均有处置或触发信号。

## 验收环境与口径

- 日期：2026-11-22（ADR 118 落地 M1–M4 均在 2026-11-20～22 完成；M4 实现 PR #70 squash 合并 = `331e210`）
- 代码：main = `331e210` 起，验收期工作树仅含 p8 材料刷新（README / docs/architecture.md，随本报告 PR 提交）；`data/graph.json`（M）按惯例永不 add；`data/notes/robustness_patrol.md`、`data/notes/test_suite_review.md` 两个未跟踪文件刻意不 add
- 服务：`FACTA_PROVIDER=deepseek FACTA_SERVER_PORT=8000 .venv/bin/python -m facta.server` 启动于仓根（**8000 端口＝VS Code 扩展缺省 serverUrl**，面板验收口径）；保险丝探针任务用 `FACTA_TASK_MAX_CONTINUATIONS=2` 逼出 `blocked`（首轮服务）；20:08 起因观察项 O2 重启过一次（详见「异常与处置」）
- 三门：PR #70 CI **五 job 全绿**（`agent`＝ruff+mypy pass、`test` 3.11/3.12 双 pass、前端产物比对闸、`vscode` job＝`npm ci && npx tsc -p . --noEmit`）；本地全量 **1009 passed / 7 skipped**（`test_mcp_http.py`、`test_mcp_config.py` deselect——本地代理 502 环境性预存失败，与 M3 同口径）
- 验收探针：`data/skills/wprobe.md`（`# wprobe` H1 头格式、write 档、budget 6、settle skip；**验后已删**）；探针仓 `/tmp/facta-accept-20261122/proj_w/`（`PLAN.md` 标记 `PROJ_W_MARKER_a4f1c9e2`）；双轨稿存 `/tmp/facta-accept-20261122/brief_{manual,generated}.md`
- 口径：API + SSE 事件流取证为主（`curl -sN --max-time N` 截流）；VS Code 壳经 Computer Use（TRAE-computer-use-ptc 规程，macOS 辅助功能授权 + 应用级动作授权弹窗逐次确认）；`POST /api/runs/{id}/confirm` 字段 `{"approve": true}`
- 发布素材：`vscode/facta-2.0.0.vsix` 已 `npx vsce package` 打包（7 files，13.66KB；WARNING：缺 repository 字段与 LICENSE，p11 前不阻断，记观察项 O5）并 `code --install-extension` 装入真机 VS Code（替换 0.1.3）

## 待验收清单逐项结论（对应 ADR 118「待验收」）

| 项 | 结论 | 真机证据 |
|---|---|---|
| ①三门 + CI 五 job 绿 | **过（CI）** | PR #70 五 job 全绿（1m21s–5m11s）；本地全量 1009 passed / 7 skipped（`test_skills_api.py`、`test_confirm_kind.py`、`test_task_api.py`、`test_version.py`、`test_browser.py` 全链路等 M4 新增覆盖在内） |
| ②Brief 双轨对照（116 遗留，118 §8） | **过** | 本报告 §4 全文：四维度对照结论**通过**，且产出定性级发现（O1）；账目不一致（116 已勾 vs 117/118 称未做）随 §4 收口 |
| ③钉子①：VS Code 壳内任务委派全生命周期 | **过（API 侧 + 壳内微链路）** | API 侧：三任务 B/P/S 全链路（本报告 §3.1–3.3，确认 kind 双路、计划人审、steering、保险丝 blocked、resume 两次、cancel、账单端点全覆盖）；壳内：命令面板 `Facta: 打开任务面板`（`facta.openTasks`）真机执行成功、webview 面板打开（窗口标题「Facta · 任务」、加载 `shellHtml`→`/tasks?id=…`，服务日志可见 `GET /tasks?id=0b1411ea… 200` + `/api/tasks`/`/api/runs`/`/api/skills`/`/api/projects` 轮询 200）；面板内点击微链路（委派→待确认→面板内批准→done→查账单→状态栏）2026-11-22 真机补测完毕（§3.4，steering/cancel/resume 面板按钮为可选件未实操，功能面由 Playwright+API 覆盖） |
| ④钉子②：材料核对 + 五跨越自检表 | **过** | README/architecture 已按钉子②刷新（p8，随 PR #70 后的材料 PR 入库）：七面板/七入口/118 份 ADR/1000+ 用例（实测 1009）/五跨越特性小节/已知边界表逐项对上；五跨越自检表见 §5，p11 进 Release notes |
| ⑤Playwright 用例 | **过（CI）** | `tests/test_browser.py` 委派全链路（mock 服务起 Web 侧委派→202→done）在 `test`×2 job 绿 |
| ⑥版本单点 | **过** | `GET /api/status` 实测 `{"version":"2.0.0",…}`；`vscode/facta-2.0.0.vsix` 包名/`package.json` 2.0.0；`test_version_single_source_of_truth` 钉住 `pyproject` dynamic + `facta/__init__.py.__version__`；README「已知边界」注明不建 CHANGELOG（变更史真值源＝ADR + Release notes） |
| ⑦确认 kind 链路 | **过** | 双路取证：`write-auth`（任务 B：`confirm.request kind="write-auth"` → approve → 二写免停，tool.result 带「命中规则：write-auth」）与常规 `kind:""`（任务 P：`make_plan` 人审、`run_command` 常规确认）；聊天页确认卡 kind 文案随 p4 落地（`confirmKindLabel`） |
| ⑧零回归抽查 | **过** | 聊天页 `/` 200（确认卡 `confirmKindLabel` 文案随 p4）；IDE 信箱 chip/RunDetail 计划面板由 Playwright 全链路 + 前端产物闸覆盖（M3 行为不回归）；`facta.open`/`facta.attachContext`/`facta.openTasks` 命令齐在 vsix 贡献点（`openTasks` 真机执行过，§3.4）；`/api/tasks*` 六端点契约真机全覆盖（list/create/get/resume/cancel/billing）；`POST /api/runs/sync` → `{"text":"CHAT_OK"}`；主 agent `write_file` 免确认直写逐字节不变（任务 S 的 s1–s5 相对路径写全程 `confirm.request=0` 顺带复证） |
| ⑨发布 tag v2.0.0 + 首个 GitHub Release | **待 p11（按约定执行前须用户确认）** | 素材已齐：五跨越自检表（§5）+ 已知边界 + 快速开始三段（README §「委派第一个任务」）+ `vscode/facta-2.0.0.vsix` 附件 |
| ⑩2.0 收官自检 | **过（表已成稿）** | §5 五跨越逐条自检：四跨越达成、宿主跨越本期兑现；未兑现项（hooks 等）如实标注不宣称，随 p11 进 Release notes |

## 钉子① 全链路证据

### 3.1 任务 B（wprobe 技能 / write 档：确认 kind + 免停 + 账单）

- 任务 `8f2a0567`（repo_root=探针仓，授权缺省＝技能声明 write）→ run `c0f7037c`：
  `confirm.request {"kind":"write-auth",…}` → `POST /api/runs/c0f7037c/confirm {"approve":true}` → 200 → 首写落盘 → **二写全程无第二个 confirm.request**（tool.result 带「命中规则：write-auth」= 档位置位记录）→ done。
- 终态字段：`authorized_write=true`、`fuse_fired=false`、`continuations=0`；探针仓落 `a.txt`/`b.txt`＝`A_M4_OK`/`B_M4_OK`，`PLAN.md` 标记未动（写围栏换锚零回归）。
- 账单端点 `GET /api/tasks/8f2a0567/billing`：2 calls / ¥0.001621。

### 3.2 任务 P（计划人审 + 计划范围闸 + 保险丝 + resume）

- 任务 `1e4f85b1`（三步计划：写 p1/p2 + 一步越权）→ 7 个 run 的完整续跑链：
  1. run `059cd936`：`make_plan` 人审 `confirm.request kind:""` → approve → 计划立起；
  2. 计划范围闸**拒** `mcp__write_local_file`（步骤 tools 未声明该工具，`spawn_step`/执行路径拒收）；
  3. 续跑链触发保险丝：`blocked: 续跑次数达上限（2）`（`FACTA_TASK_MAX_CONTINUATIONS=2` 探针口径）；
  4. `POST /api/tasks/1e4f85b1/resume {"text":…}` → 新 run（**保险丝复位**）→ 再次续跑→再熔断 blocked；
  5. 二次 resume「一口气做完」→ done，`p1.txt`/`p2.txt` 落地探针仓。
- `run_command` 常规确认同样取证到 `kind:""` → approve → 执行。
- 账单：33 calls / ¥0.1916；终态 `fuse_fired=false`（resume 重置基线，与 M3 语义一致）。

### 3.3 任务 S（steering 改写计划 + 保险丝 blocked + cancel）

- 任务 `db1fcca2`（慢任务：分五次写 s1–s5）→ run `41667db6` 执行中 `POST /api/runs/{id}/steer {"text":…}` → **202**；
- steering 生效证据：计划被改写插入 `extra.txt` 步骤且被执行（run `7a5635dd`/`af597e35` 事件流可见改写后步骤落地）；
- 续跑撞保险丝 → `blocked`（`fuse_fired=true`、`continuations=3`）→ `POST /api/tasks/db1fcca2/cancel` → **cancelled** 成功；
- 账单：20 calls / ¥0.1129。全库任务终态无遗留 queued/running。

### 3.4 VS Code 壳内证据（Computer Use）

- **已取证**：命令面板路径（View 菜单→Command Palette→键入「打开任务面板」）真机执行 `facta.openTasks` 成功——窗口标题转为「Facta · 任务」，webview 加载 `shellHtml` 注入的 `iframe src=/tasks?id=…`，服务日志确认面板页/产物/五个 API 轮询（tasks/runs/skills/projects/status）全 200；扩展为 `facta-2.0.0.vsix` 实装（命令标题「打开任务面板」为 2.0.0 专有，0.1.3 无此命令，可反证装的是新版）。
- **面板内点击微链路（2026-11-22 补测完毕，真机全链）**：聊天面板「任务视图」链接进 `/tasks` → NewTaskForm 四要素提交（goal=「用 write_file 写文件 shell_ui_probe.txt 内容 SHELL_UI_OK，写完后回 PROBE_DONE。」；技能=wprobe (write)；项目=自定义路径…→repo_root=/tmp/facta-accept-20261122/proj_w；授权档位=write（技能声明））→「委派」→ 新任务卡渲染（运行中 + 技能/档位/仓 chips）→ 任务挂 `waiting-approval`（「待确认」徽章）→ 展开任务卡见 ConfirmCard（「需要你的确认 / 工具：write_file / **本任务写授权：批准后本任务后续写操作免停。**」+ 参数 JSON）→ 面板内点「批准」→「本任务写已授权」chip 上卡 → 任务 **done**（任务 `fcf2f3f5…`，`authorized_write=true`，`continuations=0`；探针文件 `shell_ui_probe.txt`=`SHELL_UI_OK` 落盘）→「查看账单」内嵌展开（合计 2 次调用 / ¥0.0012，与 `GET /api/tasks/{id}/billing` 逐字段一致）→ 状态栏计数取证（`Facta 任务：⟳ 运行中　⏸ 待确认（点击打开任务面板）`，批准后计数归零）。证据：窗口级截图 `/tmp/confirm_card.png`、`/tmp/billing_evidence.png` + AX 元素序列 + API 复核。
- **实测踩坑（如实记录，供后续 CU 验收复用）**：①CU 截图有帧滞后，窗口被遮挡时渲染冻结但 AX 树实时——地面真值用 `screencapture -l <窗口>` 窗口级截图；②原生 `<select>` 弹出层不吃坐标点击，正解=点击打开弹层→`press_key down/end`→`return` 键盘选定；③`press_key` 不带 element_id 跟**系统前台**走，用户点击会抢前台吞键（需用户让出前台数分钟）；`type_text` 带 element_id 走 AX 定向投递不受前台影响；④屏幕录制授权后 `check_permissions` 仍报 denied，以实际出图为准。
- **未覆盖（可选件，如实标注）**：steering/cancel/resume 的**面板按钮**实操未做（核链路已全取证）；三者功能面由 ⑤Playwright 全链路 + §3.1–3.3 API 侧证据覆盖，不阻断。
- 顺带观察：macOS 下 Computer Use 操作 VS Code 每类动作首个调用会弹**应用级授权对话框**（超时 300s 无人点则动作失败，"Request timed out"），人工点允许后持久化；`screenRecording` 缺失仅影响截图不影响语义树。VS Code 检测到 AX 客户端后自动进入「Screen Reader Optimized」模式。

## Brief 双轨对照（118 §8 / 116 遗留验收）

- **口径**：真实评审对象＝「修复主 agent write_file 的 repo_root 不生效」（p9 验收观察项转正的真任务）。同一时点独立写两份：手工轨（起草者凭调研，[brief_manual.md](/tmp/facta-accept-20261122/brief_manual.md)，5950 字节）与生成轨（`POST /api/runs` mode=brief，run `ce974e1e`，12 轮预算满 / 25 次工具调用 / 约 90s / 5806 字符，[brief_generated.md](/tmp/facta-accept-20261122/brief_generated.md)）。生成轨盲跑（不看手工稿）；首跑 `dc98426c` 撞 O2 模型降级（输出为 mock 兜底），**作废重跑**。
- **四维度对照**：

| 维度 | 手工轨 | 生成轨 | 判定 |
|---|---|---|---|
| 引用真实性 | 5 处引用逐条核真（115 `repo_root` 只登记注释、116 §3 归属分流、117 实现注记 2、052 黑名单、files.py 围栏行号） | 6+ 处引用逐条核真（`assemble.py:390` 注释与 `skills.py:198` `replace(mother_ctx, workspace_root=repo_root, …)` **逐字对上**；116:188 负决策、117 §4「主 agent 任务维持 WORKSPACE_ROOT 锚点（零回归）」原文、111 锚点缝）；无编造；`build/lib` 旧副本甄别正确 | **两轨全真**；生成轨行级定位更准 |
| 相关性 | 答了链路定位+修复方案+回归面，但把问题定性为 bug | 定位到同一链路且多找到 `_resolve_repo_root` chat 恒 None 断点；**推翻输入句前提**——按 117 §4 属明裁行为，修复＝修订 117 负决策须走 ADR，给出方案 A/B 分叉 + 六条待拍板 | **生成轨占优**（抓住定性层面张力） |
| 过时判定 | 引用均现行有效，但漏引 117 §4 现行裁定，结论方向与现行裁定冲突 | 显式区分 116「M2 不接」（已被 117 §4 兑现取代）与 117 §4 现行裁定，判「无任何已裁定的『主 agent 也换锚』条文」 | **生成轨占优** |
| 长度与耗时 | 约 2000 字（5950 字节）；调研+成稿约 6–8 分钟 | 5806 字符（含约 600 字符过程旁白）；约 90s、25 次工具调用 | 生成轨约 2.5×长、耗时更短；长在逐条原文引用+待拍板清单，未见凑字 |

- **对照结论：通过**——引用两轨全真；生成轨在相关性与过时判定上占优（117 那次对照是手工轨过时判定占优，本轮反向，印证两轨不可互替）；长度/耗时代价合理。与 113 定位一致：**Brief 供事实底座、决定归人裁**——本轮连「问题定性」都回到人裁（是否修订 117，见 O1）。
- **副产物（定性级发现，记 O1）**：p9 观察项「write_file repo_root 不生效」经生成轨的 `⚠️ 判定提示` 与人工复核 117 §4 原文（「技能任务带 repo_root 时……主 agent 任务维持 WORKSPACE_ROOT 锚点（零回归）。读工具 M2 已锚，此处补齐写侧」）确认为**明裁行为而非实现走样**。
- **账目收口**：ADR 116「真机双轨例」已勾（注记称 2026-11-17 于 117 评审时兑现），但 117 待验收注记与 118 §1 现状表均称「未覆盖/未做」——三处账目不一致（O3）。本期以 118 §8 权威口径**重做一次**收口：113/116/117 待验收随本报告 PR 回写，118 §1 现状表同 PR 修正。

## 五跨越自检表（114「五个跨越」→ 2.0 发布自检，114 待验收第 2 条）

| 维度（114） | 1.0 | 2.0 承诺 | 自检 | 证据 |
|---|---|---|---|---|
| 交互形态 | 一问一答 | 任务委派：后台长任务 + 多任务并行 | **达成** | `POST /api/tasks` 委派→后台推进（M3 队列/并行/续跑）；M4 任务面板五块（委派/队列/确认/账单/项目设置）；真机三任务并行终态干净 |
| 控制模型 | 取消 或 等完 | 打断纠偏（steering，不杀 run）+ 分档授权 | **达成** | 任务 S steering 202→计划改写落地（run `7a5635dd`/`af597e35`）；授权档位三档（技能声明+任务可收严），确认卡 `kind` 分流（§3.1/3.2） |
| 知识模型 | 全局一份 | 按归属四层（任务/项目/用户/全局）+ 可见性矩阵 | **达成**（M2/M6.5 落地，M4 接上任务） | 115 §1 `repo_root` 从「只登记」接上消费（learned 注入/固化按归属分流）；M2 归属隔离与默认最小可见；用户级 `~/.facta/user.md` 面板第四栏 |
| 能力定义 | 硬编码 mode | skill 数据化（`data/skills/*.md`），零代码加工作流 | **达成** | `data/skills/` 文本即技能（brief/review 迁移为首批，双轨生成轨即 brief 技能真机产物）；wprobe 探针技能零代码热插（运行时装配，M3/M4 两期沿用）；`GET /api/skills` 带档位 |
| 宿主 | CLI + Web 页 | VS Code 壳为一等宿主（任务面板 + 命令） | **本期兑现** | `facta-2.0.0.vsix`：`facta.openTasks` 命令+webview 任务面板+状态栏计数（§3.4）+信箱（`read_mailbox`）；CI `vscode` job 闸编译 |

- **未兑现项如实标注（不宣称）**：hooks/插件机制（114 §116 行：in-process 插件沿 079，hooks 触发信号部分兑现——**单开案**，不在 2.0 范围宣称）；Run 明细不持久、20s 轮询粒度、不上 marketplace、无 CHANGELOG（README「已知边界」表已列）；steering/cancel/resume 面板按钮未实操（可选件，§3.4；核链路已真机全取证，功能面由 Playwright+API 覆盖）。

## 钉子② 材料核对记录

| 材料项 | 要求（118 钉子②） | 核对结果 |
|---|---|---|
| README 版本口径 | ADR 计数 115→118 | ✓ 两处（头注 + 文档导航）均为 118 |
| 面板清单 | 任务面板五块 + 七面板 | ✓ 界面一览/任务面板职责改写（委派/队列/确认/账单/项目设置）|
| 宿主条目 | VS Code 壳一等宿主 | ✓ 「CLI + Web 双壳 + VS Code 宿主」→七入口 + openTasks + 状态栏计数 |
| 2.0 特性面 | 五跨越可见 | ✓ 新增「Facta 2.0 五跨越」小节（各带 ADR 链接）+ 快速开始「委派第一个任务」（curl 示例）|
| 已知边界 | 不宣称未兑现 | ✓ 「已知边界 / Known limits」表（Run 明细不持久/20s 轮询/不上 marketplace/无 CHANGELOG）|
| 用例数 | 现状如实 | ✓ 700+→1000+（实测 1009 passed） |
| architecture.md | 随里程碑迭代 | ✓ v0.134：交互层七面板+VS Code 壳、server 段 M4 增补、决策索引 [118]、里程碑表 Facta 2.0 M1–M4 ✅ 收官行（五跨越）、待讨论项 118 §1 三分界线追认、活清单补 117 验收观察 5/6 两条 |

## 发现的异常与处置

| # | 现象 | 定性 | 处置 |
|---|---|---|---|
| O1 | 主 agent（无技能）任务 `write_file` 的 `repo_root` 不生效：绝对路径被拒「路径越界」、相对路径落 facta 仓根；技能任务同款可写（p9 实测） | **非实现 bug，是 ADR 117 §4 明裁行为**（「主 agent 任务维持 WORKSPACE_ROOT 锚点（零回归）」），经 §4 双轨对照定性 | 不在本期改码。若要让主 agent 也锚 repo_root＝修订 117 负决策，挂**触发信号**（外部试用者被绕乱、或主 agent 多项目并行冲突时立案走 ADR）；连带口径（绝对路径收不收、read/search/list 同族、terminal cwd）见手工轨 §6 待拍板 |
| O2 | 长跑服务进程模型调用劣化：20:00 起 `openai._base_client Retrying request`×6/模型 → 降级链 deepseek→siliconflow→mock 全失败；同时段**新进程同款调用正常**（直调 `deepseek-flash` 成功、curl 正常）→ 重启服务即自愈 | 运维级（进程内连接状态劣化疑似 FD/连接池陈化；非模型通道故障——降级链行为本身符合设计，「真模型暂时不可用」mock 兜底如实） | 已重启自愈（20:08 起服务正常，`CHAT_OK` 探针过）。挂**触发信号**：同一进程内再次复现即立案排查网关连接管理；报告如实记录降级首跑作废 |
| O3 | 账目不一致：ADR 116 勾注「双轨例 2026-11-17 于 117 评审时兑现（结论通过）」，但 117 待验收注记称「Brief 双轨对照仍未覆盖，随 M4 一并做」、118 §1 现状表称「未做」 | 账面失同步（117 评审记录节确有双轨全文与四维度结论；116 勾注引用属实） | 本期按 118 §8 权威口径重做一次双轨（§4）收口；113/116/117/118 账目随本报告 PR 回写修正 |
| O4 | Computer Use 通道四坑（实测全记录 §3.4）：①动作类调用先弹应用级授权对话框（300s 超时无人点即失败）；②`press_key` 无 element_id 跟系统前台走，用户点击抢前台即吞键（需用户让出前台）；③CU 截图帧滞后/被遮挡窗口渲染冻结（AX 树实时，窗口级 screencapture 为地面真值）；④原生 select 弹层不吃坐标点击（键盘选定正解） | 环境/工具面限制，非产品缺陷 | 面板内微链路 2026-11-22 补测完毕（§3.4）；剩余可选件=steering/cancel/resume 面板按钮实操（功能面已由 Playwright+API 覆盖）；不改产品码 |
| O5 | `vsce package` WARNING：vscode/package.json 缺 `repository` 字段、缺 LICENSE | 发布素材完备性小缺口，不阻断 vsix 安装使用 | p11 发布前顺手补 `repository` 字段（LICENSE 仓内已有 MIT，vsix 内未带——`vsce` 可 `--skip-license` 或补 `license` 字段，p11 一并处理） |

## 成本账单（M1 起「每单挂账」纪律延续）

本期验收新增账单（2026-11-22）：

| 任务 | 内容 | LLM calls | 成本 | 备注 |
|---|---|---|---|---|
| `8f2a0567`（任务 B，wprobe） | 确认 kind + 二写免停 | 2 | ¥0.001621 | `authorized_write=true` |
| `1e4f85b1`（任务 P，计划） | 人审 + 范围闸 + 保险丝×2 + resume×2 + 收官 | 33 | ¥0.1916 | 7 个 run 续跑链 |
| `db1fcca2`（任务 S，steering） | steer 改写 + blocked + cancel | 20 | ¥0.1129 | `fuse_fired=true`、`continuations=3` |
| run `ce974e1e`（brief 生成轨） | 双轨对照 | 12 轮预算满 / 25 工具调用 | run 级无账单端点（113 已知），约 90s | 见 §4 |
| run `dc98426c`（作废）/ 3 个 chat 探针 | O2 取证 | — | 极小 | 降级/探活用，不计验收成本 |

- 存量复核：全库 41 任务终态干净（done/failed/blocked/cancelled，无 queued/running 遗留）；M1–M3 账单口径延续（任务级 billing 端点 + usage ledger）。

## 单测覆盖但真机未复现的项

- 60s 巡检兜底（队列「错过事件」场景真机不可造）——M3 已由 `tests/test_queue.py` 钉，本期沿用。
- `vscode` job 的 `tsc --noEmit` 编译闸在 CI 承担，本地 vsix 打包时同产物编译通过（`vscode/out/extension.js` 由 `npm ci && npx tsc -p .` 产出）。

## 清理与复核

- 已删：`data/skills/wprobe.md`（探针技能）；仓根污染文件 `extra.txt`/`s1.txt`/`s2.txt`（O1 实测副产物，已 rm，git status 干净）。
- 保留：`/tmp/facta-accept-20261122/`（探针仓 + 双轨稿 + 事件流取证，供复核）；8000 端口验收服务视后续需要决定关停。
- 不 add：`data/graph.json`（M）、`data/notes/robustness_patrol.md`、`data/notes/test_suite_review.md`（刻意不入库）。
- 版本单点复核：`/api/status` 2.0.0 = vsix 2.0.0 = `facta/__init__.py.__version__` = `vscode/package.json` 2.0.0（`test_version_single_source_of_truth` + 扩展侧常量，CI 双 job 钉）。

## 验收结论

**ADR 118「待验收」除⑨发布（p11，执行前须用户确认）外全部通过。** 无 P0/P1 阻断项；观察项 O1–O5 均已定性并有处置/触发信号。Facta 2.0 M1–M4 五跨越：四跨越达成、宿主跨越本期兑现，未兑现项（hooks 等）如实标注。可进入 p11 发布（tag v2.0.0 + 首个 GitHub Release + vsix 附件）。
