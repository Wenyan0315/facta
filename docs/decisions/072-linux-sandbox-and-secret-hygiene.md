# ADR 072：Linux 沙箱后端与 bot 场景的秘密卫生

- 状态：**已拍板（丁案 #22 落地；甲案 ubuntu-22.04 路线落地）**
- 日期：2026-10-02
- 立项出处：#15 复盘后的优先级梳理（P1）——facta-bot 跑在 ubuntu CI 上，而 048 的
  seatbelt 是 macOS 专属；`detect_backend()` 在 CI 恒返回 None，`wrap_command` 诚实降级
  为 `off`——**bot 会话的写围栏比开发机薄一层，且这是 bot 的日常运行态不是边缘态**
- 关联：048（seatbelt 甲案平台派发）、049（读侧凭证围栏）、052（记忆写围栏）、
  #15 ③（评论触发鉴权已修）、#16（提交白名单闸门）、057（plan-scope）

## 背景与动机

048 留下的原话：「Linux/Windows 后端挂触发信号，检出也不假装支持」。彼时 bot 还不存在，
降级面是「开发者在非 macOS 机器上自己跑」——人会看弹窗，风险有兜底。

#15 之后这个前提变了：bot 在 ubuntu runner 上独立改代码、开 PR，且触发面已对
`agent` label 开放。在 CI 上 `run_command` 的防线只剩两层：应用层白名单
（terminal.py）+ 确认闸门（bot 场景 = auto-deny）。**进程级写围栏这一层是缺的。**

## 威胁模型（bot 场景逐项过）

1. **复合/危险命令**：白名单 + auto-deny 已覆盖（#15 ④ 的行为不确定已由 #16/#20 收敛）。
2. **写越界**（改 `.github/workflows/`、`.git/hooks`、记忆文件、workspace 外路径）：
   白名单内没有写命令，写必须经确认 → auto-deny。**残余面**：`python -m pytest`
   是免确认的，测试代码本身可写任意路径——注入若能让 bot 写出恶意测试再跑 pytest，
   写围栏缺位就是真缺口。概率低但路径存在。
3. **秘密进上下文（本 ADR 认为最实的一条）**：runner 进程 env 里有
   `BOT_TOKEN`（仓库写权限 PAT）与 `DEEPSEEK_API_KEY`。`run_command` 的子进程
   **原样继承全量 env**——`cat /proc/self/environ` 或 `cat /proc/1/environ`
   是白名单免确认命令（不匹配 049 的 `.env` 正则），一次调用全部秘密进模型上下文。
4. **外发**：秘密进上下文后的出口——写进补丁文件混进 PR（#16 提交白名单已收窄到
   `src/ tests/ docs/ evals/`，但 `docs/` 里的文字同样公开）、写进 PR 正文/评论、
   编进 `web_search` 的 query（搜索 provider 是第三方）。**网络维度的硬隔离不在
   本 ADR 范围**（与 048「网络另案」同款裁定）。
5. **「runner 是一次性 VM」论为什么不成立**：VM 一次性，秘密不一次性。
   搞坏 runner 无人在意，偷走 PAT 与 API key 是真实损失。

## 选项

### 甲：bubblewrap 后端（推荐，挂进 048 的平台派发层）

`bwrap` 是 util-linux 生态的用户态沙箱（flatpak/chrome 同款底座）：
ubuntu runner `apt-get install bubblewrap` 一行可得。语义可对齐 seatbelt profile：

```
bwrap --ro-bind / / \
      --bind <workspace> <workspace> \
      --bind /tmp /tmp \
      --tmpfs <workspace>/.git/hooks（或 --ro-bind 空目录遮蔽） \
      --ro-bind <空文件> <workspace>/.env（遮蔽读，049 对齐） \
      --die-with-parent --unshare-pid \
      /bin/sh -c <command>
```

- 落地形态与 048 同构：`detect_backend()` 加一支（`sys.platform == "linux"` 且
  `shutil.which("bwrap")` **且空命令试跑成功**——探测制本来就是「检出也不假装支持」，
  试跑失败诚实降级 off）。
- **已知不确定项（实现期必须实测）**：Ubuntu 23.10+ 用 AppArmor 限制非特权
  user namespace 创建，bwrap 在 ubuntu-24.04 runner 上是否开箱可用需要
  在 CI 上跑一次探针工作流验证；不可用则评估
  `kernel.apparmor_restrict_unprivileged_userns=0`（runner 内可 sudo）或退回丁案-only。
- 网络维度维持另案（`--unshare-net` 会掐断 git/pytest 的正常需求，分类放行是
  独立设计，不塞进本轮）。

### 乙：nsjail（否决）

能力超集（cgroup 资源限制、namespace 全餐），但配置语言重、ubuntu 仓库版本旧、
源码编译引入构建链——对一个「写围栏对齐」需求是杀鸡用牛刀。若未来要做网络/资源
双维度再重审。

### 丙：接受现状（否决）

「runner 即沙箱」论见威胁模型 5。丁案落地后残余风险可接受，但作为最终答案不成立。

### 丁：子进程 env 净化（推荐，与甲并行、独立交付）

