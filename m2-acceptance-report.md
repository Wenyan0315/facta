# M2 界定与技能——真机验收报告

- 日期：2026-11-15
- 依据：ADR [116](docs/decisions/116-m2-bounding-and-skills.md)（M2 期四件事）+ ADR [114](docs/decisions/114-facta-2-roadmap.md)（钉子①②）+ ADR [111](docs/decisions/111-v01-review-prototype.md)（review 等价点）
- 实施：squash commit 97689fd 合并 main（PR #63），三门 CI 绿
- 验收环境：真机服务 `FACTA_PROVIDER=deepseek`、`FACTA_SERVER_PORT=8100` 后台运行；全部通过 `curl` 走真实 API 面，不看代码实现

## 1. 验收钉子对照总表

| 钉子 | 状态 | 凭据 |
|---|---|---|
| ① 零代码加技能（新增 data/skills/\*.md 即用，不碰 py） | **通过** | §2（含 422 对照） |
| ② 第二项目记忆归属隔离 | **通过** | §3（四小件全过） |
| review 迁移等价（111 等价点重锚） | **通过** | §4 |
| brief 六节抽查 | **通过** | §5 |

## 2. 钉子①：零代码加技能

**操作**：服务启动前在 `data/skills/` 落 `probe.md`（临时探针，内容=回复须以 `PROBE_OK` 开头 + 复述工具菜单；`budget=3`、`settle=skip`、无 `tools` 节=缺省全量）。未改任何 py 文件，未重启服务之外的任何代码动作。

**主验**（`POST /api/runs/sync`，`mode:"probe"`）：

- HTTP 200，`status:"completed"`（settle=skip 技能 sync 照常收尾），run_id `eb7b9cf5…`，session_id `20261115-222725`。
- reply 开头 `PROBE_OK`；随后复述工具菜单**恰 8 件**：get_current_time / list_notes / read_notes / search_notes / search_and_summarize / read_file / search_code / list_dir，并声明「未调用任何工具、未执行其他操作」。

**对照钉**（`mode:"probe_nonexistent"`）：

- HTTP 422：`{"detail":"未知 mode：'probe_nonexistent'（可用技能：['brief', 'probe', 'review']）"}`。
- 错误信息里列出 `probe` —— 证明服务运行时把未进过代码的技能目录装配进了技能表（零代码钉子的直接证据）。

**清理**：验完已删 `data/skills/probe.md`，`git status` 确认 `data/skills/` 无未跟踪文件。

## 3. 钉子②：第二项目记忆归属隔离

第二项目仓建在 /tmp 探针区：`/tmp/facta-accept-20261115/proj2/`，含 `PLAN.md`（内容含独特串 `PROJ2_MARKER_7f3a9c2e`）与 `decisions.md`（ADR P2-01：存储选 sqlite）。

### 3.1 登记（就地式）

`POST /api/projects` → 201：

```json
{"name":"proj2","repo_root":"/private/tmp/facta-accept-20261115/proj2",
 "memory_mode":"in-repo","learned_path":"/private/tmp/facta-accept-20261115/proj2/.facta/learned"}
```

（`/tmp` → `/private/tmp` 是 macOS 符号链接 resolve，登记即落盘）

### 3.2 四小件

| # | 验项 | 结果 |
|---|---|---|
| a | `GET /api/projects` 列表含 proj2 | 通过 |
| b | 就地式 learned 目录已建（登记即建目录） | 通过：`<repo>/.facta/learned/` 存在 |
| c | 技能锚点/记忆指向第二项目 | 通过：§3.3 |
| d | 本仓登记被拒 | 通过：400 `本仓（WORKSPACE_ROOT）禁止登记为项目：恒走全局记忆，双读入口会漂移` |

### 3.3 越界与隔离实证（brief + repo_root=proj2）

`POST /api/runs/sync`（`mode:"brief"`，`repo_root:"/tmp/facta-accept-20261115/proj2"`）→ 200 completed。reply 关键节选：

- 文件锚点指向第二项目：`PLAN.md`（36 B）原文被逐字引用，含 `PROJ2_MARKER_7f3a9c2e`；`decisions.md` 的 ADR P2-01（2026-11-01）被引用。
- **本仓记忆零注入**（隔离成立）：reply 明写「`.facta/learned/` 为空目录；知识库检索…无相关内容」——本仓全局 learned（含大量本仓 ADR/约束）一条都没有出现。对照 §5 本仓 brief 中「ADR 105/B-lite 红线（记忆，非本仓读得）」「项目级约束（记忆）」均正常注入，对照组清晰。
- 结构上无写：「我无法写文件或改代码，以上均未落盘」——工具菜单只读 8 件在第二项目同样成立。

