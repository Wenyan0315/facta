# 058 外部锚点选型：LongMemEval S 臂 36 题（P0-9 解①）

日期：2026-09-28
状态：已拍板，已实现（`evals/lme_eval.py`）

## 背景

刺 #4「自己出题自己考」：046 冻结集提供的是纵向可比（与自己的 bash 基线），
横向可比性（与竞品/论文数字同尺）零进展，roadmap P0-9 解① 一直未动。
本 ADR 记录解① 的选型、装配决策与判定口径。

## 地面真值（选型前逐项核实，全部实机验证）

| 事实 | 读数 | 对选型的影响 |
|---|---|---|
| Docker | 未安装 | SWE-bench 官方 harness 依赖容器跑 repo 测试，本机不可行 |
| Python | 3.14 | 多数基准 harness（datasets、evalscope 等）未适配 |
| datasets / evalscope | 未安装，pip 装不动 3.14 轮子 | 排除「装个评测框架」路线 |
| HuggingFace 直连 | 不通 | 走 hf-mirror.com 镜像下载成功 |
| SSL | cafile=None（企业代理环境） | curl 下载需 `-k` 或镜像证书链正常时才通，实测镜像可下 |
| LongMemEval S 臂 | 单文件 265MB，500 题，每题 ~48 个 haystack 会话 | 全量灌库是真「大海捞针」，与论文主榜同口径 |
| `data/notes/` | 16 文件，是冻结集 r1/i3/i4 的 verify 计数基准 | LongMemEval 灌库绝不碰它——每题用独立临时目录 |
| bge-m3 批量 embed 上限 | n=32/64/128 全部成功，dim=1024（探针实测） | 最大单会话 ~114 块 < 128 ⇒ `sync_notes` 一文件一次 embed 够用，无需切子批 |
| 子集文件 | `lme_subset.json` 18.6MB | 已进 `.gitignore`（`data/bench/` 整目录），可 `--build` 一键重建 |

## 选项

- **甲：SWE-bench Verified 子集**（roadmap 原文点名的方向）——本机不可行
  （Docker 未装 + Python 3.14），且测的是 repo 级编码能力，与本项目
  「个人 agent + 对话记忆 + 笔记检索」的主轴错位。
- **乙：LongMemEval S 臂 30-50 题**——公开基准、无 Docker 依赖、方向与
  本项目记忆/检索主轴一致；每题 ~48 会话全量灌库，可比论文主榜量级。
- **丙：继续扩冻结集**——不是外部锚点，不解决刺 #4，排除。

**用户拍板（逐字）：「乙：LongMemEval S 臂 30-50 题」。**

roadmap 原文（P0-9 line 115 点名 SWE-bench）按纪律不改写，只在本 ADR 与
roadmap 状态行尾部追加记录选型偏移。

## 子集口径

6 种题型 × 每型按 `question_id` 排序取前 6 = **36 题**（确定性抽样，可复现，
合 046「不出随机题」纪律）。题型：single-session-user /
single-session-assistant / single-session-preference / multi-session /
knowledge-update / temporal-reasoning。

## 装配决策（最小装配，不走 assemble()）

- 每题 `tempfile.TemporaryDirectory()` + `KnowledgeBase(embedder)`
  （InMemory store）——零跨题污染、零磁盘副作用；不走 `assemble()` 是为
  避免 monkeypatch 五六个模块常量、避免污染 `data/graph.json`。
- `ToolRegistry()`（audit=None 不落盘）+ `register_builtin(registry,
  ToolContext(notes_dir, kb, llm=internal_llm))` + `build_default_agent(registry,
  None)`（learned_dir=None 不注入项目知识）。生产同款 `sync_notes` 灌库、
  同款 `run_turn` 循环、同款 `max_tool_rounds=5`。
- **`ensure_persona(session, agent)` 必须显式调**——第一轮 36 题全灭
  （答题层 0/36、检索层 hit@3 却 100%）的根因就是漏了它：`run_turn` 不注入
  system prompt，人设由装配工厂 `build_agent` 内部种（S8a）。裸 `Session()`
  下模型无人设裸跑，对英文题回罐头客服问候、0 工具轮。这是 harness 级
  装配缺陷，不是机制问题——修复只动了 `evals/lme_eval.py`，没为分数动 `src/`。
- 文件名带日期 `{day}-{idx:03d}.md`：冒烟实测过反面——无语义 hex 命名
  （`000-answer_a7b44747_1.md`）时 agent 调完 `list_notes` 判「看不出内容」
  直接反问，根本没走 `search_notes`（测的是命名混淆不是记忆能力）。
- `haystack_dates` 的日期分隔符是**斜杠**（`2023/08/11 (Fri) 15:58`），直接当
  文件名会被解析成子目录——36/36 全炸 FileNotFoundError 踩过一次，
  `replace("/", "-")` 修。⚠ 终端回显会把 0x2f 渲染成 `-`，肉眼核不出来，
  验证数据必须 `hex(ord(c))` 验码点。
