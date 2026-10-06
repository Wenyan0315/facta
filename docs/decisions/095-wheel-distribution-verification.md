# ADR 095：wheel 分发验证——干净环境安装、静态资源、数据目录与 mock 启动

- 状态：已裁定 + 已验证（2026-10-06 实验通过，见「验证结果」）
- 日期：2026-10-06
- 上游：[087](087-project-review-and-next-priorities.md) R09（验证分发与外部接入）
- 推翻关系：无新增

## 背景

071 留过承诺：「数据目录与代码目录分离」的真消费者是
`pip install facta`（非 `-e`）的用户——那时 `paths.py` 的
`__file__` 锚定跳到 site-packages，`data/` 写不进散落四处，
首次启动也找不到 notes 语料，`FACTA_DATA_DIR` 覆盖就是为此开的
口子。但这条链路从未真走过一遍：仓库里无 MANIFEST.in、
pyproject 无 package-data 配置，`server/static/`（含 fw 构建产物）
大概率根本没进 wheel——「支持 wheel 安装」目前只是一句没验过的话。

R09 的 087 原文边界：验证 wheel 安装、静态资源、数据目录与
mock 启动；VS Code 壳、独立记忆产品等外部接入等真实需求，
不把验证扩成新产品开发。

## 裁定

1. **验证方式 = 一次性实验，结果记进本 ADR，不造常驻测试**。
   干净环境 = 新 venv + 装 `pip wheel` 打出的 wheel（`[web]`
   extras）+ 从中立 cwd（非仓库目录）启动。CI 化留触发信号：
   第一次有真实外部用户装 wheel 时再议。
2. **验证清单**（逐条过，记通过/失败与证据）：
   ① wheel 内容审计：`unzip -l` 查 `facta/server/static/` 与
   `static/fw/` 是否入包；
   ② 干净 venv 安装 wheel[web] 成功（离线 mock 路径不需要
   rag/dev extras）；
   ③ `FACTA_PROVIDER=mock` + `FACTA_DATA_DIR=<tmp>` +
   `FACTA_USER_MEMORY=<tmp>/user.md` 从中立 cwd 起
   `python -m facta.server`：进程就绪、不崩；
   ④ HTTP 断言：`/` 200（含 `#env-badge` 挂载点）、`/app.js`
   200、`/memory` 200（fw 产物链路）、`/api/status` 200 JSON、
   `POST /api/sessions` 201 且 `GET /api/sessions` 见得到；
   ⑤ 数据目录纪律：运行后 `<tmp>` 下有数据落盘，仓库目录
   `git status` 零变化（090 的「env 覆盖=全新数据根」兜底）。
3. **失败处置**：验证发现的打包缺陷（如静态文件缺打包配置）
   属 R09 份内，直接修（加 MANIFEST.in / package-data 之类最小
   改动）并重跑清单；超出打包范畴的缺陷（如某端点 wheel 下行为
   不同）不就地扩 scope，登记 architecture.md 已知问题 + 触发
   信号，另立 ADR。
4. **README 暂不加「pip install facta」安装段**——本 ADR 验的是
   「wheel 能装能跑」，不是「已发布 PyPI」。公开发布动作（版本
    tagging、twine 上传）等用户显式要求。

### 为什么不顺手发 PyPI

发布是不可逆外动作（包名占用、版本不可撤），且带来真实的维护
承诺（issue 响应、兼容窗口）。R09 只要「能装」的证据，不要
「已发布」的事实。

## 边界（显式记录）

- 只验 mock 档离线路径；真 provider（API key、embedding、
  chromadb）的分发验证留到 R06 同款「真烧预算」场景一并过。
- 不验 sdist 安装（wheel 是 pip 首选路径；sdist 兜底价值低）。
- macOS 单平台验证；Linux/Windows 的 wheel 行为差异留触发信号
  （第一个非 macOS 用户反馈时）。

## 验收（087 的 R09 完成标准，逐条）

- [x] 干净环境验证 wheel 安装：见「验证结果」节。
- [x] 静态资源：①④条。
- [x] 数据目录：③⑤条。
- [x] mock 启动：③④条。
- [x] 验证结果记录进本 ADR；若修复打包缺陷，三门全绿。

## 验证结果

实验现场 `/tmp/facta_r09/`（wheel + 干净 venv + 中立 cwd `run/` +
tmp 数据根），2026-10-06 跑完，**清单五条全过**。实验先后坐实并
修掉 5 个分发阻断缺陷：

| # | 缺陷 | 修复 |
|---|------|------|
| 1 | wheel 0 个静态文件（vanilla 页 + fw 产物全丢，网页全 404） | pyproject 加 `[tool.setuptools.package-data]` 逐层枚举 static 四层 |
| 2 | `mcp_config.py` 顶层 import httpx（rag extras，mock 档起步即崩） | `TYPE_CHECKING` 块 + url 分支内延迟导入 |
| 3 | `web.py` 顶层 import httpx（同上） | `_import_httpx()` helper，三处用点延迟导入 |
| 4 | `FACTA_DATA_DIR` 全新根 notes 目录缺失，炸 loader 第一道防线 | assemble env 门控 `mkdir(parents=True, exist_ok=True)` |
| 5 | mkdir 后空目录炸第二道防线，且**二次启动必崩**（首启建的空目录下轮即成「已存在的空目录」） | 语义定稿：env 覆盖下 notes 无 `.md` =「还没写过笔记」的正常状态，知识库/图谱同步跳过；仓库布局两道防线一字不动 |

逐条结果：

1. **wheel 审计**：修复后 `facta/server/static/` 13 个文件全入包
   （app.js / style.css / index.html / fw 4 页 + assets 5 js /
   vendor/marked.min.js）。✅
2. **干净 venv 安装**：新 venv 装 wheel + `[web]` extras 成功。✅
3. **mock 启动**（中立 cwd，`FACTA_PROVIDER=mock` +
   `FACTA_DATA_DIR`/`FACTA_USER_MEMORY` 指向 tmp）：三种场景全过——
   首启（全新根，同步跳过日志按预期打出）、二启（空 notes 不崩，
   缺陷 5 的回归场景）、三启（放入一篇 .md 后同步恢复执行：
   「知识库同步：新增 1」，图谱 mock 档按设计跳过抽取）。✅
4. **HTTP 断言 9 条全 PASS**：`/`（含 `#env-badge`）、`/app.js`、
   `/memory` + fw 资产本体、`/tasks`、`/graph`、`/notes`、
   `/api/status`（093 三字段齐，seatbelt 检出）、
   POST+GET `/api/sessions`。✅
5. **数据目录纪律**：tmp 根落盘 `memory/sessions/*.json` 与
   `notes/hello.md`；仓库 `git status` 只有本次源码改动，零运行时
   污染。✅

遗留口径（记录在案，不扩 scope）：env 覆盖数据根下若用户删光全部
`.md`，同步保持跳过、Chroma/图谱旧条目不主动清——内存 KB 下次
启动自然归零，Chroma 路径属 rag 档边缘场景，触发信号=第一个真实
rag 档 wheel 用户反馈。
