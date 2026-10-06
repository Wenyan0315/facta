# ADR 085：会话 fork + Pi 六段式摘要与文件清单

- 状态：**已批准**
- 日期：2026-10-06
- 立项出处：[competitive-roadmap P1-5](../internal/competitive-roadmap.md)（v0.5 编排补遗）
- 关联：073（摘要决定节锚点与程序校验）、077（决定节轨迹对账纪律）、S8a（会话身份=文件名）、P1-6（create 全程持锁评审修复）、P1-7（`is_readonly` 只读判定）

## 背景

roadmap P1-5 原文：

> 会话树操作补齐。来源：Pi 的 /tree /fork /clone + compaction 摘要格式
> （Goal/Constraints/Progress/Key Decisions/Next Steps/Critical Context +
> readFiles/modifiedFiles 累计）。改动：① 会话支持 fork/clone（从任意节点开
> 分支重试）；② compaction 摘要采用 Pi 的六段式结构并累计已读/已改文件清单，
> 压缩后 agent 不重复读文件。

两处病灶：

- **没有 fork**：长会话走到死胡同时，用户只能开新会话，整段上下文（已建立的
  事实、偏好、决定）随之丢失；「换个思路重来」≠「忘掉一切重来」。
- **摘要不提示已读文件**：压缩后 agent 靠摘要正文猜「我读过哪些文件」，猜漏了
  就重复 `read_file` 已经读过的东西——压缩的收益被重复读文件吃掉一部分。

## 拍板

### ① fork = head-fork（复用 create + load，不做树）

数据模型是**扁平消息列表**，不是树。「从任意节点开分支重试」需要树结构 +
节点寻址，而当前既无树节点模型，也无前端树 UI 或 `/tree` 命令——为一个
尚未出现的形态预建树，违背活 spec「方向提前定、细节临期定」。

head-fork 满足 P1-5 的两条真实验收：

- 「换个思路重来」不必开新会话丢上下文——fork 出的是**完整副本**（底片 +
  摘要 + 游标 + 标题 + 计划棋盘），换思路从头继续即可。
- 「fork 会话可独立继续」——新 id 是全新物理文件，与源会话解耦，互不影响。

实现收敛为一行语义：`SessionStore.fork(sid) = create(load(sid))`。`load` 从
JSON 重建返回全新 `Session`（天然深拷贝），`create` 已含 id 分配 + 落盘 +
进程内锁 + flock 跨进程锁（P1-6 / ADR 071 现成），不必重写任何分配/落盘逻辑。

- **不做树结构、不做节点寻址、不做 `/tree` 命令**——ponytail「删优于增」：
  树是「从任意节点开分支」的字面实现，但当前无消费方。**触发信号**：前端
  出现分叉视图需求、或用户要「从第 N 轮处重试」时，再引入树节点寻址与
  `fork(sid, from_index=N)` 语义。
- **fork 只读源、建新会话，不走 `_require_writable`**——fork 不写源会话，
  源正在被 worker 独占时 fork 是安全的（读到的是当刻落盘的快照）。只过
  `_require_session`（源必须存在）。

### ② 六段式摘要 + 确定性文件清单（不经 LLM）

六段式：**【目标】/【约束】/【进展】/【关键决定与约束】/【下一步】/【关键上下文】**。

其中【关键决定与约束】不是新段，而是**复用现有决定节**（073/077 的三段：
有效/被取代/已撤回）。理由：`_DECISION_TAG = "【关键决定与约束】"` 是程序校验
锚点（子串匹配），`_RETRACT_TAGS` 轨迹丢失检测依赖它——Pi 的 "Key Decisions"
与 073 决定节语义同源，强拆会破坏对账纪律与既有测试。段头必须精确为
`【关键决定与约束】`，三段语义、锚点 `被取代：`/`已撤回：`/`不许静默消失`/
`曾讨论过该计划` 原样保留。

文件清单走**确定性路径，不进 LLM**：

- 新增 `collect_file_activity(messages) -> tuple[list[str], list[str]]`：
  从完整 append-only 底片的 `tool_calls` 提取 `name == "read_file"` 的
  `arguments.path` 进已读清单、`name == "write_file"` 的进已改清单；
  `json.loads(tc.get("arguments") or "{}")` 解析，去重保序（按首现顺序）。
