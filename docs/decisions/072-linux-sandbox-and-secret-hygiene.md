# ADR 072：Linux 沙箱后端与 bot 场景的秘密卫生

- 状态：**草案（待拍板）**
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

**当前待拍板项**：甲案落地的唯一代价是 agent.yml 加一行
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`（在 bot job 内、
跑 agent 之前；runner 一次性，影响面限于当次 run）。拍板通过则按 048 同构实现
`detect_backend()` Linux 分支；否决则本 ADR 以丁案-only 结题，残余写围栏缺口挂触发信号。

## 触发信号

- bwrap 探针在 ubuntu-24.04 runner 的实测结果 → 决定甲案是否落地
- bot run 审计里 `sandbox=off` 的出现率（现状：CI 上恒 100%）→ 甲案落地后应归零
- 出现「测试文件被注入改写后跑 pytest」的实机样本 → 威胁模型 2 的残余面升级为独立案
