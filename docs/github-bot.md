# facta-bot 接入指南

让 facta 自己当仓库的 AI contributor：issue 打 label 自动修，PR 开了自动审。
机制全部复用内核公开接口（`assemble` / `run_turn`），代码在 `src/facta/github_bot/`。

## 文件清单

| 文件 | 作用 |
|---|---|
| `.github/workflows/agent.yml` | 触发器：label / `@facta fix` 评论 / 同仓 PR 更新 |
| `src/facta/github_bot/worker.py` | 非交互入口：跑一轮内核 → 收 git 成果 → 开 PR / 发 review |
| `src/facta/github_bot/prompts.py` | 任务提示词模板（调教区，与机制分离） |

## 一次性配置（约 15 分钟）

### 1. 建 label
仓库 Settings → Labels → 新建 `agent`（颜色随意）。想接手的 issue 打上它即可。

### 2. 加 secrets
Settings → Secrets and variables → Actions：

- `FACTA_BOT_TOKEN`：**细粒度 PAT**（github.com/settings/personal-access-tokens），
  权限只勾 contents / pull-requests / issues 的读写，资源只选本仓库。
- LLM key：按 `.env.example` 里的实际变量名加（如 `DEEPSEEK_API_KEY`）。

### 3. 分支保护（守住「永不自动合并」）
Settings → Branches → 给 `main` 加保护：
- Require pull request before merging（至少 1 人 approve）
- Require status checks（CI 全绿）
- **不要**把 facta-bot 加进 bypass list。

### 4. 本地干跑（不花 token、不碰网络写操作）
```bash
BOT_DRY_RUN=1 BOT_TASK=fix-issue ISSUE_NUMBER=1   GITHUB_REPOSITORY=Wenyan0315/facta   python -m facta.github_bot.worker
```
dry-run 模式下评论只打印、git 提交/PR 全部跳过，用来调提示词。

## 为什么 push 要用 PAT 而不是 GITHUB_TOKEN

GitHub 防递归：用 `GITHUB_TOKEN` 发起的 push **不会触发**任何工作流，
bot 开的 PR 就没有 CI 跑。用 PAT（或 GitHub App token）则与真人推送等价，
`ci.yml` 会正常在 bot 的 PR 上运行。这是官方行为，不是配置错误。

## 安全边界

- **fork PR 不触发**：workflow 的 `if` 里写死了 `head.repo.full_name == github.repository`。
- **高危操作自动拒绝**：worker 的 `on_confirm` 恒返回 False，bot 没有批准权。
- **永不合并**：bot 只开 PR；合并必须过人 + CI。
- **diff 截尾 / 轮数上限 / 30 分钟超时**：prompt 预算、runaway loop、挂死三道闸。
- **权限最小化**：PAT 只读本仓库、只有三个 write scope。

## 调教提示词

改 `prompts.py` 即可，不用动 worker。常用调法：
- agent 改得太狠 → 收紧 `单 PR 净增删行 < 300 行` 或加「列改动清单等确认」
- 测试老红 → 在模板里强调「先跑 pytest 定位再改」的顺序
- 评论太多噪音 → 调 worker 里的 `PROGRESS_INTERVAL`

## 已知边界

- agent 需要具备**文件读写和命令执行**工具（ReAct 工具集里通常已有；
  如果当前 registry 没有 exec 类工具，先补一个再走这条链路）。
- `issue_comment` 触发点在 fork PR 上拿到的是只读 token：评论 `@facta fix`
  会在 push 时报错——这是故意的（不对不可信代码执行写操作）。
- 一个 issue 的并发由 workflow 的 `concurrency` 串行化；不同 issue 互不干扰。