### 3.4 顺手钉：重复登记

- 名字重复（proj2 + 新路径）→ **409**「项目已登记：proj2」。
- 仓重复（新名字 + proj2 路径）→ **400**「该仓库已登记为其他项目」。码值与名字重复不一致但语义清晰可辨，记入 §6 备注，不改码。

## 4. review 迁移等价（111 等价点重锚）

`POST /api/runs/sync`（`mode:"review"`，`repo_root:本仓`，对象 `src/facta/skills.py`）→ 200 completed。

- 等价点①（只读不改）：reply 开头「我没有修改任何文件，也没有运行测试」，全文为静态评审意见。
- 等价点②（无写工具调用声称）：释放检查——`write_file/write_note` 各 1 次出现，上下文均为描述 registry「摘除 write_note、write_file」，非声称调用；`已修改/已写入/提交/落盘` 零命中。
- 等价点③（迁移后仍可达）：reply 提及「尝试读取 src/facta/review.py 报文件不存在」——review.py 已删、人设迁入 `data/skills/review.md`，而 `mode:"review"` 通过技能表正常出活，迁移等价成立；且「router=None 显式钉死」「十类输入都抛 ValueError」等评审意见与 ADR 116 ✓ 对得上（模型自己核出）。
- 纪律面：预算用尽处诚实披露（「工具预算在读到 history.py 前用完」……「未做任何编造」风格一致）。

## 5. brief 六节抽查（本仓为对象）

`POST /api/runs/sync`（`mode:"brief"`，`repo_root:本仓`，需求=给 review 报告加评论标签面板）→ 200 completed，reply 5358 字符。

| 节 | 状态 | 抽查 |
|---|---|---|
| ①任务目标复述 | 通过 | 区分了「本地方案评审页 A」与「GitHub PR 审查 bot B」两个 review 表面并请用户定 |
| ②相关历史决定 | 通过 | ADR 079（2026-10-05，已读原文）、ADR 111/093（据源码注释引用，诚实标注原文未读到） |
| ③仓库现状 | 通过 | 前端逐页/`App.jsx` 状态与无反馈 UI/`api.js` 接线、后端、bot 三块，标注读到的原文 |
| ④约束与纪律 | 通过 | Preact/二次出现不抽共享模块/评审只读边界/数据目录唯一真值源/架构决定须登记 |
| ⑤验收建议 | 通过 | A/B 两路线分列 + 通用三门复验，明确「建议，非既有口径」 |
| ⑥待用户补充 | 通过 | 8 条未核实项全部列明，未编造 |

补两点：本仓记忆正常注入（见 §3.3 对照）；1 处诚实披露——`docs/decisions/` 列表在 100 项处被工具输出截断，模型标注「101–117 的文件名与内容未读」并列入 §⑥，未硬编。

## 6. 发现的异常与处置

| # | 发现 | 定性 | 处置 |
|---|---|---|---|
| 1 | 仓重复登记返回 400（名字重复返回 409） | 一致性瑕疵，语义清晰、无功能影响 | **不改码**，登记备注；若日后统一 4xx 语义再议 |
| 2 | `/tmp` 被 resolve 成 `/private/tmp` | macOS 符号链接正常行为 | 无处置 |
| 3 | brief 中 `docs/decisions/` 列表 100 项截断，模型未读 101–117 | 工具输出截断 + 预算耗尽，模型诚实披露未编造 | **不改码**，观察项 |

## 7. 结论

M2 四件事真机验收**全部通过**，无需要回改代码的异常。钉子①证明技能数据化成立（加技能=加 md 文件，零 py 改动）；钉子②证明第二项目记忆按 repo_root 隔离（锚点指向、learned 零注入、本仓登记拒绝、就地式目录落盘）；review 迁移等价与 brief 六节均为真机 API 行为确认。

遗留观察项两条（§6 #1/#3）已挂档，触发信号到再动。

---

附：验收期间服务仍在后台运行（端口 8100，可留可停）；探针文件 `data/skills/probe.md` 已删除；/tmp 探针区 `/tmp/facta-accept-20261115/` 保留供复查。