# 决策记录 · 记忆面板用户级分栏（2026-09-25）

> [025](025-memory-panel.md) 剪裁项 + [034](034-m6.5-user-memory.md) 裁定 6 的兑现：面板从「项目级三桶」扩到「项目级三桶 + 用户级一栏」。返回 [architecture.md](../architecture.md)

## 立项：两处触发信号不一致，以 025 为准

- 025 剪裁写的是「触发信号 = S5 用户级位置定型」——**已响**：M6.5 建成 `paths.user_memory_path()` → `~/.personal-agent/user.md`（`CORTEX_USER_MEMORY` 可覆写），固化管线双作用域落盘。
- 034 裁定 6 写的是「触发信号 = 用户级条目积累到需要查看/编辑的真实摩擦」——**未响**：写本篇时 `~/.personal-agent/` 目录尚不存在，用户级条目 0 条。
- **裁定以 025 为准**。理由：面板的价值是可见性/信任（「护城河可视化」），不依赖条目数量；等条目攒够再做，等于让用户在最需要核对「它到底记住了我什么」的那一次（第一批画像落盘时）看不见。034 那条信号据此作废，本篇为唯一出处。
- **如实记录的后果**：分栏首屏是「暂无条目」，且固化管线写出第一条 user 条目前一直是。这是正确行为不是 bug，验收按此口径。

## 裁定（三条）

1. **`user` 当第四个伪 category，零新端点**。GET/PUT/DELETE `/api/learned[/{category}/{line}]` 原样复用，服务端只加一个路径解析 `_learned_path(category)`，白名单从 `CATEGORIES` 扩成 `CATEGORIES + ("user",)`；前端 `CATEGORY_ORDER` 多一项 + 一个中文名。依据：user.md 与项目桶是**同一种落盘物**——同款 `- [日期] 内容` 行、同一把 `LEARNED_LOCK`、同款坏行宽容、同一条 append-only 契约（`consolidate._append` 只是按 scope 分叉了目标路径）。另开一套端点 = 把 `read_learned/update_line/delete_line` 的语义复制四份。
2. **路径必须走 `paths.user_memory_path()`，不在 app.py 存常量**。它是函数不是常量（`CORTEX_USER_MEMORY` 覆写点）；面板与 agent 注入侧（`assemble.py`）必须同源。存常量的事故形态很具体：测试用 tmp_path 覆写了环境变量，面板却仍盯着真用户 home 里的 user.md——**拿测试操作生产隐私文件**。
3. **不做「手工新增条目」按钮**。写入侧只归固化管线（萃取→审查→硬校验→落盘），其中 `_SENSITIVE_PATTERNS` 敏感凭证禁令是代码级硬边界。面板开手工添加入口 = 绕过审查与禁令，把手机号/API key 写进一个 **git 管不着**的文件。要手写就自己开文件——这条路一直开着。

## 风险（本次核心）

| # | 风险 | 对策 |
|---|---|---|
| 1 | **git 不兜底**：`data/learned/*.md` 进 git，误删可 `git checkout`；`~/.personal-agent/user.md` 在任何仓库之外，删了就是删了 | 不加 `.bak`（单份同目录备份第二次误删即覆盖，防护弱到接近装饰，还要新增文件语义）。改为把不可逆性写在 UI 上：用户级栏固定一行提示「跨项目生效 · 不进 git，删除不可撤销」，删除仍走 025 的行内二次确认 |
| 2 | **暴露面内容更私密**：项目桶装技术决定，user.md 装偏好/习惯/行程 | 现状服务只绑 `127.0.0.1` 且全端点无鉴权，风险等级不变。**红线**：任何把服务绑到非 loopback 或加公网隧道的动作，必须先补鉴权并另立 ADR——那时 user.md 从「本机隐私」变成「网络泄露面」 |
| 3 | **改完不热刷新**：prompt 是装配时读盘一次的快照（`build_default_agent`），面板删掉一条，已在跑的会话仍带着它 | 不加热刷新（025 已挂信号：真实使用发现「刚固化的它不知道」再工具化）。前端文案不承诺即时生效 |
| 4 | **整重写 vs 固化 append 竞争** | **不是新增风险**：`consolidate._append` 的 user 分支与项目桶在同一把 `LEARNED_LOCK` 内落盘，`update_line/delete_line` 持同锁。复用即继承 |
| 5 | **双标签页行号错位**（025 已知边界）：同一 bug 在无 git 兜底的 user.md 上后果更重 | 接受。加乐观锁（etag/version）要给三端点都改协议，为低频手滑付三份复杂度不值。触发信号=真出现过一次错位丢条 |
| 6 | **文件不存在**（新用户，也是当前状态） | `read_learned` 对缺失文件返回 `[]` → 显示「暂无条目」；PUT/DELETE 走既有 `path.is_file()` → 404。不新增分支 |

