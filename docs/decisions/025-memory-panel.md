# 决策记录 · 记忆面板 v1（2026-09-19）

> 021 定位「护城河可视化」的兑现：固化管线的产出不再是黑箱。返回 [architecture.md](../architecture.md)

- **范围（评审药方全集）**：查看 + 编辑 + 删除 learned 三桶（decisions/constraints/other）——「用户对第二大脑的掌控感」。剪裁：用户级记忆（仓库外）不进 v1（触发信号=S5 用户级位置定型）；preferences 桶刻意不开（consolidate 既有隐私红线：能力跟着位置走）。

- **行号定位协议**：GET 返回的 line = 文件 0-based 行号，原样传回 PUT/DELETE。可行依据：固化 append-only（只加尾行），读写窗口内已有行号稳定；写回以「原行数组整重写」保真——空行/手写行原样保留（append-only 固化的兼容前提）。已知边界（v1 接受，git 兜底）：读写窗口内双标签并发互删错位（单用户概率极低）；窗口期固化 append 与重写全文的竞争。

- **坏行宽容（清理幻觉污染的正当路径）**：不符合 `- [日期] 内容` 格式的手写行不炸——date=null、content=原行全文、可删可编辑（编辑原样替换）；好行编辑保留原日期前缀（时间戳归程序管，consolidate 纪律「不信模型」的 UI 延伸：也不让人手滑改掉）。

- **实现**：服务端原语 `src/agent/memory/learned.py`（read/update/delete_line，10 用例锁定含空行保留/尾换行/越界 404/穿越白名单）+ app.py 三端点（GET/PUT/DELETE /api/learned[/{cat}/{line}]，category 白名单挡路径穿越，空内容 400）；前端 FW 新栈第二入口 `frontend/src/memory/`（/memory 端点 + index.html 侧栏「记忆面板」链接）。多入口构建红利：Vite 自动抽 preact 共享 chunk（hooks.module.js），tasks.js 15KB→1.1KB。

- **前端教学靶心（对照任务视图的只读轮询）**：状态分层——全列表 entries 归 App（一份真相），编辑框草稿/删除确认态归 Entry（条目级状态放条目组件）；操作后 refetch（改/删成功→重拉→新状态驱动新 UI，行号协议自洽）；受控组件（textarea 绑本地 state）。删除走行内二次确认（点删→变「确认删除」），不引弹窗组件。

- 验收：三道门全绿（314 passed，+10）；浏览器完整闭环——三组渲染/日期徽章/hover 显按钮、编辑流（改文保存日期不变）、删除流（二次确认→条目消失 16→15、其余逐条不变）、验收测试条目删后 learned 文件字节级还原（git 零 diff，协议无副作用验证）。
