# 060 P0-5 第一刀：失败台账 + make_plan 查重（记忆×执行耦合）

> 状态：**已落地（2026-09-28）**｜日期：2026-09-28
> 授权出处：用户 2026-09-28 在「接下来要做啥」问卷中选「P0-5 记忆×执行耦合」；流程经用户认可＝先读地面真值 → 本草案 → **批准后动工**（044 同款「草案待批」先例）
> 立项出处：competitive-roadmap.md P0-5（内部归档）（第 85-89 行）＝刺 #7「P0 中至少一条定位驱动」的独苗 + 护城河 8.2「流程能力数据化，每执行一次耦合数据厚一分」； AgentFold 查重 + LongHorizon「验证过的经验」
> 一句话结论：**失败记录的真值源早已在盘上**（plan 归档随 session.json 持久化、失败 note 终态必填），缺的不是 Run Store schema 而是一个跨会话派生索引——`data/plan_failures.jsonl` 台账 + make_plan 时**软拦**查重（回灌警告、模型自判改向），这就是「相似失败不再重犯」的最小闭环。

## 背景与动机

roadmap 原文四子件：

> ① Run Store 存带标签的执行/失败干预记录（改动类型/失败原因/指标 delta）；② plan 阶段先查重——与历史失败高度相似的方案直接拦截或改向；③ 记忆召回条件跟随当前 plan 上下文；④ finish_plan 收官回验（承诺漂移，第 9 节⑦）：步骤产出 vs 计划原文一致性检查，不一致转人审。

验收标准：evalkit「相似失败不再重犯」场景通过；反哺数据只经固化管线入记忆（走 P0-7 规则）。

刺 #8 纪律 WIP=1：四子件不能一次全做。地面真值显示①的 roadmap 指定落点（Run Store）在结构上承载不了跨任务查重（见地面真值 1），而真正的原料（plan 归档）早已持久化——**第一刀的裁法与 roadmap 的「首批改动点」有实质偏移，必须先立据再动手**，故本案以草案形态先行。

## 地面真值（写代码前核对，全部读码取证）