- 「累计」由**每次扫描全量底片现算**实现——不要 Session 新字段、不要新游标、
  不要持久化改动：底片是 append-only 全量，现算天然就是累计，且与底片永远
  一致（不存在「改了内容忘了同步清单」的漂移，同 `derive_title` 的哲学）。
- `build_payload` 在有 summary 时调用 collect，若有清单则以确定性 system
  文本块（`【已读文件】`/`【已改文件】` + 逐行 `- path`）**追加到摘要消息
  content 尾部**——确定性拼接，LLM 不参与清单生成，模型只负责「据此别再读」。

**负决策**：

- 不让 LLM 在摘要里「顺便记文件清单」——文件路径是精确数据，交给模型必
  漏写/错写；确定性提取零 token、零幻觉，且「哪些文件读过」本就在底片
  `tool_calls` 里躺着，是现成真值源。
- 不给 Session 加 `read_files`/`modified_files` 字段——现算即可，加字段等于
  为第二份真值买单（与 P1-7「不硬编码只读名单」同哲学）。
- 不把六段式做成严格 schema 校验（每段都必须在场）——073 已定「宽容方向」：
  决定节缺失只 warning 照用不阻断。六段里的其余段是「引导模型写全」的软结构，
  不做段级程序校验（那会为格式漂移付重试 token）。

## 正面（本项落地的位置）

- **`src/facta/memory/store.py`**：`SessionStore.fork(sid, *, now=None) -> str`，
  实现为 `return self.create(self.load(sid), now=now)`。
- **`src/facta/server/app.py`**：新增 `POST /api/sessions/{sid}/fork`
  （`status_code=201`），`_require_session(sid)` 后返回 `{"id": ctx.store.fork(sid)}`。
- **`src/facta/memory/compressor.py`**：改 `_SUMMARY_PROMPT` 为六段式（保留
  决定节三段语义与锚点）；新增 `import json` + `collect_file_activity`；
  `build_payload` 在有 summary 时注入文件清单文本块。

## 边界（诚实登记）

- **head-fork 不是 tree-fork**：「从任意节点开分支」的字面语义（从第 N 轮处
  截断重试）本项不实现。需要时再引入节点寻址，届时 `fork(sid)` 增 `from_index`
  参数，head-fork 是 `from_index=len` 的特例——本项语义可无痛升级，不是死路。
- **fork 的源会话快照是「当刻落盘」**：若源正被 worker 写入中，fork 读到的是
  上一次 save 的快照（源会话外写已被 `_require_writable` 挡住，内写由 worker
  独占）。教学规模下单轮内消息增量不大，可接受；触发信号＝需要「实时 fork
  在跑会话」时，把 fork 也纳入 `_require_writable`。
- **文件清单只在「有 summary」时注入**：无 summary 时是 M6.1 全量直发语义
  （原文都在 payload 里，文件清单是冗余）。「已改文件」从 `write_file` 提取，
  不覆盖其它写类工具（`write_note`、`run_command` 落盘文件不在内）——那是
  知识库/命令产物，不是「读文件清单」要防的重复读。

## 验收

- `SessionStore.fork`：新 id ≠ 源 id、新文件存在、内容（messages/summary/
  summarized_upto/title）与源一致、改新会话不影响源（load 深拷贝）。
- `POST /api/sessions/{sid}/fork`：201 + `{"id": new}`；源不存在 404、id 非法 400。
- `collect_file_activity`：从 tool_calls 提取 read/write 的 path，去重保序、
  无 arguments 或缺 path 不炸。
- `build_payload`：有 summary + 有清单时，摘要消息 content 尾部含
  `【已读文件】`/`【已改文件】` 块；无清单时不含（无 tool_calls 场景零影响）。
- 六段式：`_SUMMARY_PROMPT` 含六个段头与 077 对账锚点；决定节缺失/轨迹丢失
  既有测试仍绿（宽容方向不变）。
- 三门通过（ruff / mypy / pytest）。
