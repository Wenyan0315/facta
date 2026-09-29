# 069 开源前评审修复轮：6 P1 + 3 P2 全处置

> 状态：**已批准（2026-09-29，用户拍板「尽量修复掉明显的问题」）**，同日落地｜日期：2026-09-29
> 立项出处：开源前评审（内部诊断文档，含复现证据；不下沉时不在公开范围）。评审结论「先修 P1 再邀请朋友安装使用」——本轮把 6 条 P1、3 条 P2 全部处置，产品边界建议按「修实锤、挂信号」分流。
> 一句话结论：**测试全绿掩盖的是边界缺陷：启动降级、崩溃保存、并发建会话、过期记忆快照、白名单三洞、配置加载顺序——全部按「保对话本体、可失败步骤容错、保守方向确认」三条原则修复，每条配回归测试钉住。**

## 修复清单（评审编号 → 处置）

| # | 缺陷 | 修法 | 测试钉 |
|---|---|---|---|
| P1-1 | 只配 DEEPSEEK key 时 `assemble` 崩在 embedder 构造 | `_rag_missing_reason()`：缺 SILICONFLOW key 或缺 chromadb → 词袋 + 内存库（教学组合同款），语义 RAG 是检索增益不挡主聊天；降级不打开磁盘 Chroma（词袋维度与既有 BGE 集合不兼容），补齐 key 自动回增量路径 | 3 条（缺 key／缺 chromadb 用 `sys.modules[None]` 技巧／教学组合不算降级） |
| P1-2 | `.env.example` 被 `.env.*` 吞掉，clone 后无模板 | `.gitignore` 加 `!.env.example`（注意：gitignore **不支持行内注释**，尾注释会变成模式的一部分——第一版因此失效，改为独立注释行） | `git add` 成功即验收 |
| P1-3 | 白名单三洞：`sort -o/tmp/x` 粘连、`git diff --output=`、`./tools/echo` 假冒 | ① 程序带路径一律确认（白名单语义=「PATH 里公认只读的程序」，仓库内同名程序证明不了自己是它）② `_DANGEROUS_PREFIXES` 按前缀抓短选项粘连 ③ git 补 `--output`、find 补 `-fprint` 家族 | 3+4 条新参数化用例 |
| P1-4 | 记忆改/删后旧会话仍把旧 system 快照发给模型 | `ensure_persona` 第三分支：快照过期就地刷新（`messages[0]` 是「人设+记忆快照」的冻结副本，不是历史存档真值）；只动 content 不动位置，游标无感 | three_branches 扩 stale 分支 + 合并去重后同样刷新 |
| P1-5 | 固化/标题抛异常把 `store.save` 一起拖死，已回答文本消失 | `settle_session` 重排：**保底 save → 标题（失败退首句）→ 固化（失败游标不动）→ 终态 save**；固化游标语义不变（推进后必有 save） | 4 条（固化崩对话仍存／报告失败游标停／成功推进／标题炸退首句） |
| P1-6 | 同秒并发 `create` 拿到同一个 id + 互踩 tmp | `create` 全程持 `threading.Lock`（锁域毫秒级）；`save_session` tmp 名带 uuid（同 sid 并发写不再互踩同一 `.tmp`） | 4 线程 Barrier 对齐，id 全唯一、文件全落 |
| P2-7 | 坏 JSON 报「未写入」但游标照样推进，记忆静默丢失 | `consolidate` 返回 `(report, ok)`：ok=False（坏 JSON）不推进游标下轮重烧；「无条目／全驳回」是 ok=True 的正常零写入 | 2 处既有失败测试补 `ok is False` 断言 |
| P2-8 | `.env` 里的 provider 配置来得太晚（assemble 才 load_dotenv） | `server/__main__` 先 `load_dotenv()` 再读；改名 `FACTA_PROVIDER`（旧名 `AGENT_PROVIDER` 兼容，新名优先） | —（入口脚本，实机即验） |
| P2-9 | mock「不联网」承诺与 Context7 默认 enabled 矛盾 | `mcp_servers.json` 里 ctx7 改 `enabled: false`（与 `mcp_config.py` 内置默认对齐）；用户显式开启才连 | —（配置即行为） |

边界建议（评审「产品与发布边界」节）处置：**修实锤**＝`data/learned/constraints.md` 过时行（「没有通用文件读写工具」与 S4 后工具清单矛盾，clone 用户会继承错事实）改为以实际注册清单为准；README「副作用不重复」改为「已记录结果原样恢复，结果未知的副作用提示核验」（恢复不是 exactly-once，文案与 040 实现对齐）。**挂信号**＝工作目录固定仓库根（wheel 分发语义待专项验收）／记忆行号身份（两标签页并发删改）／作者 learned 记忆随仓库分发（是否给新用户空起步是产品决策）／跨进程 create 窗口（CLI+Web 同跑）／沙箱仅 macOS（terminal.py ⑤ 既有注记）。

## 验收

- 三门：**735 passed, 2 skipped**（基线 721，净 +14：白名单 7 参数化 + store 并发 1 + settle 容错 4 + RAG 降级 3，另 2 条既有失败测试补 ok 断言）；ruff（assemble 曾撞 PLR0912 13>12，embedder 三元化压回，054/056「不提阈值」同款）；mypy 59 文件干净。
- 评审复现口径对照：P1-1/P1-5/P1-6/P2-7 均按评审的故障注入/Barrier 手法写成测试；P1-3 三个实测用例全部进参数化清单。

## 未做与理由（评审建议中刻意不采纳项）

- **`shlex.split()` 全量解析**：评审自己注明「不能解决连写选项和程序来源问题」——半吊子解析比不解析更危险（制造已解析假象），本案用前缀刀+路径拒绝定向堵洞。
- **shutil.which 校验程序真实来源**：PATH 篡改场景（评审提及「不能证明是系统 echo」的深水区）超出本轮，带路径程序已全拒绝，裸命令名的 PATH 劫持挂信号（触发＝出现实际绕过案例）。
- **wheel 打包验收**：评审环境缺 setuptools 未完成，非仓库缺陷；开源发布前用 `python -m build` 补一次正式验收即可。

## 触发信号

- 降级路径被误报（有 key 且装了 rag 仍走词袋）→ 查 `_rag_missing_reason` 的 import 探测是否被环境干扰。
- `create` 再现同 id / FileNotFoundError → 跨进程窗口坐实，升级文件锁（`O_EXCL` 占位）。
- 过期 system 再现（payload 带已删记忆）→ 查是否出现绕过 `build_agent` 直改 messages 的路径。
- ok=False 的固化连续失败 ≥3 轮 → 不是重试能救的（模型持续坏输出），升级为面板可见的健康信号。
