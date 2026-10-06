# ADR 089：记忆面板切换稳定 ID（by-id 定位 + 内容版本校验 + 懒迁移）

- 状态：**已批准**
- 日期：2026-10-06
- 立项出处：[087/R02](../decisions/087-project-review-and-next-priorities.md)
- 关联：025/041（记忆面板）、071（稳定 ID）、074（状态 tag 与 tombstone）、088（待办持久化，同轮先例）

## 背景

087/R02 落点 [server/app.py](../../src/facta/server/app.py)、
[memory/App.jsx](../../frontend/src/memory/App.jsx)、
[memory/learned.py](../../src/facta/memory/learned.py)。

底层早有 `update_line_by_id` / `delete_line_by_id`（071），但面板三端点与前端
仍按行号寻址：GET 返回 `line`，PUT/DELETE 路由与前端操作也传行号。手动整理、
坏行删除或墓碑物理回收（078）都会改变行号，让旧页面持有的位置失效，随后提交
可能改到错条目。这是「记忆行号身份」边界（learned.py 模块注释已登记）的产品侧
兑现——把面板从行号切到稳定 id，并给「旧页面在条目变化后提交」一个明确答复。

## 拍板

### ① 懒迁移 `ensure_ids`（写侧不动）

新增 `learned.ensure_ids(path) -> int`，在 GET 读路径先跑：复用
`read_learned(include_inactive=True)` 的解析，为「id=None 的行」补 `<!--id:xxx-->`
尾注释，已带 id 的行原样不动（幂等）。seq 取「全文件内同 (date, content) 的第
seq 次出现」——**计数含已带 id 的行**，保证 append 后补 id 不撞既有行、重复跑不漂。

选「GET 读路径懒迁移」而非「consolidate 写侧落 id」（Option A 裁定）：
- id 生成的唯一入口收敛到 `ensure_ids` 一处（单一真值源）。
- consolidate `_append` 热路径零改动（最小改动）。
- id 落盘后稳定：编辑时 `update_line_by_id` 保留原 id，不重算。

### ② API 切 by-id + 内容版本校验

- GET 返回加 `id` 字段、移除 `line` 字段（前端契约从行号改为 id）。
- PUT/DELETE 路由 `/api/learned/{category}/{line_id}`（`line_id: str`），调
  `update_line_by_id` / `delete_line_by_id`，id 不存在 → 404。
- `LearnedUpdateRequest` 加 `base_content: str`（必填）：PUT 时读当前在用条目的
  `visible_text` 与 `base_content` 比对，不一致 → 409（乐观锁，参照 042 notes 的
  `base_hash` 先例）。条目已被撤回/删除（tombstone）→ 在用列表查不到 → 404。

「已变化」的两种答复（409 冲突 / 404 不存在）由此各归各位：改到的是**同一 id**，
但内容已被别人改过 → 409；条目已不存在 → 404，不会改到错条目。

### ③ 写侧 id 取舍

consolidate `_append` 仍不写 id 注释（Option A）：新固化行无 id，下一次 GET 的
`ensure_ids` 补上。写侧零改动。

## 边界（诚实登记）

- **seq 撞车残差**：`make_id` 的 seq 方案在「编辑改了某同内容行的 content → 再
  append 一条同 (date, content) 行」的极端场景下可能撞 id（已编辑行 key 变了导致
  under-count seq）。这是 ADR 071 已记载的「极小概率」残差，本轮不做持久化计数器
  （成本/复杂度不对等）；触发信号＝真实撞车造成错误定位时，再评估计数器。
- **版本校验非事务**：PUT 的 `read_learned` 比对与 `update_line_by_id` 的写之间
  有一瞬窗口（`update_line_by_id` 内部才持 `LEARNED_LOCK`）。与 042 notes 的
  `base_hash` 同款「读→比对→写」乐观锁，单用户量级下可接受，不做跨读写的整段锁。
- **删除的幂等口径**：DELETE 先用 `read_learned`（默认过滤 inactive）确认条目
  在用，已被撤回的条目再删 → 404（不是重新盖 tombstone），避免「删两次」静默改掉
  事件时间。

## 验收

- 无 id 注释的旧文件 GET 后补上稳定 id；重复 GET 不再改文件（幂等）。
- GET 返回 `id` 不含 `line`；PUT/DELETE 按 id 定位，id 不存在 → 404。
- 旧页面：条目被他人编辑后提交 → 409；条目被删除后提交 → 404。
- 编辑保留日期前缀与原 id 注释；删除留 tombstone 语义不变。
- consolidate `_append` 写出的新行无 id 注释（写侧未改），下次 GET 补上。
- 三门通过（ruff / mypy / pytest）。