## 剪裁

- 不做用户级的 preferences 分桶（025/034 同款剪裁仍有效：量小，检索分层挂 032 裁定二 v2 信号）。
- 不做用户级记忆的检索/分页（同上，全量注入零压力）。
- 不动项目级三桶的任何既有行为（纯增量：白名单 +1、路径解析 +1 函数、前端 +1 栏）。

## 验收

- 三道门全绿（ruff / mypy / pytest，基线 484 passed）
- 新增测试（`tests/test_learned.py` 同族）：① `_learned_path("user")` 跟着 `CORTEX_USER_MEMORY` 走；② GET 含 user 组；③ PUT/DELETE user 生效且项目桶文件字节不变；④ 未知 category 仍 400；⑤ 路径穿越（`../notes`、`user`）仍被白名单挡住
- `npm run build` 后浏览器闭环：四栏渲染、用户级提示文案在、编辑保存日期不变、删除二次确认后条目消失且 user.md 少一行

## 验收结果（2026-09-25）

- 三道门全绿：ruff All checks passed ｜ mypy 58 文件无问题 ｜ pytest **487 passed, 2 skipped**（基线 484，+3）
- 测试落在 `tests/test_learned.py`（10 → 13）：`test_learned_path_user_follows_env`（①+白名单枚举）/ `test_api_user_scope_roundtrip`（②③，项目桶按字节比对）/ `test_api_user_scope_missing_file_and_guards`（④⑤+文件不存在）
- **口径修正**：验收第 2 条⑤原写「路径穿越（`../notes`）」——实际防线是白名单枚举，`notes`/`preferences` 直接 400，穿越串压根到不了拼路径那步；测试按白名单口径钉，不造一个防不住的假想敌。
- API 级实机（8010 最小 AppContext 实例 + curl，靶文件 `/tmp/umem-041/user.md`；8000 被用户常驻服务占着，且真入口 `assemble()` 会跑图谱抽取烧额度，故绕开）：GET 出 user 组 3 条（含坏行 `date=None`）→ PUT `user/0` 200 且日期保留 → DELETE `user/1` 200、剩余行号重排 → `PUT preferences/0` 400、`DELETE user/9` 404 → `git diff --stat -- data/learned` 输出为空（项目桶未被碰）
- 浏览器闭环（同一实例）：四栏标题 `决定与理由/约束与教训/其他事实/用户级记忆`；`.memory-hint`「跨项目生效 · 不进 git，删除不可撤销」在且**只在**第四栏；坏行日期显示「—」；点「删除」→ 行内变「确认删除/取消」且此时未发请求 → 点「确认删除」→ 网络面板见 `DELETE /api/learned/user/1` + 随后 refetch → 第四栏剩 1 条、项目三桶条目数不变（2/9/13）、`user.md` 从 2 行变 1 行
- 未取证一项：截图（`browser_take_screenshot` IDE 超时，未重试——DOM 断言已覆盖同一事实）
- 交付副作用：8000 上运行的常驻服务内存里是旧 `app.py`，需重启才有第四栏；静态资源已随构建更新 → 重启前会出现「前端有第四栏、后端只返三桶」的短暂不一致（第四栏空，不报错）