- 题面注入 `今天是 {question_date}`：temporal-reasoning 型必需。

## 判定标准（两个读数互相归因）

- **accuracy**：LLM-judge（siliconflow，跨供应商避自评偏好）输出二值
  `correct` + 1-5 `score`。与论文 GPT-4o judge **非严格同尺**，只作量级锚定。
  金标为「信息不足」时，候选明确说不知道才算对（与 LongMemEval 口径一致）。
- **hit@3**（离线 `kb.search`，用 `answer_session_ids` 金标）：出生产闸门
  （min_score=0.55）与原始召回两档，差值 = 阈值吃掉的召回。
  检索层与答题层分开读数——第一轮全灭时正是靠这个把根因定位到装配层
  而不是检索层。
- 落盘 `data/evals/lme-*.json`（gitignored），含账本（tokens/cost）。

## 反方

1. n=36 小样本，单题 ±2.8 个百分点，只够量级判断不够精细对比（046 纪律：
   不拿 n=1 下机制结论，同理不拿 36 题宣布「达到论文水平」）。
2. judge 非同尺：论文用 GPT-4o judge，本项目用 siliconflow 系模型，
   分数不可直接并表，只能锚定数量级。
3. 领域偏移：roadmap 原文的 SWE-bench 测编码 agent，LongMemEval 测对话
   记忆——刺 #4 在「编码能力横向可比」维度依然无锚点，本 ADR 只补
   「记忆/检索」维度。
4. 免费额度依赖：一轮 36 题 ≈ ¥0.16（embed 为主），额度断了锚点就断。

## 不做

- 不跑 M 臂（~2-3 倍会话量，时间与额度不划算，S 臂已是论文主口径之一）。
- 不为分数动 `src/`（046 standing decision 延伸：机制改动不得以本基准
  分数为验收判据，除非立项依据是结构性证据）。
- 子集与原始数据不入 git（数百 MB，可一键重下/重建）。

## 遗留与触发信号

- knowledge-update 题型没有 tombstone 机制：旧事实与新事实同时在库里，
  检索层无法区分时效——若该题型显著低于其他题型，触发「笔记时效标记」立项。
- judge 同尺化：若将来接入 GPT-4o 级 judge（付费 key），复跑对齐一次。
- 抽样扩量：36 → 全量 500 题的触发信号＝需要与论文数字正式并表时。

## 实机读数

**第一轮（harness 缺陷轮，作废）**：36/36 答题层全灭（score=1），但检索层
hit@3 100%——两个读数互相归因的设计当场兑现：根因定位到装配层（漏种人设）
而不是检索层。前 7 题快照的失败形态：0 工具轮 + 罐头客服问候（英文题回
「I'm ready to help」）／`list_notes` 盘点跑题／搜成别的话题。修
`ensure_persona` 后冒烟 1/1（score=5），复跑全量。

**正式轮（2026-09-28，`lme-20260928T051304Z`，n=36）**：

| 读数 | 值 |
|---|---|
| accuracy（judge 二值） | **25/36 = 69.4%** |
| 质量分均值（1-5） | 4.06 |
| hit@3（生产闸门 min_score=0.55） | 34/36 = 94% |
| hit@3（原始召回） | 35/36 = 97% |
| max_rounds 保险丝 | 0/36 |
| 成本 | ¥0.2851（embed 6.1M tokens 为主） |

按题型：multi-session 6/6、single-session-user 6/6、single-session-preference
4/6、temporal-reasoning 4/6、single-session-assistant 3/6、
**knowledge-update 2/6**。

**三条解读（守 046 纪律，不下超样本结论）**：

1. **检索层不是瓶颈**：hit@3 94%（闸门只吃掉 1 题召回）而答题 69.4%——
   失败集中在答题层（检索到了但答错/答含糊），与冻结集 r7 那种「召回不到」
   的形状相反。
2. **knowledge-update 触发信号兑现一次**：该题型检索 6/6 全命中、答题仅
   2/6——库里新旧事实并存（实机样本：旧运动鞋「起初床下」vs「现在壁橱
   鞋架」，模型答了旧版或含糊两可），正是「无 tombstone/时效消歧」的预期
   形状。但 n=6 小样本，**按 046 纪律不据单次读数立项**，下次锚定跑复核
   （连续两轮垫底再立项「笔记时效标记」）。
3. **max_rounds 0/36**：单轮问答基准不吃计划预算，与冻结集「`max_tool_rounds=5`
   与多步计划抢预算」（活清单）形成对照——预算张力是任务形态的函数，
   不是常量选错。

judge 非同尺（siliconflow vs 论文 GPT-4o）+ n=36：本读数只作**量级锚定**，
不与论文数字并表。落盘 JSON 含每题工具轨迹与账本，复跑口径见模块 docstring。
