# ADR 070：开源兼容轮——多家供应商 + 个人偏好拆出

- 状态：已采纳
- 日期：2026-09-29
- 触发：开源前的「作者偏好 vs 通用兼容」盘点
- 关联：069-open-source-review-p1-fixes（评审修复轮）、068-project-rename-facta（改名）

## 背景

068/069 把项目推到了「可开源」门口。外部用户和作者镜像不同点：

1. **作者只用 DeepSeek 系 + 硅基流动**——`PROVIDERS` 表就这两家。
3. **作者把硅基流动当 embedding 默认**——`assemble.py` 写死 `get_embedder("siliconflow", ...)`，`_rag_missing_reason` 只查 `SILICONFLOW_API_KEY`。
4. **作者的个人约束记忆随仓库分发**——`data/learned/*.md` 全员入库。

这些都是「**作者偏好**」而非「**架构约束**」。开源要的不是「让作者改习惯」，而是「让外部用户不被作者习惯绑架」。

## 决策

### 1. 主 LLM 配置表扩四家：openai / qwen / zhipu / moonshot

`src/facta/core/llm.py` 的 `PROVIDERS` 字典加四行：openai（gpt-4o-mini）、qwen（DASHSCOPE 兼容入口、qwen-plus）、zhipu（glm-4-flash，当前免费档）、moonshot（moonshot-v1-8k）。

- 价目全部标占位，README/.env.example 写明「以官网实时价为准」
- 不在表里的中转商不必加代码——`{PREFIX}_BASE_URL` / `{PREFIX}_MODEL` 环境变量直接覆盖
- 默认仍是 deepseek 系，老用户零感知

### 2. embedding 解耦：FACTA_EMBED_PROVIDER

`src/facta/knowledge/knowledge_base.py`：
- `EMBED_PROVIDERS` 加 openai 行（text-embedding-3-small，min_score 0.5 保守起步值）
- 新增 `EMBED_PROVIDER_DEFAULT = "siliconflow"`（保持作者原有行为）
- 新增 `configured_embed_provider()` 读取 `FACTA_EMBED_PROVIDER`

`src/facta/orchestrator/assemble.py`：
- `_rag_missing_reason` 改按 `configured_embed_provider()` 查对应 prefix 的 key
- embedder 装配改 `get_embedder(configured_embed_provider(), ledger)`
- 未知供应商名给清晰报错（含可选清单），不静默走错路径

`min_score` 仍跟着「模型」走：换 embedding 模型必须重跑校准探针再调（注释明示，0.5 是 OpenAI 起步保守值）。

### 3. learned 记忆移出仓库

`data/learned/*.md` 是助手跑出来的个人偏好/约束/决策——属于运行时数据而非代码资产：

- `.gitignore` 加 `data/learned/*.md`（保留 `constraints.example.md` 模板）
- `git rm --cached data/learned/constraints.md decisions.md other.md`（本地保留，重置为「未跟踪」）
- 新建 `data/learned/constraints.example.md` 教怎么写自己的 constraints
- `data/notes/` 仍入库——那是人工策展的语料库（M3 起就声明）

代码层面无变化：`consolidate.py` / `learned.py` 早已按 `LEARNED_DIR` 动态创建目录（ADR 045 同款路径）。

## 故意没动的边界

- **降级链「自动挂有 key 的备用真模型」**——跨故障域设计合理，作者行为是「主选坏掉自动跨供应商兜底」。要禁掉得加 `FACTA_FALLBACK=off` 开关；当前零信号（外部用户没反馈过跨供应商降级吓人），先不动。**触发信号**：外部用户反馈「账单跳到意外供应商」或「我只要一家」。
- **`FACTA_PROVIDER=mock` 教学组合仍不抽 embedding**——词袋 + 内存库 + 不抽图谱三件套是「零钱离线」的最小闭环，开源用户的快速体验路径靠它保。已 P1-1 兜底：真模型缺 key/依赖时同款退路。

## 触发信号（下次复盘看这十条）

- 外部用户提「我用的家不在这表里」→ 评估是否补行
- 降级链误跨供应商被报告 → 评估 `FACTA_FALLBACK` 开关
- min_score 校准值被打脸（BGE-M3 0.55 / OpenAI 0.5 都待实证）→ 重跑探针与 evals

## 验证

- **测试**：739 passed, 2 skipped（基线 735，净 +4）
  - `test_rag_key_follows_configured_embed_provider`：embed 选 openai 时查 OPENAI_API_KEY
  - `test_rag_rejects_unknown_embed_provider`：拼错供应商给清晰错误
  - `test_rag_openai_key_unlocks_full_path`：配齐 OPENAI_API_KEY 不再降级
  - `test_providers_table_lists_open_source_compat_set`：四家全在表里
- **ruff**：All checks passed
- **mypy**：Success: no issues found in 59 source files

## 影响面

| 文件 | 改动 |
|---|---|
| [src/facta/core/llm.py](../../src/facta/core/llm.py) | PROVIDERS 加四行 |
| [src/facta/knowledge/knowledge_base.py](../../src/facta/knowledge/knowledge_base.py) | EMBED_PROVIDERS 加 openai 行 + configured_embed_provider() |
| [src/facta/orchestrator/assemble.py](../../src/facta/orchestrator/assemble.py) | _rag_missing_reason 按所选供应商查 + embedder 装配改走配置 |
| [tests/test_app.py](../../tests/test_app.py) | 3 条 embed 选择测试 + 旧测试补 `delenv("FACTA_EMBED_PROVIDER")` |
| [tests/test_jev.py](../../tests/test_jev.py) | 1 条 PROVIDERS 表覆盖测试 |
| [.env.example](../../.env.example) | 五大供应商 key 模板 + FACTA_EMBED_PROVIDER 注释 |
| [.gitignore](../../.gitignore) | `data/learned/*.md` 排除，保留 `*.example.md` |
| [README.md](../../README.md) | 兼容表 + 69→70 计数更新 |
| [data/learned/constraints.example.md](../../data/learned/constraints.example.md) | 新建模板 |

## 反向证据（确认作者偏好真就是偏好）

- 「用硅基 BGE-M3」在 commit 记录里是历史选择——不是任何 ADR 写过的硬约束
- 037「教 LLM 用工具」对比表的训练脚本是硅基 API——这是「作者训练时方便」，不是「架构必须」
- OpenAI 兼容接口本身就是为了「换家 = 改 base_url」设计的（023 注释同款）
- data/learned/constraints.md 内容全是个人项目规则——clone 后下游用户没义务遵守

故采纳：偏好与开源兼容放在各自的位置，不混。