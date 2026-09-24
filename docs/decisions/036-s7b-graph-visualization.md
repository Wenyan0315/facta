# 决策记录 · S7b 知识图谱可视化面板（2026-09-24）

> S7 第二站：把「知识地图」做成能探索的一等能力——力导向图面板 + 重建图谱。返回 [architecture.md](../architecture.md)

- **两个选型（开工拍板）**：
  - **P1 自研 SVG 力导向**（用户从「自研 / D3-force / Cytoscape」三选中拍板自研）：零运行时依赖，贴合项目「从零造轮子理解原理」调性（vector store 的 InMemory 版同哲学）。力模拟四件（库仑斥力/胡克弹簧/中心引力/阻尼）约 80 行纯函数，个人图谱几十到几百节点完全够用。D3/Cytoscape 是黑盒 + 引入依赖，与「理解原理」调性相悖。
  - **P2 进阶交互**（用户纠正我的「基础+探索」推荐，再次命中设计原则第 7 条）：我拿「本轮范围别太大」当砍范围理由——但搜索定位、路径高亮、类型过滤正是「知识地图」区别于静态图的一等能力，不该砍。进阶清单：拖拽节点/滚轮缩放/拖空白平移/单击高亮 1 跳邻居/再点第二个节点高亮最短路径/搜索定位/按类型显隐/关系四色图例/孤岛·枢纽标注。

- **架构裁定**：
  - **图数据一次全量拉回前端**：`GET /api/graph` 返回 nodes/edges/stats，图小，搜索/路径 BFS/过滤全在前端内存算——不需额外交互端点（前端 BFS ≤3 跳与后端 `query_graph` 的 `path` 原语同语义，两端对同一「最短关系链」概念）。
  - **重建端点复用抽取管线**：`POST /api/graph/rebuild` = force 全量重抽（清空指纹 → sync_graph → 落盘），与 S7a 工具层 `_sync(force)` 同款逻辑；同步端点花 LLM 钱，前端 loading 状态等待。
  - **图级并发锁 GRAPH_LOCK（放 graph.py）**：S7b 引入新并发面——常驻 Web 进程下，面板重建端点（请求线程）与对话内 sync_graph 工具（worker 线程）可能并发读写同一 GraphStore。sync_graph 是多步复合操作，dict 单步 GIL 原子性覆盖不了整段。锁属于数据层「图是共享资产」的并发协议（调用方工具/server 都是客户），放 graph.py 而非 app.py。

- **AppContext 挂 graph**：S7a 只把 graph 注入 ToolContext（供 query_graph 读），server 层拿不到。S7b 给 AppContext 加 `graph` 字段（`field(default_factory=GraphStore)`，mock 教学路径空图不破坏既有 `_make_ctx`），assemble 里显式挂载——surface 面板端点与查询原语共用同一份活图。

- **前端 skeleton 复制 tasks/memory**：第三入口 = `frontend/src/graph/{App,api,force,main}` + `graph.html` + vite input，产物进 `static/fw/`（进 git）。全宽覆盖（`.graph-main` 顶掉 `#chat` 的 820px 列表宽度——力导向图不比列表）。SVG marker id 用 ASCII（中文 fragment 在 `url(#…)` 引用有编码坑，避开）。

- **实机验收抓到的首帧 bug（已修）**：`positionsRef` 在 `useEffect`（渲染后）才初始化，首次数据渲染时 SVG 边/节点已读 `positionsRef.current[a].x`——空数组 `TypeError: reading 'x'`，React 树崩溃。修复：位置初始化提前到 `load()`（数据一到位即初始化，渲染时已就绪），`useEffect` 只管启动力模拟。**教训：派生数据（位置）的初始化时机必须早于首帧消费它的渲染**——useEffect 是「渲染后」，不是「渲染前」。

- **验收**：pytest 457 passed（+3 graph 端点/页面）；ruff/mypy 全绿；CI 绿；真模型浏览器验收——35 节点/37 边渲染、类型过滤（技术节点隐藏）、搜索定位（匹配「向量/实体向量」）、箭头 marker、拖拽 cursor 全通过。