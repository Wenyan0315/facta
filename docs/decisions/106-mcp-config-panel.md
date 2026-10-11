# ADR 106：MCP 可视化配置面板——全编辑档 + 待重启生效语义

- 状态：已裁定（2026-10-11，里程碑范围单项定稿 + 能力档位全编辑，均经
  用户拍板）；实现与验证读数见文末
- 日期：2026-10-11
- 上游：[102](102-n07-hygiene-dispositions.md) N07-③（排期拍板）、
  architecture 待讨论「前端配置界面」①（MCP 粘贴配置，替换手动编辑
  JSON）、[044](044-memory-layer-mcp.md)（A 径前置，清单里已有
  `enabled:false` 典型样本）、[042](042-notes-panel.md)/[089](089-learned-panel-stable-id-and-conflict.md)
  （乐观锁先例）、[093](093-ui-surface-fork-status-memory-errors.md)（错误
  约定）、[094](094-backlog-readme-frontend-ci.md)（FW 多入口 + CI 构建闸）
- 推翻关系：无（344 行触发信号「手动编辑出现真实摩擦或产品线空位」由
  102 拍板以「排下一里程碑」提前兑现——排期是产能分配，非摩擦驱动）

## 背景

344 行（前端配置界面）把「MCP 粘贴配置」立为用户点名要做：配置从
「写文件」升级到「填表单」。前置早已清（044 落地后清单里 notes/ctx7/
memory 三台正好覆盖面板全部典型形态：command 型启用、url 型禁用、
command 型禁用），本里程碑（106 专项，单项定稿）把它做完。校验不新造：
`mcp_config.load_server_specs`（command/url 二选一、必给一个、prefix 缺省）
就是写侧的单一真值源。

## 设计

### 服务端（`mcp_servers.json` 的读写 API，RESTful 与 notes/learned 同款）

- `GET /api/mcp`：返回 `{managed_path, effective_path, env_override,
  hash, servers, active}`——`managed_path` 恒为仓库默认文件（面板的
  写入目标），`effective_path` 是 `MCP_SERVERS` env 指向的生效文件
  （未设时与 managed_path 相同），`hash` 是整文件 base_hash（乐观锁
  令牌），`active` 是当前进程实际接入的服务器名单（文件 vs 运行时
  对照的「运行时」半边）。
- `POST /api/mcp/servers`：新增条目（body 含 `base_hash`）；撞名 409。
- `PUT /api/mcp/servers/{name}`：替换该条目的表单字段
  （command/url/prefix/enabled/timeout）+ `base_hash`；不存在 404，
  hash 不匹配 409（**整文件锁**，042 notes `base_hash` 同款——面板要
  替换的恰是手编场景，盲覆盖会吞手改）。
- `DELETE /api/mcp/servers/{name}`（query 带 `base_hash`）：不存在 404，
  hash 不匹配 409。
- 写侧三步不变式：读现文件 → 改条目 → **新内容先过 `load_server_specs`
  校验再原子落盘**（P0-3 手法）——校验失败的请求整体拒绝，磁盘上
  永远不会有让下一次启动走「MCP 工具静默全消失」分支的坏清单
  （S8a 边界①记过的最难查形状）。

### 生效语义（对照展示，不做热重载）

MCP 装配在 `assemble()`（进程启动一次，per-session registry 搬运同一批
Tool 对象）——改配置**重启才生效**，这是面板必须诚实呈现的产品语义：
文件清单与 `active` 不一致时面板显示「待重启生效」。不做一键重启/
热重载（要动 assemble 生命周期，与本面板「管文件不管进程」的边界
不成比例；触发信号=待重启提示成为日常摩擦）。

### env 覆盖与密钥边界（两条诚实护栏）

- `MCP_SERVERS` 设置时面板**仍只管仓库默认文件**，顶部显眼提示
  「当前生效的是 MCP_SERVERS 指向的个人文件，面板改动不影响当前
  运行配置」——面板不读写 env 指向的文件（密钥文件不进仓库也
  不经 UI 伺服，mcp_config 模块注释的纪律）。