1. **Run Store v1 是内存版、重启即空**：[run_store.py](../../src/agent/server/run_store.py) docstring「内存版多 Run 容器（教学版）。升级触发信号：>1 进程/多实例部署时换 Redis/DB」＋ `list_runs` 注释「v1 全在内存、重启即空」⇒ 子件①若以 Run Store 为落点，**重启后查重对象归零**，「跨任务不再重犯」在结构上不成立。①的落点必须换。
2. **plan 归档已随 session.json 持久化**：[store.py:133](../../src/agent/memory/store.py) `PlanBoard.from_dict(raw.get("plan", {}))`；[memory/plan.py:336-348](../../src/agent/memory/plan.py) `to_dict/from_dict` 含 `archive` 全量（事件史含 created/revised/step_updated/finished 四种）⇒ **失败记录的真值源早已跨重启存在**，缺的是跨会话的读取索引，不是新的存储真值源。
3. **失败原因早已是必填项**：`update_step` 校验「终态 must 带 note（结果/跳过理由/失败原因）」（memory/plan.py:195-196）⇒ roadmap ①要的「失败原因」标签**原料已在盘上**，零新增声明成本。
4. **finish_plan 是唯一的归档迁移点**（memory/plan.py:314-323）：校验全终态 → 追加 finished 事件 → `archive.append` + `active=None`；且 `board.finish_plan` 全仓库只有一个调用方（tools/plan.py:119，Grep 穷举）⇒ 台账的写入时机＝工具层 finish_plan 成功后扫 archive 顶，一次拿全「本计划全部 failed 步骤」，无第二入口要堵。
5. **Plan 没有标题字段**（memory/plan.py:61-71：只有 `steps + tools`）⇒ 「方案」的可比文本＝**步骤标题列表**（查重比对的原料形态）。
6. **roadmap「首批改动点」与地面真值两处偏移**：(a)「Run Store schema」——见地面真值 1，不动；(b)「`orchestrator/assemble.py` plan 注入段」——plan 轮首投影实际在 **loop.py**（[loop.py:235](../../src/agent/orchestrator/loop.py) `_plan_stamp` ＋ :585 `plan_msg = _plan_stamp(session.plan)`），assemble.py 里只有工具注册（assemble.py:336 `register_plan_tools(sub, sctx)`）。本刀两件都不动，偏移在此登记（roadmap 原文留档不改写）。
7. **learned 召回是构建时全量快照**：[agent.py:124-154](../../src/agent/orchestrator/agent.py) `_learned_block` 三桶全量拼 prompt 尾；assemble.py:340-341 注释「AGENTS.md 式：构建时读盘一次拼 prompt 尾部，会话中途固化不热刷新」⇒ 子件③「召回跟随 plan 上下文」要改的是这条**全量注入链**，与失败记录无数据依赖，是独立一刀。
8. **finish_plan 现状零回验**（tools/plan.py:117-122 只调 `board.finish_plan` 转错误串；memory/plan.py:202-216 只校验「全步骤终态化」）⇒ 9.2⑦ 的承诺漂移缝**真实存在**；但回验要接 LLM 一致性判定（internal_llm）＋「不一致转人审」要接确认缝，复杂度自成一体，独立一刀。
9. **子 agent 的失败自动进主会话棋盘**：`spawn_step` 回写的是主会话同一个 board（[spawn.py:316/330](../../src/agent/tools/spawn.py) `board.update_step`）⇒ 委派出去的失败步骤收官时同样进 archive，台账**无需为 spawn 加任何分支**就覆盖委派失败。
10. **固化管线是反哺的唯一合法通道**（验收标准②）：consolidate.py 的 P0-7 分流（project 桶 constraints 无 verified 背书 → 降级候选不落盘）⇒ 台账**永不写 learned/**，落点是 `data/` 运行时文件（checkpoint jsonl 同族，.gitignore 已覆盖 `data/` 运行时资产的惯例）；且 `_learned_block` 只读 `learned/*.md`（agent.py:137），台账**结构性进不了**全量注入链。
11. **evalkit 场景制可加新场景**：`SCENARIOS = scenarios/frozen_real.jsonl`（[frozen_eval.py:101](../../evals/frozen_eval.py)），多轮 task 列表 / 场景级 confirm policy / `new_session` 均已支持（frozen_eval.py:46-55）；历史场景与注入场景同文件居住是既有惯例（frozen_eval.py:6）⇒ 新场景以**追加行**入档，既有行逐字不动（git diff 自证），harness 零改动。
12. **路径常量的居住规则**（paths.py:8-17 头注记）：「跨模块共享的路径常量住这里；只被一个模块用的路径……加了就是死旋钮」⇒ 台账路径只被 tools/plan.py 读写，常量住 **plan.py 模块内**（锚 `paths.DATA_ROOT` 拼文件名），paths.py 不动；测试注入＝monkeypatch 模块常量（`user_memory_path()` env 覆写同款先例，assemble.py:368-371 注释）。
13. **finish_plan 可能跨线程并发**：S8a 多会话并发（assemble.py:83-87）＋ 059 并行 spawn ⇒ 台账 append 需进程内锁（059 `_emit_lock` 同族修法：锁共享收口点一次，不在调用侧各自加）。

## 选项（第一刀裁哪件）

- **甲（①+② 最小闭环）**：失败台账 + make_plan 查重。
- **乙（④先行）**：finish_plan 收官回验——独立、不依赖持久化裁定，但对的是 9.2⑦ 承诺漂移缝，不直接对验收标准第一条。
- **丙（③先行）**：召回跟随 plan——改 learned 全量注入链，与失败记录无关。
- **丁（四子件全做）**：违反刺 #8（WIP=1）。

## 拍板

1. **第一刀＝甲（①+②），乙丙丁否决。** 「相似失败不再重犯」的最小闭环就是「有处可查（①）＋查（②）」：无①则②无查重对象，无②则①是虚荣记录（写了没人读的资产不是资产）。③④与失败记录零数据依赖（地面真值 7/8），各自独立成刀，登记遗留。丁违反 WIP=1。
2. **①的落点＝plan 归档派生台账 `data/plan_failures.jsonl`，不用 Run Store**（地面真值 1/2）。哲学同 graph.json「索引是缓存」：**真值源＝session.json 的 plan archive，台账是可删可重建的派生索引**（重建＝遍历 sessions 扫 archive；重建脚本本刀不写，见不做 6）。台账行**自含查重所需全部字段、查重时不回查 session**——进程死于「收官后、settle 落盘前」会留孤儿行（指向没落盘的 sid），无害。
3. **台账行字段 v1**＝`{ts, sid, steps, failed, summary}`：`steps`＝本计划全部步骤标题列表（方案的可比文本，地面真值 5）；`failed`＝`[{id, title, note}]`（note＝失败原因，地面真值 3）；`summary`＝finish_plan 收官声明。**「改动类型/指标 delta」两标签砍掉**——前者无声明入口、后者的数据源只能是评测读数（生产运行时没有指标），记不做 4。
4. **②软拦不硬拦。** make_plan（创建与修订同）执行前读台账，命中即在回灌里追加「历史相似失败」段（当时失败步骤标题＋note 摘要，上限 3 条防爆），并标注「原因描述来自当时 agent 自述」；**计划照常创建成功，改向权归模型**（M5「错误文案即提示词」同款哲学：信息给模型，决策归模型）。不程序拦截的理由：v1 相似度算法粗糙（拍板 5），误报硬拦＝程序挡住合法方案，代价高于漏报。roadmap 原文「直接拦截或改向」→ v1 软拦，偏移登记；升级触发信号见遗留 1。
5. **相似度 v1＝`difflib.SequenceMatcher`**（标准库零依赖）：新计划步骤标题拼接串 vs 台账每行 `steps` 拼接串（含 failed titles），ratio ≥ 0.6 记命中。**0.6 是拍脑袋起步值**，实机校准，诚实登记；中文标题字面重叠 ≠ 语义相似的局限认账（反方 2）。
6. **读写同居 tools/plan.py，memory/plan.py 零改动。** 台账是工具层的派生物，不是 plan 域的状态机语义（薄包装原则不倒灌：board 不该知道台账存在）；写入＝`_finish_plan` 在 `board.finish_plan` 成功后扫 `archive[-1]`（有 failed 步骤才追加，全 done/skipped 不写），读取＝`_make_plan` 前置。路径常量按地面真值 12 住本模块。
7. **台账 append 持模块级 `threading.Lock`**（地面真值 13）。与 039 边界⑤同款裁定：锁存在即可，不造并发测试。
8. **evalkit 新场景「相似失败不再重犯」**：scenarios JSONL 追加行（既有行逐字不动，地面真值 11）——setup 预置一条台账记录（直接写 data/plan_failures.jsonl，等价于「历史上失败过」），任务给出与历史失败高度相似的多步骤请求，断言＝make_plan 回灌含「历史相似失败」段。046 纪律：答案先于实现冻结；**n=1 只作存在性证据，单次绿不记战功、不下机制结论**。
9. **反哺纪律的验收是结构性的**：台账住 `data/plan_failures.jsonl`（运行时资产），`_learned_block` 只读 `learned/*.md`（地面真值 10）——台账进不了全量注入链；它出现在模型上下文的唯一通道是 make_plan 查重命中时的回灌段（带「当时 agent 自述」标注，不满足 P0-7 verified 的客观背书标准，故绝不以「已验证经验」面貌出现）。consolidate 管线一行不动。

## 判定标准（写代码前定）

1. 制造一次 failed 步骤并 finish_plan → `data/plan_failures.jsonl` 追加一行，含 `sid`/`steps`/`failed[].title`/`failed[].note`/`summary`；无 failed 步骤的收官**不追加**。
2. 随后 make_plan 提交相似步骤表（ratio ≥ 0.6）→ 回灌含「历史相似失败」段＋当时 note 摘要＋「agent 自述」标注；不相似的步骤表零命中、回灌逐字不变。
3. **软拦语义**：命中后 `make_plan` 照常返回「已创建」——程序拦截不存在。
4. **持久化**：重启进程后查重仍命中（台账在盘上不在内存）。
5. **并发**：模块级锁在 append 路径上（代码审查可见；不造并发测试，039 边界⑤同款）。
6. evalkit 新场景通过；既有场景行逐字不动（git diff 自证）；bash 臂不受影响。
7. 三门全绿（ruff / mypy / pytest），净增测试覆盖判定标准 1-4。

## 实现

`src/agent/tools/plan.py`（六处）：模块 docstring 补 060 段；imports（`json`/`threading`/`UTC, datetime`/`SequenceMatcher`/`DATA_ROOT`）；常量四件（`_FAILURES_PATH` 住本模块锚 `DATA_ROOT`／`_FAILURES_LOCK` 模块级 append 锁／`_SIMILAR_THRESHOLD`／`_MAX_HITS = 3`）；`_record_failures(board, summary)`（`board.finish_plan` 成功后扫 `archive[-1]` 事件史，有 failed 才 append 一行）；`_check_history(steps)`（读台账逐行比对、命中组「历史相似失败」段）；两处挂线＝`_finish_plan` 成功后落账 + `_make_plan` 回灌尾部拼接。`memory/plan.py` **零改动**兑现（board 不知道台账存在）。

`tests/test_plan.py`（六件）：failed 步骤收官追加一行含字段断言（判定 1）／全 done 不追加（判定 1 反向）／修订换表前的 failed 也入账／相似命中回灌含「历史相似失败」+ note 摘要 +「agent 自述」标注且计划照创（判定 2/3）／不相似零命中回灌逐字不变（判定 2 反向）／校准钉住（偏移 3）。测试用 monkeypatch 模块属性换 tmp 路径；判定 4（重启持久化）由结构兜底＝读路径每次过盘、进程无内存缓存，预置台账行的测试本身就是「非本机制写入者留下的盘上行」被命中。

`evals/scenarios/frozen_real.jsonl`：追加第 16 行 `m1-plan-failure-dedup`，既有 15 行逐字不动（git diff 自证）。setup 预置一条台账记录（等价于「历史上失败过」），任务＝两步相似调研；verify＝审计日志 grep「历史相似失败」（机制硬断言），judge 只评 agent 反应质量与任务完成度。

**实现期偏移登记（拍板原文不动，偏移记这里）**：

1. **`sid` 砍掉**（拍板 3 字段表含 sid）：Session 不持 sid（store.py「身份＝文件名」），title 也靠不住（settle 才提炼）⇒ 台账行实为 `{ts, steps, failed, summary}`。
2. **`failed[].id` 砍掉**（拍板 3 原 `failed=[{id, title, note}]`）：id 是「当时那张表」的编号，修订换表即重排、跨表无指称意义 ⇒ failed 条目＝`{title, note}`；title 靠**逐事件 fold 当时 id→title 表**找回（created/revised 换表、step_updated failed 时查表）⇒ 修订换表前的 failed 也入账（`view()` fold 只看得见最终表，事件史记得）。
3. **阈值 0.6 → 0.5 实机校准**（拍板 5 预登记「拍脑袋起步、实机校准」，非 046 禁止的为分数调机制）：m1 首跑模型给步骤标题补括注（「搜索 RAG 的最新实践（检索个人知识库 + 联网补充）」），对台账拼接串的对称 ratio 被稀释到 **0.582 < 0.6** ⇒ 漏报、verify 红。软拦误报成本≈零（一段可忽略的警告）、漏报＝机制永不触发，代价不对称 ⇒ 宁低勿高；另加回归测试钉住漏报现场原件（与 m1 同措辞台账 + embellished 标题 ⇒ 仍命中）。
4. **m1 场景台账 steps 与题面措辞对齐**＝诚实对齐 v1 字面匹配的能力边界（反方 2 已认账「换措辞的同方案漏报」）；题面与机制均未动，校准的只有阈值。
5. **`.gitignore` 补一行（地面真值 10 那句与真值不符，实现期纠正）**：草案地面真值 10 写「checkpoint jsonl 同族，`.gitignore` 已覆盖 `data/` 运行时资产的惯例」——实际 `.gitignore` 是**逐项点名制**（`data/memory/`、`data/vector_db/`、`data/todos.json`、`data/audit/`、`data/checkpoints/`、`data/worktrees/`、`data/evals/`…），没有「`data/` 整目录忽略」；`git check-ignore -v data/plan_failures.jsonl` **exit=1（未被忽略）**。后果具体：生产实机跑一次带 failed 步骤的收官，工作区就冒出一个 untracked 文件，将来 `git add -A` 会把它误提交成「代码资产」。修法＝`.gitignore` 加一行（带注释说明「派生索引，真值源＝session.json，可删可重建」）。教训登记：地面真值核对漏了「惯例」与「实然」的差别，靠 `git check-ignore` 而非读印象才抓出来。

三门：**ruff All checks passed｜mypy Success（59 files）｜pytest 688 passed, 2 skipped**（059 基线 682，净 **+6**；既有测试一条未改＝纯加法）。

**m1 两轮实机读数**（046 纪律：n=1 只作存在性证据，不记战功、不下机制结论）：首跑（阈值 0.6）FAIL＝verify exit 1，质量 4/5，¥0.0517（漏报现场，即偏移 3）；重跑（阈值 0.5）**PASS**＝质量 4/5，介入 2 次，17.8s，¥0.0495——agent 回答里显性回应「2026-09-20 有过一次相似计划因联网搜索全部超时失败；这次搜索本身是成功的，不算重蹈覆辙」＝软拦信息被接收并用于自判的存在性证据（反方 1「形同虚设」的第一个反例，n=1）；judge 扣分项（第二步笔记未落盘）属工具预算耗尽，与本案机制无关；另观测到该轮第二步 failed（预算耗尽）自身也落了台账＝机制自循环的第一个实机样本。

**冻结集整轮重跑（口径已变 ⇒ 必须整轮对照）**：本刀既加题库第 16 行、又改了 `make_plan` 回灌路径（可能影响既有场景），按 046/059「改口径不做单点补测」的纪律跑 full 臂 16 题。

- 原始读数 `frozen-20260928T074825Z`：**13/16（81%）**、质量均分 4.25、¥0.3295、2058.5s、人工介入 7 次、`contaminated` 全空。对照 056 那轮基线 `frozen-20260928T033601Z`（15 题 full 臂 12/15、质量 4.73、¥0.4018）：题数 +1、口径不同，**不构成可比退化/进步结论**。
- **三条红里两条读数无效**：`i1h` 与 `m1` 均 `ReadTimeout`（i1h 957.5s、calls=1、¥0.0019；m1 961.7s、calls=0、¥0.0000，harness 自动标注「疑似降级到 mock（llm_calls=0 cost=0.0）——分数不可信」，且 verify exit 2＝`grep: data/audit: No such file or directory`）⇒ 供应商网络瞬时故障，与机制无关。`--only i1h,m1` 补跑 `frozen-20260928T120920Z`：**i1h PASS 5/5 6.6s ¥0.0102｜m1 PASS 3/5 介入 2 次 15.1s ¥0.0565**（m1 的 `confirm_tools=[make_plan×2]`、`guards=[plan-scope]`；judge 扣分＝第二步整理笔记未完成，与首轮同因＝工具预算，查重机制本身转绿）⇒ **有效读数 15/16（94%）**。
- 046 纪律登记：小样本、单轮、含无效读数、m1 的 n 仍为 2（两跑同题）⇒ **不据此下任何机制结论**，本轮只作 060 落地后的第一个整轮存档点。

**r2 红的归因（唯一机制相关红，结构上与 060 无关）**：`r2-plan-research` 质量 2/5、llm_calls=14、confirms=3、`guards=[plan-scope]`、17 次工具调用、**4 步全 failed、全程未调 `finish_plan`**。链条＝`make_plan`（据 agent 自述：`tools` 只声明了 `spawn_subagent`）→ `spawn_step`×3（子上下文继承计划白名单，而子任务实际要 `search_notes`/`read_notes`/`list_notes` ⇒ 被 057 范围闸逐次拦）→ `make_plan` 修订扩白名单 + 主上下文直调 `list_notes`/`read_notes`/`search_notes`×2 + `spawn_subagent`×3 → 第三次 `make_plan` → `update_plan_step`×4 全 failed。agent 在 step 1 的 failed note 里自述根因：「首次因计划白名单只含 spawn_subagent，子任务里的 search_notes 被拒；修订白名单后工具预算已耗尽，未能重跑」（审计日志截断 `tools` 参数，白名单内容取 agent 自述 + `guards=[plan-scope]` 佐证，非程序事实）。

与 060 不可能相关的两条结构性理由：① 副本 setup 不预置台账（`data/plan_failures.jsonl` 在副本里不存在）⇒ `_check_history` 零命中、`make_plan` 回灌逐字与改前相同；② 未收官（无 `finish_plan`）⇒ `_record_failures` 零写入。病灶＝活清单「`spawn_step` 焊点松动」×「057 计划白名单继承」的**第五次观测，首次导致 4 步全 failed**，并与活清单「`max_tool_rounds` 与多步计划抢预算」同源（14 次调用里 3 次花在改计划上）。两处已在活清单补注；**不在本 ADR 内修**（跨刀，WIP=1）。

附带观察（挂触发信号，不预先加机制）：本轮起台账会开始积累「工具预算耗尽」类 failed——这类 note 与方案本身对不对无关，将来可能成为查重的噪声源（拿预算事故去拦一个本来可行的计划）。样本 n=1，不做过滤；触发信号＝台账里此类行占比目视过半、或出现一次「被预算类历史失败误导改向」的实机案例。

## 反方（预写）

1. **软拦可能形同虚设**：模型无视警告照走老路。——这正是新场景要观察的行为；若实机读数显示无视率高，走遗留触发信号 1 升级（硬拦或警告挪进轮首投影）。先给信息、后上程序，与 make_plan 自身「模型自判点不点」的哲学一致。
2. **difflib 对中文标题粗糙**：字面重叠 ≠ 语义相似，换措辞的同方案漏报。——v1 认账；语义检索属「召回升级」，触发信号＝实机漏报案例（与 032 裁定二 v2 的「量到再上检索」同纪律：先用最省形态拿到真实读数）。
3. **双写漂移**：台账与 session.json 可能不一致（进程死亡留孤儿行）。——台账定位是派生索引不是真值源，孤儿行自含全部查重字段、不碍查重；真出漂移实例再写重建脚本（YAGNI）。
4. **只记 failed 步骤，不记「人审干预」**（拒绝/改向）——roadmap ①原文有「干预记录」，但人审事件住在审计日志（AuditLog），是另一数据源的另一把刀；本刀不碰（不做 7）。
5. **台账无限增长**——教学项目量级（几十会话、每次收官追加数行）无视；触发信号＝单文件 MB 级。

## 不做（防范围蔓延）

1. 子件③（记忆召回跟随 plan 上下文）——learned 注入链的独立改动，开刀时本案台账是现成「plan 上下文」数据源。
2. 子件④（finish_plan 收官回验）——LLM 一致性判定＋人审缝接线，独立一刀。
3. Run Store 持久化/外置（Redis/DB）——触发信号不变：多实例部署。
4. 「改动类型/指标 delta」标签——无数据源（拍板 3）。
5. 硬拦/程序强制改向——触发信号见遗留 1。
6. 台账重建脚本、台账 UI/CLI 查看命令——资产可见性 v1 靠直接读 jsonl。
7. 人审干预记录（审计日志派生）——另一数据源，另一把刀。

## 遗留与触发信号

1. **软拦无视率高**（实机读数）→ 议硬拦，或把「历史相似失败」从回灌挪进轮首投影（loop.py `_plan_stamp`）——届时才动 roadmap 说的「plan 注入段」。
2. **漏报案例出现**（换措辞的同方案没拦住）→ 相似度上语义检索；与 P2-1 记忆三件套、032 裁定二 v2 信号同源并议。
3. **③④开刀时**：本案台账是现成数据源（③的「plan 上下文」比对原料、④的「计划原文 vs 步骤产出」比对原料），不许另起第二份失败记录。
4. **台账单文件 MB 级** → 滚动/截断策略。