`run_command` spawn 子进程时剥掉匹配 `*KEY* / *TOKEN* / *SECRET*` 的环境变量
（保留 `PATH` 等运行必需）。一行级改动、三平台通吃、不依赖任何沙箱后端——
**它防的是威胁模型 3 这条最实的路，且在 macOS 上同样成立**（seatbelt 围的是文件，
不围 env；`env`/`printenv` 不在白名单但 `/proc/self/environ` 是 macOS 没有的路径——
macOS 上等价通道是 `ps eww` 或 launchctl，均不在白名单，故 macOS 残余面更小，
但 Linux 上 /proc 是全开的）。

注意一个次序：bot 进程本体（worker.py 的 LLM 调用）需要 key，**净化只作用于
`run_command` 的子进程 env**，主进程 env 不动。

### 戊：GitHub 侧缓解（已部分落地，记录）

`FACTA_BOT_TOKEN` 已是细粒度单仓库 PAT（写权限仅本仓库）；#16 后评论触发需
OWNER/MEMBER/COLLABORATOR。残余：PAT 无法按「不许读 actions secrets」收窄
（PAT 本来就碰不到 secrets，但 bot 进程的 env 里有它们——这正是丁案要堵的）。

## 建议裁定（待拍板）

**丁案先行 + 甲案随后**：

1. 丁案：本轮就做（小、独立、三平台受益），配测试钉住「子进程 env 不含 *KEY*」。
2. 甲案：先跑 CI 探针验证 bwrap 可用性，可用则按 048 同构落地 Linux 后端，
   profile 语义逐条对齐 seatbelt（写白名单四处 / .env 读写双遮 / .git hooks+config
   / 目录黑名单 + MEMORY_WRITE_FENCE）；不可用则在 ADR 补记实测结论后重审。
3. 乙、丙否决（理由如上）；网络维度维持「另案」不变。

## 实测记录（2026-10-02，探针 workflow run 36952782730）

1. **丁案已落地**（#22）：`terminal.py` 子进程 env 剥 `*_KEY/_TOKEN/_SECRET` 形态变量，
   测试钉住 `sk-` 形态秘密不进子进程。
2. **探针①（裸 bwrap）= FAIL**：ubuntu-24.04 runner 报
   `bwrap: setting up uid map: Permission denied`，实测
   `kernel.apparmor_restrict_unprivileged_userns = 1`——Ubuntu 23.10+ 的 AppArmor
   userns 限制实锤，bwrap 开箱不可用。
3. **探针①b（`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` 后）= OK**：
   runner 一次性 VM 内 sudo 改内核参数可行，bwrap 正常起。
4. **探针②（写围栏语义）= 对齐 seatbelt**：workspace 内可写（`WRITE_WORKSPACE: OK`），
   `/etc` 与 `$HOME` 只读（`Read-only file system`）——甲案的 profile 语义成立。
5. **探针③（/proc/self/environ）= 可见**：`SECRET_PROBE_TOKEN` 经 `grep -c` 命中 1 次——
   威胁模型 3 实锤，bwrap 不围 env，丁案净化是必要补层（已落地）。

~~**当前待拍板项**~~（已拍板，见下节实测记录续）

## 实测记录续（2026-10-02，矩阵探针 run 36954014738 → 甲案落地）

6. **ubuntu-22.04 裸 bwrap = OK**（矩阵探针新增 leg）：AppArmor userns 限制是
   23.10 引入的，22.04 LTS 上 `bwrap --ro-bind / / --unshare-pid` 开箱即通，
   探针②③语义同验成立。**裁定：走 22.04 路线，不动内核参数**——agent.yml 与
   ci.yml 钉 `ubuntu-22.04`（22.04 支持到 2027 年中，到期前重审或届时
   ubuntu-latest 策略有变再说），零 sudo sysctl。
7. **甲案落地形态**：`sandbox.py` 加 `_bwrap_available()`（which + 空跑探测制，
   检出也不假装支持——048 同款）与 `build_bwrap_argv()`，`wrap_command` 派发
   第二支。语义逐条对齐 seatbelt：ro-bind / 打底、写白名单 root+/tmp、
   黑名单 ro-bind 盖回、.git hooks/config 围死、.env 读写双遮、网络放行（另案不变）。
8. **与 seatbelt 的三个已知语义差**（bwrap 无 regex，接受并记录）：
   - 遮蔽清单是 wrap 时枚举——wrap 之后新建的 .env 不在围栏内（seatbelt 是模式匹配）；
   - .env 目录走 `--tmpfs` 遮蔽（读空、写「成功」但随进程消失），文件走
     `--ro-bind /dev/null`（读空写 EROFS，与 seatbelt 同语义）；
   - 报错文案不是 seatbelt 的 Operation not permitted（EPERM）：ro-bind 目录写 =
     Read-only file system（EROFS），/dev/null 遮蔽文件写 = Permission denied
     （EACCES）——run_command 工具描述已对齐三种文案，bot 自我纠正材料不缺。
9. **CI 视野**：ci.yml 同钉 22.04 + 装 bubblewrap——test_sandbox 的 bwrap 实跑类
   （写围栏正反对称/记忆围栏/git 闭环/子进程继承）在 CI 恒跑，bot 的日常运行态
   不再是「本地 skip 的盲区」。

## 触发信号

- ~~bwrap 探针实测结果~~（已完成：24.04 FAIL / sudo OK / 22.04 裸 OK，甲案走 22.04 落地）
- bot run 审计里 `sandbox=off` 的出现率（现状：CI 上恒 100%）→ 甲案落地后应归零（出现即查 bwrap 安装/探测）
- 出现「测试文件被注入改写后跑 pytest」的实机样本 → 威胁模型 2 的残余面升级为独立案
