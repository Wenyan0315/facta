# 068 开源前品牌统一：cortex / personal-agent → Facta

> 状态：**已批准（2026-09-29，用户拍板定名 Facta，三项改名范围全采推荐项）**，同日落地｜日期：2026-09-29
> 立项出处：开源准备。定名前项目四处名字互相打架——GitHub 仓库 `cortex-from-scratch`、pyproject 发行名 `personal-agent`、Python 导入包顶层名 `agent`、用户可见名「Personal Agent」；且 cortex 已被他人项目占用，不适合作为开源品牌。
> 一句话结论：**Facta（拉丁语「已成之事」，slogan *Facta, non verba*——行胜于言，正对「执行主轴，不是陪聊」定位）；开源前零外部用户是改名零成本窗口，发行名/导入名/环境变量/用户目录一次性全统一，历史决策记录保持 append-only 不动。**

## 定名过程（留痕，避免以后再翻案）

- 实测候选约 40 个，逐个查 PyPI / npm / GitHub 注册状态与品牌撞车，淘汰原因各有实证：factotum/majordomo/steward 等 PyPI 被占；memor/memini/memoro 被 AI 记忆产品占；expedio/efficio/teneo/paratus/therapon/accuro/drasis 各撞公司；外部专家方案 **Noerun**（neuron 变位词，注册全空）进入终选但英文直读 "no run" 且与产品定位无语义连接，用户终选 Facta。
- 仓库名去后缀：`cortex-from-scratch` → `facta`（不带 `-from-scratch`）。「从零自研」是卖点但不是名字，放 README 首行 + GitHub topics。

## 统一范围（旧 → 新）

| 维度 | 旧 | 新 |
| --- | --- | --- |
| GitHub 仓库 | `Wenyan0315/cortex-from-scratch` | `Wenyan0315/facta`（GitHub rename，旧址自动跳转） |
| PyPI 发行名 | `personal-agent` | `facta`（`pip install facta`） |
| Python 导入包 | 顶层 `agent`（`src/agent/`） | 顶层 `facta`（`src/facta/`），`python -m facta[.server]` |
| 用户可见名 | Personal Agent | Facta（CLI banner / FastAPI title / 四个 HTML title / MCP clientInfo / 前端包名） |
| 环境变量 | `CORTEX_*`（7 个） | `FACTA_*`（USER_MEMORY / SANDBOX / CONSOLIDATE_THRESHOLD / STUCK_LIMIT / SEMANTIC_CACHE / MAX_CONCURRENT_RUNS；DATA_DIR 仍是未兑现触发信号，同步改名） |
| 用户级记忆目录 | `~/.personal-agent/` | `~/.facta/` |

导入包改名是本案最大 diff（100 个 py 文件、576 处引用），但全部机械替换（import 语句 + monkeypatch 字符串模块路径 + `src/agent` 路径 + `python -m agent`），721 测试网兜底；不修则 `pip install facta` 往 site-packages 装顶层 `agent` 通用名，是真实的命名空间隐患。

## 旧目录迁移语义（paths.py）

`user_memory_path()` 首次被调用且未显式设 `FACTA_USER_MEMORY` 时：**新目录不存在 + 旧目录存在 → 整目录 `shutil.move` 一次性搬移**；两边都在属异常状态（不猜不合并，记忆不可逆，045/064 同款保守，留给人处理）。不留双读逻辑——两个真值源就是本模块开篇点名的漂移温床。迁移行为已用隔离 HOME 临时目录实证两条分支（搬迁 / env 覆写时不动旧目录）；用户真实目录在首次运行时自动迁移。

## 刻意不动（append-only 边界）

- `docs/decisions/` 全部 ADR 原件：其中的 `CORTEX_*`、`~/.personal-agent/`、`src/agent/` 链接是写作时点的历史事实，不回改。考古映射＝本篇的旧→新对照表。
- `docs/reviewq/` 外部专家冻结评审（含其机器上的绝对路径证据链接）。
- `evals/scenarios/frozen_real.jsonl` 冻结题面（题面里出现旧变量名只是 prompt 文本，063 答案卷隔离后不影响 harness——harness 代码自身已用 `FACTA_USER_MEMORY` 构造子进程环境）。
- `data/notes/` 语料与 `data/memory/` 历史会话底片：不改写已发生的语料和对话。
- 活文档（architecture.md / roadmap / 手册类）按现行事实更新，架构图升 v0.95，png 待外部专家按新名重绘。

## 验收

- 三门：**721 passed, 2 skipped**；ruff 6 处仅为 import 字母序（agent→facta 换位），--fix 后全绿；mypy 59 文件无问题。
- editable 重装后从仓库外目录 `import facta` 指向 `src/facta/__init__.py`；`python -m facta echo` 全链跑通。
- 迁移两分支实证（见上）。

## 触发信号

- 发布后任何新的 `CORTEX_`/`~/.personal-agent`/`src/agent` 引用出现＝改名漏网（活文档/代码侧 grep 应归零，历史目录除外）。
- 用户首次运行后旧目录仍在＝迁移未触发（查启动路径是否绕过 `user_memory_path()`）。
- 两边目录并存的人工处置请求出现 → 先 diff user.md 再手工合并，不补自动合并逻辑（直到出现第二个真实案例）。
