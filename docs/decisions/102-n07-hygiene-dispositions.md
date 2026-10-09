# ADR 102：N07 活清单卫生——三件处置（TODOS 结案 / 续跑口径修正 / MCP 面板排期）

- 状态：已裁定（2026-10-09，③ MCP 面板排期经用户拍板：下一里程碑）
- 日期：2026-10-09
- 上游：[097](097-next-milestone-mechanism-closeout-and-product.md) N07、
  [090](090-data-root-unification.md)（①的实际修复方）、[040](040-run-checkpoint-impl.md)
  （②的口径出处）
- 推翻关系：②修正 040 与 architecture.md 的一处文档口径错误（功能无缺陷，
  历史判断不动——续跑机制本身工作，错的是那句 HTTP 边界描述）

## 三件处置

1. **325 条 TODOS_PATH 标结案（①）**：090 已实际修掉——验证于
   `assemble.py:76`：`TODOS_PATH = DATA_ROOT / "todos.json"`，与
   `paths.py` 全族同列，`FACTA_DATA_DIR` 覆盖圈已覆盖待办。活清单
   条目标注结案（登记时未标是文档滞后不是代码缺口）。
2. **324 条续跑文档口径修正（②）**：原句「`POST /api/runs` 传空 text
   即续跑」在 HTTP 边界不成立（`CreateRunRequest.text` 必填纯字符串，
   空串会追加一条空用户消息）。修正动作：architecture.md server 行
   改为真实口径（续跑 UX＝用户再发一条消息，heal 在 worker 进场时
   补齐悬挂轮次）；[040](040-run-checkpoint-impl.md) 作为历史记录按惯例
   **不改写原文**，加注记指向本 ADR。功能零改动。
3. **MCP 可视化配置面板排期（③，用户拍板）**：**排下一里程碑**。
   本里程碑产品线已有 N04（VS Code 壳）+ 098/D01（方案评审）双线，
   面板不阻塞任何一项；前置（A 径 044 落地 + memory 服务器入清单）
   早已清，排期纯粹是产能分配。触发信号沿用 334 条原文：手动编辑
   `mcp_servers.json` 出现真实摩擦、或产品线出现空位时优先。

## 边界

- ①的结案不改变 325 条登记时的历史判断（当时确实未修）；修掉它的
  是 090，本 ADR 只补文档滞后。
- ②不改 `CreateRunRequest` 类型（空 text 消费者仍不存在，归一化
  留给真消费者出现时——324 条触发信号①原文保留）。
- ③不是「不做」——用户点名的「明确要做」身份不变，只定档期。

## 验证结果

- 纯文档与登记件，无代码改动；三门读数维持基线（879 passed，
  7 skipped）。
