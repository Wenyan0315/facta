# 决策记录 · 混合检索评估与裁定（2026-09-19）

> 待讨论清单最后一条的兑现：让数据说话。返回 [architecture.md](../architecture.md)

- **评估设计**：三路并评 + miss 归因。语料 = data/notes（线上 14 篇）+ evals/corpus（Context7 经 HttpMcpClient 抓取 Preact/FastAPI/SQLite 文档 ~8 万字；evals/fetch_corpus.py 可复跑，语料版本化进 git）——补掉「14 篇小库 miss 不真实」的规模边界。题库 13→30 题并形态分层标注（paraphrase 概念改写 17 / exact 精确词 10 / absent 无答案 4）；指纹做块级验证（抓出一处 markdown 跨行断字假 miss——子串标注的经典坑）。三路：**vector**（评估参数 top5 无闸门测排序 + 生产参数 top3+闸门测真实命中，与 search_notes 同参）、**grep**（极简词覆盖计数，与词袋同款 tokenize 保公平——真实 rg 短语匹配只会更准，本路数据是 grep 增量下界）、**union**（两路并集，混合形态模拟）。

- **数据（BGE-M3 生产配置，线上真实路径）**：R@5=0.96（语义排序接近满分）；生产 miss 5/26（19%）；**grep 补救率 0/5 = 0%**（立项阈值 ≥50%）。词袋对照：生产 miss 21/26，grep 补 4（19%）——全部是「中文 query × 中文笔记 + query 含专名」题（RAG/Agent/json_each）。

- **三个数据洞察**：
  1. **grep 的增量随向量能力增强而衰减**：词袋时代 grep 救专名题（19%），BGE 时代 0%——语义模型自己把专名题做掉了（hydrate/computed/Depends/STRICT 等 exact 题 BGE 全命中）。混合检索治的是「向量弱」，不是「向量漏」。
  2. **中文 query × 英文文档是 grep 的结构性死区**：字面 token 零重合，真实 rg 同样救不了（除非查询先改写成英文词）——而跨语言语义匹配恰是 BGE 强项。
  3. **生产 miss 的主因是闸门/深度截断而非排序**：5 个 miss 里 3 个 vec@5 有命中（top5 排上了、top3+0.55 丢了），仅 2 个 top5 全丢——改进杠杆在参数调优与查询改写，不在加检索路。

- **裁定：混合检索不立项，本条关闭**。替代方向记档：①截断类（3 题）→ top_k/闸门调优留真实使用反馈驱动（不主动调——BGE absent 闸门 3/4 拦住、React fiber 漏过提示降闸门会放大误放行，闸门有双向张力）；②语义鸿沟类（2 题 top5 全丢）→ 查询改写（search_notes 工具描述已引导模型换词重试；S5 Agent 对象时可评估「检索 miss 自动改写重试」）。**复活触发信号**：语料再涨一个量级使 R@5 显著回落，或真实使用出现「query 含精确专名但向量没排上」的 miss 案例。

- 顺手记档的已知边界：BGE absent 闸门假阳性（相关主题在场时相似度超线——React fiber vs preact react compatibility）；grep 路停用词表手工维护（中文逐字 tokenize 的固有噪声）；sqlite.md 等语料含 Context7 主题查询带来的重复段落（真实语料本有冗余，如实保留）。

- 验收：指纹块级验证 30/30（修正 1 处跨行断字后）；三道门全绿（ruff/mypy/295 passed）；BGE 评估实际 API 成本 ~0.01 元量级（embed 30 query + 604 块索引一次）。
