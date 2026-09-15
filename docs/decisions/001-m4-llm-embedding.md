# 决策记录 · M4 · 模型接入（LLM 与 Embedding）

> 迁自 docs/architecture.md 决策记录（2026-09-15 v0.37 拆分，内容原样保留）；append-only——修正与补充以新记录追加，不改写旧记录。返回 [architecture.md](../architecture.md)

- **LLM 接入（M4 重构）**：不做"每家供应商一个类"，而是 `OpenAICompatibleLLM`（一个类）+ `PROVIDERS` 配置表；环境变量约定 `{PREFIX}_API_KEY / {PREFIX}_BASE_URL / {PREFIX}_MODEL`；加供应商 = 配置表加一行。命令行 `python -m agent <provider>` 切换。

- **Embedding 接入（M4 后半，2026-09-05）**：与 LLM 层同构——`Embedder` 接口 + 双实现（`BagOfWordsEmbedder` 教学版 / `SiliconFlowEmbedder` BGE-M3）+ `get_embedder()` 工厂；`KnowledgeBase(embedder)` 依赖注入。**阈值跟着实现走**：`default_min_score` 是 Embedder 的类属性（词袋 0.35 / BGE 0.55，量纲不同不可混用），调用方不再硬编码。选型依据：业界 RAG 直接上 embedding（无 TF-IDF 过渡），BGE-M3 在硅基流动免费。

- **BGE 阈值校准法**：先跑探针看"相关/垃圾"问题的分数分布（相关 0.645~0.784，垃圾 ≤0.491），阈值卡在两者之间的沟里（0.55），两侧留余量——数据定参，不拍脑袋。

- **Embedding 层对称重构（2026-09-05 Q1）**：`SiliconFlowEmbedder`（供应商焊死在类名）→ `OpenAICompatibleEmbedder`（通用类）+ `EMBED_PROVIDERS` 配置表，与 llm.py 完全对称。要点：①`min_score` 进配置表跟着**模型**走（换模型必须重校准）②embedding 的模型覆盖环境变量用 `{PREFIX}_EMBED_MODEL`，与聊天的 `{PREFIX}_MODEL` 分离——同供应商的聊天/embedding 是两个独立开关，共用会互相覆盖 ③工厂键名统一为供应商名（`get_embedder("siliconflow")`）。回归验证：BGE 指标与重构前逐项一致。

- **key 安全**：key 只存 `.env`（已被 .gitignore 排除），代码只读环境变量，绝不硬编码。

- **网关**：不做独立部署网关服务；`get_llm()` 工厂演进为进程内 Router（按任务路由模型、重试/限流/成本统计）
