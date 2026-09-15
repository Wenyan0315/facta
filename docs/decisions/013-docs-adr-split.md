# 决策记录 · 文档治理：ADR 拆分（2026-09-15）

> 迁自 docs/architecture.md 决策记录拆分惯例（v0.37）；append-only。返回 [architecture.md](../architecture.md)

- **ADR 拆分（2026-09-15，v0.37）**：architecture.md 决策记录累积至 57 条，两处既定触发信号同日兑现（重构评估「S2 收尾执行拆分」+ 三方评审「architecture.md 再长大一轮时拆」）。裁定四件：①`docs/decisions/` 按里程碑一篇（001–012），append-only、不改写旧记录——拆分用一次性脚本机械迁移，57 条逐条 sha256 校验零丢失零改写、标题前缀断言防错位（脚本先全量构建校验、通过后才落盘）；②否决档案独立成活清单 veto-archive.md（跨里程碑持续追加的注册表，不是某天的 dated 记录）；③architecture.md 保留全景图/各层状态/演进主线/路线图/企业级考量 + §五索引表，单文件从 216 行回到 ~150 行；④新里程碑收官动作改为「决策写新篇 → 索引表加行 → 版本号照升」。已知边界：data/learned/constraints.md 里「决策写进 architecture.md」是 2026-09-13 的固化条目（dated 记录不改写），其语义由本文件与 architecture.md 更新规则行接管。
