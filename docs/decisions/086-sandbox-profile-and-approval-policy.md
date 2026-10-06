# ADR 086：Sandbox profile 与确认缝正交（执行隔离档 × 确认策略）

- 状态：**已批准**
- 日期：2026-10-06
- 立项出处：[competitive-roadmap P1-6](../internal/competitive-roadmap.md)（v0.5 编排补遗）
- 关联：048（run_command 沙箱）、050（确认归因 guard）、052（记忆写围栏）、072（Linux bwrap 后端）

## 背景

roadmap P1-6 原文：

> 两个独立旋钮——执行隔离档（none / workspace-write / container）× 确认策略
> （untrusted / on-request / never）；container 档放 P1 末期，先落地前两档 +
> 策略矩阵文档化。

病灶：当前「隔离」与「确认」是两根耦合在单个 `FACTA_SANDBOX=off` 开关上的线——
沙箱只有「有无」二元（048），确认缝只有「按工具 needs_confirmation」单档（S4b）。
把 agent 交给非完全信任的任务（批量处理陌生仓库）时，只能靠确认缝硬扛，没有
「整批降信任」的档位可切。

## 拍板

### ① 两个独立旋钮，各自点用点读环境变量

不新建中心 config 文件、不做 `ToolRegistry.__init__` 构造参数——遵循项目既有
「配置 = 环境变量，消费点 `os.environ.get` 现读」模式（`FACTA_SANDBOX` /
`FACTA_DATA_DIR` / `MCP_SERVERS` 同款）。构造参数需要四处透传
（`assemble.py` 母/session、`spawn.py` 子 registry、`cli.py` bare），零传播的
env 点用点读是单一真值源、最短 diff。

### ② 执行隔离档（`sandbox.py`）

新增 `sandbox_level()`，把 `FACTA_SANDBOX` 归一化为三档：

| 档 | 语义 | 后端 |
|---|---|---|
| `none` | 无围栏，原样跑 + 审计打标 off | — |
| `workspace-write` | **默认档**。写限项目根 + TMPDIR，读全放（除 .env 一族） | seatbelt / bwrap / 无后端降级 off |
| `container` | 容器隔离 | **后置**（P1 末期） |

归一规则：`off`/`none` → `none`（兼容 048 遗留开关）；`container` → `container`；
其余（`auto`/`on`/缺省/未知值）→ `workspace-write`（默认档，语义与旧 `auto` 一致）。

`detect_backend()` 改为 `sandbox_level()` 驱动：`none` → `None`；`container`
本版诚实降级走 `workspace-write` 探测（**不假装有容器围栏**，048「无后端诚实
降级」同款裁定）；`workspace-write` → 现有平台探测。

### ③ 确认策略（`registry.py`）

新增 `approval_policy()`，把 `FACTA_APPROVAL_POLICY` 归一化为三档：

| 档 | 语义 | guard |
|---|---|---|
| `untrusted` | 全确认——即便工具免确认也弹 | `untrusted-policy` |
| `on-request` | **默认档**。按工具 `needs_confirmation` 现状 | 规则名或 None |
| `never` | 跳过确认（needs 恒 False） | None |

归一规则：`untrusted` → `untrusted`；`never` → `never`；其余（缺省/未知）→
`on-request`。

确认判定收口抽成 `_needs_confirmation(tool, args) -> (needs, guard)`（同
`_scope_denial` / `_approval_trace` 的抽出理由：`execute` 分支数已撞 ruff
PLR0912，不提高阈值）。`untrusted` 档 `guard` 统一记 `"untrusted-policy"`（即便
工具自带规则名也覆盖——该档弹窗的归因是「策略强制」，不是「撞了工具哪道围栏」）。

### ④ 策略矩阵（正交，独立开关）

