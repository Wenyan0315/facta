# 决策记录 · FW 站前端基建试点（2026-09-19）

> 021 四联裁定④的兑现：Preact+Vite 框架化，任务视图重写作试点。返回 [architecture.md](../architecture.md)

- **试点完成**：任务视图 v1（60 行 vanilla：innerHTML 模板 + querySelector 逐个填充）→ v2（Preact 状态驱动）。功能对齐（5s 轮询/状态徽章/交付摘要/防注入），结构换血：`useState` 状态即真相 + `useEffect` 副作用（轮询/竞态守卫/卸载清理）+ JSX 文本自动转义——v1 靠纪律手写 textContent 防注入，v2 结构性免疫。已知病灶「任务视图偶发不切换」属会话切回主视图的 DOM 竞争，任务视图本页未复现；主视图迁移时验证是否随声明式渲染消失。

- **工程布局**：`frontend/`（Vite 项目根：package.json / vite.config.mjs / src/tasks/）构建产物落 `static/fw/`（子目录隔离，不碰 vanilla 三件；主聊天视图渐进迁移）。四裁定：①产物进 git（15KB/gzip 6.4KB 量级，CI 保持纯 Python 三道门不加 node 步骤——触发信号=前端长大到多页面多依赖）；②无 hash 文件名（NoCacheStatic 已禁缓存，hash 只增 diff 噪声）；③复用 style.css（试点不引 CSS 方案）；④node 装 `~/tools/` 官方 tarball（v22.23.2，不装 Homebrew，PATH 入 ~/.zshrc；npm registry 走 npmmirror）。

- **验收抓到的真 bug（构建链第一课）**：浏览器验收发现任务卡片空白——Vite 构建默认把 HTML 里的 script 写成服务器根绝对路径 `/assets/tasks.js`，但产物经 `/fw/` 子目录伺服，404。修法一行：`base: "/fw/"`。教训：**构建工具的默认值假设「整个站点归它管」，嵌入既有服务器时子路径部署必须显式声明**。修后 50 次轮询零失败，卡片/徽章/摘要/轮询稳定性全过。

- **dev 工作流**（后续前端开发的日常）：`uvicorn`（8000）+ `npm run dev`（5173，proxy /api 与共享静态资源）→ HMR。构建命令 `npm run build`（改源码后必须重跑，产物才进 static/fw/）。

- 验收链：三道门全绿（ruff/mypy/295 passed）+ 端点冒烟（/tasks、/fw/assets/tasks.js 200）+ 真实 Run 端到端（deepseek 跑「用一句话解释什么是向量数据库」completed + preview 上屏）+ 浏览器两轮（首轮抓 404，复验通过）。