- **headers 不暴露**：GET 只返回 `has_headers` 布尔（密钥值不出现在
  任何响应里），PUT 替换条目时服务端保留原 headers——含密钥的配置
  从面板这条路写不进仓库文件（当前唯一远程 ctx7 无 headers，零真实
  需求；触发信号=真实出现需带 key 的远程服务器）。

### 前端（FW 站第五入口，与四入口同款伺服）

- `frontend/src/mcp/`（api.js + App.jsx + main.jsx）+ `mcp.html`；
  vite inputs 加 `mcp`，产物进 git（CI 构建比对闸自动覆盖，094）；
  `app.py` 加页面路由 `/mcp`；`index.html` nav 加一行「MCP 配置」。
- UI：顶部生效状态区（env 警告横幅 + 待重启提示）→ 服务器列表
  （name + 类型 + command/url + prefix + enabled 开关 + 编辑/删除）→
  编辑表单（command/url 按类型二选一切换、prefix、timeout、enabled）
  → 新增按钮。保存成功后 refetch（拿新 hash + 刷新对照状态）。
- 409/404 处理沿用 089/093 模式：内联错误横幅 +「重新载入」按钮。
- name 是条目身份（URL 参数），不做改名编辑——删了重建即可达
  （懒惰阶梯：改名 = 改身份 + 撞名语义纠缠，不值）。

## 边界（范围外，均有触发信号）

- 连通性测试按钮（「测试连接」要真拉子进程/发 HTTP；触发信号=面板
  落地后出现「配了连不上」的排查需求）。
- headers/密钥编辑（见上）。
- Skill 可视化配置（skill 未实现，挂 S6 技能包立项——344 行原文）。
- 热重载/一键重启（见上）。

## 验收

- [x] 端到端（自动化链路）：面板 API 开关 memory 服务器 → 磁盘文件
  如实变更（真进程 8199 端口往返验证）；「重启后工具出现」由既有
  测试族拼合覆盖——test_mcp_config 钉「enabled 服务器被
  assemble_servers 拉起注册」、面板钉「PUT 写进文件」、assemble 钉
  「启动读同一清单」，三环字面同源。
- [ ] 浏览器侧用户实测收口：/mcp 页面开关 memory 服务器并保存 →
  重启服务 → 对话里 memory__ 工具出现/消失。
- [x] 写侧校验不变式：任何写请求不可能让磁盘留下过不了
  `load_server_specs` 的清单（`test_write_rejects_command_and_url_both`
  钉 400 + 磁盘不动 + 无 .tmp 残留）。
- [x] 三门 + 基线：ruff 0 error、mypy 64 files clean、pytest
  **895 passed, 7 skipped**（880 基线 + 15：面板端点 14 + Playwright
  冒烟 1）。
- [x] CI 前端构建闸过（产物进 git：fw/mcp.html + assets/mcp.js）。

## 验证结果

- **实现形状**：`assemble_servers` 返回 `(client, name)` 对（名字跟着
  客户端走，AppContext 记 `mcp_active`；下游 `__main__.py`/github_bot
  的 close 循环零改动）；端点四条（GET /api/mcp + POST/PUT/DELETE
  /api/mcp/servers/{name}）；404 先于 409（条目没了就是没了，「重新
  载入」救不了不存在的条目）；FW 第五入口（frontend/src/mcp/ 三件套
  + vite input + 路由 /mcp + index.html nav-link）。
- **真进程端到端**（8199 端口 mock 档，`mcp_active=["notes"]` 模拟
  已装配）：GET 返回三台清单 + active=[notes]（对照语义成立——
  enabled 的 notes 在名单、enabled:false 的 ctx7/memory 不在）→
  PUT 启用 memory（200 + 新 hash）→ 磁盘文件如实变更 → 还原。
- **密钥护栏实测**：GET 响应全文不含 headers 值（`test_get_returns_
  manifest_and_hides_headers` 用 `sk-test` 诱饵钉住）；PUT 替换条目后
  原 headers 原样保留在磁盘（`test_put_toggles_enabled_and_keeps_
  headers`）。
- **已知行为（非缺陷）**：面板写入的条目把 timeout/enabled 恒写全
  （原手编条目可省略用读侧缺省）——面板成为写入方后的格式归一，
  语义逐字段等价；手编爱好者会看到一次性格式 diff。