| | `FACTA_SANDBOX=none` | `workspace-write` | `container` |
|---|---|---|---|
| `FACTA_APPROVAL_POLICY=untrusted` | 全确认，无围栏 | 全确认 + 写围栏 | 全确认 + 容器 |
| `on-request` | 按工具，无围栏 | 按工具 + 写围栏 | 按工具 + 容器 |
| `never` | 零确认，无围栏 | 零确认 + 写围栏 | 零确认 + 容器 |

两旋钮互不依赖：确认策略只管「要不要人裁决」，隔离档只管「裁决通过后进程被
围在哪」。`container` 列后置，本版矩阵前三列 `workspace-write` 落地、`container`
留占位。

**负决策**：

- 不做 `ToolRegistry` 构造参数透传——四处透传换不来比 env 现读更强的能力，反
  而引入「构造时快照 vs 运行时现读」的漂移风险（与 048「不缓存 which」同哲学）。
- 不在 `container` 档假装有容器——后置就是要后置，诚实降级比假围栏安全（048
  核心裁定：不假装有围栏）。
- 不把 `sandbox_level` 档位写进 `Tool.sandboxed` 字段或 audit extra——audit 记的
  `sandbox` 是「实际后端」（seatbelt/bwrap/off），档位是配置、后端是事实，两者
  不混（`sandbox=off` 已经诚实表达「无围栏」）。

## 正面（本项落地的位置）

- **`src/facta/tools/sandbox.py`**：新增 `sandbox_level()`；`detect_backend()` 改用
  它（`none` 短路返回 `None`，`container` 与 `workspace-write` 同走探测）。
- **`src/facta/tools/registry.py`**：新增 `approval_policy()` + `_needs_confirmation()`；
  `execute()` 确认段改为 `needs, guard = _needs_confirmation(tool, args)`。
- **`.env.example`**：`FACTA_SANDBOX` 注释改为三档说明；新增 `FACTA_APPROVAL_POLICY`。

## 边界（诚实登记）

- **`container` 是占位不是实现**：本版不引入任何容器后端，`detect_backend` 对
  `container` 档返回的是 workspace-write 探测结果。触发信号＝用户要求更强的隔离
  边界（如陌生仓库批量任务连 workspace-write 都嫌宽）时，再选 Docker/podman 后端。
- **`untrusted` 全确认的摩擦未度量**：读类工具（`read_file`/`search_code`）也弹窗，
  可能触发确认疲劳（S4b 白名单要防的正是这个）。这是该档的**语义本意**（整批降
  信任），不是缺陷；触发信号＝实机发现 untrusted 档下模型被弹窗淹到无法推进时，
  再议「只写类工具全确认、读类白名单」的折中档。
- **`never` 是无头档**：跳过确认意味着无人工裁决通道（bot/CI）。与 050「无 confirm
  通道按拒绝处理」不同——`never` 是显式跳过、`confirm is None` 是保守拒绝；两者
  正交，`never` 优先（策略显式 > 缺省保守）。

## 验收

- `sandbox_level()`：`off`/`none` → `none`；`container` → `container`；
  缺省/`auto`/未知 → `workspace-write`。
- `detect_backend()`：`FACTA_SANDBOX=off` 与 `=none` 均返回 `None`；
  `=container` 与缺省返回的平台探测结果一致（不假装有容器）。
- `approval_policy()`：`untrusted` / `never` / 缺省与未知 → 三档归一。
- `_needs_confirmation()`：`untrusted` 下免确认工具也返回 `(True, "untrusted-policy")`；
  `never` 下高危工具返回 `(False, None)`；`on-request` 下规则名归因不变。
- `execute()` 端到端：`untrusted` 下只读工具也走确认（拒绝/批准路径正确、审计
  `guard="untrusted-policy"`）；`never` 下高危工具免确认直接执行、审计无 guard。
- 既有确认/沙箱测试全绿（`test_sandbox.py` / `test_terminal.py` / `test_security.py`）。
- 三门通过（ruff / mypy / pytest）。
