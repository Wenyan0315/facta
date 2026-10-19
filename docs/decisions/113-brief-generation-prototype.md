# ADR 113：D02 最小原型——项目 Brief 生成与历史决定复用

- 状态：**已开工前评审，按意见修订，待实施**（2026-10-19；实施载体随 [114](114-facta-2-roadmap.md)/M2 调整为首个 data skill）
- 日期：2026-10-19（草案）/ 2026-10-19（评审修订）
- 上游：[098](098-product-prototype-priorities.md)（D02 口径与验证条件）、
  [110](110-product-validation-and-engineering-showcase.md)（V02 自用任务）、
  [111](111-v01-review-prototype.md)（mode/repo_root 接线、锚点、归属、跳固化先例）、
  [112](112-review-tool-rounds-budget.md)（证据预算先例）、
  [114](114-facta-2-roadmap.md)（2.0 路线图：skill 数据化落点）。
- 调整关系：本案是 098/D02 的最小原型。2026-10-19 经 `/review` 完成开工前评审
  （dogfood 闭环：D01 评审 D02），四条必改与三条可选全部采纳——处置见
  「开工前评审记录」。设计本体（人设契约/工具集/预算/归属/跳固化/边界）不变；
  **实现载体从硬编码 brief.py 调整为 114/M2 的首个 data skill**。

## 背景

098 登记的用户摩擦：**「需要花很长时间 brief，以前做过的任务仍容易出错」**
——每次在新工具（Trae/Kimi Code）里开工，都要手工重述项目背景、约定与历史
决定。D02 的产品承诺：给一句任务意图，自动产出结构化开工 Brief，复用已沉淀
的项目决定，减少重复解释。098 的验证条件：**与手工 Brief、项目说明文件比较，
验证正确复用而非仅增加上下文长度**。

111 已为 D01 建立全套骨架（mode 参数、repo_root 校验、S6a 锚点注入、learned
归属、跳固化、只读证据集）；112 补齐证据预算。D02 与 D01 方向相反——评审是
「用户给方案、机器挑错」，Brief 是「用户给意图、机器产材料」——骨架复用。

## 裁定

### 1. 入口与构建链（评审补全：五落点缺一即 404 或 CI 红）

- FW 第七页 `/brief`：表单 = 任务意图（一句到一段）+ 被开工仓库根，提交走
  `POST /api/runs/sync` 加 `mode="brief"`；`CreateRunRequest.mode` 扩
  `Literal["chat","review","brief"]`，目标仓校验复用 `_review_repo`。
- 构建链五落点：`frontend/brief.html`、`frontend/src/brief/`（main/App/api，
  `submitBrief` 照 review/api.js 复制改 `mode:"brief"`——第二次出现不抽共享
  模块）、`vite.config.mjs` 入口、`app.py` 的 `/brief` 路由（FileResponse 构建产物）、
  `static/fw/` 构建产物进 git（CI 前端闸 `git diff --exit-code` 照跑）。
- Brief 在独立新会话完成（材料与产出落会话文件，来源可追溯）；缺省
  `mode="chat"` 使既有调用方逐字节不变。

### 2. 人设：Brief 人设六节契约 + 评审三条补强

输出六节：①任务目标复述；②**相关历史决定**（带日期与 ADR/出处编号；无则明说
「未找到相关决定」，不编造）；③仓库现状（读到什么写什么，不凭命名猜）；
④约束与纪律（以仓库 README/architecture 读到的为准）；⑤验收建议；⑥待用户补充。

评审补强（三条全采纳）：

- **过时判定给依据**：不只丢日期——引用时标注日期与出处，若仓库内可见后续
  ADR/注释已取代该决定，标注「可能已被 X 取代」（判定依据须为读到的原文，
  推断则标明）。
- **外部仓来源明示**：learned 只在本仓注入（111 归属不变）；对外部仓库，人设
  明示「优先读目标仓的 docs/decisions/、architecture、README 提取历史决定」，
  避免「相关历史决定」节在外部仓空转。
- **预算尽收尾纪律**：12 轮用尽后未核实的内容进「待用户补充」并明说未核实，
  不编造出处（服务 098「引用决定真实」的验收）。

出处格式的实现依据（评审待验证项，已核实）：learned 注入走 `render()`，
**行首日期保留**；`[固化:sid]` 标签按 053 只注入 VISIBLE_TAGS——所以「带日期」
可兑现，「出处」应引用条目正文中的 ADR 编号而非 sid。

事实与推测分开标注；Brief 是**草稿**——采纳与否由用户定，不自动升级为项目
决定（098 红线同款）。

### 3. 工具与预算：复用只读证据集

`BRIEF_TOOLS = REVIEW_TOOLS`（同一只读证据集：读文件/搜索/列目录/notes
检索/时间），不另立集合——两者都是「对目标仓取证」，无分化需求。预算复用
`REVIEW_MAX_TOOL_ROUNDS=12`（112 语义同样适用：Brief 要读完相关文件才能写准
现状节）。

### 4. 装配与实现载体（评审后调整）

**实现载体 = 114/M2 的 skill 数据化机制，Brief 为首个 data skill**（不再先
硬编码 brief.py 再迁移——避免同一人设双改两处）。skill 机制的实现要求由评审
确认的落点清单构成：

- **装配三落点**：`_agent_for_mode` 增 brief 分支（skill 工厂缺失时抛
  RuntimeError，照 review「不静默降级」先例）；`AppContext` 尾部增
  `build_brief_agent: Callable[[Session, Path], Agent] | None = None`；
  `assemble()` 增工厂绑定（mother_ctx/audit 递进模块级工厂）。
- **worker 两处条件**：标题补写处 `if mode == "review" and ...` 改
  `mode in ("review", "brief")`；收尾处 `if ... mode != "review"` 改
  `mode not in ("review", "brief")`——漏改收尾处会把 Brief 草稿固化进 learned，
  直接违反 098 红线（评审意见 3，最险的一条）。

learned 归属同 111（只在 `repo_root == WORKSPACE_ROOT` 注入）；Brief 不固化、
标题就地补。若 skill 机制（114/M2）动工前需要提前实现 Brief，按上述落点走
硬编码方案（本节清单即实施方案）。

### 5. 边界（不做的）

- **不自动投递**：Brief 产出给用户复制粘贴到外部工具，不做「一键发给
  Trae/Kimi」集成。
- **不做模板库/多仓聚合/Brief 历史**：首版只做「一个意图 → 一份 Brief」。
- **不动 chat 主链**：普通对话不换 Brief 人设；`mode="brief"` 显式选择。
- **不自动固化**：Brief 内容不进 learned（同 111 跳 settle）。
- **不建通用 Agent 平台**（098 首版边界沿用）。

## 开工前评审记录（2026-10-19，dogfood：D01 评审 D02）

经 `/api/runs/sync`（mode=review，repo_root=本仓）完成——**111 验收「评审
facta 本仓注入项目决定」的真机首跑**。评审输出结构完整（总体判断/必改/可选/
已核实与未核实分列），四条必改均为「按草案直接实施会翻车」级：

| # | 意见 | 处置 |
|---|---|---|
| 1 | `mode="brief"` 装配缺三处落点 → 静默跑成 chat | 采纳（§4 装配三落点） |
| 2 | `/brief` 页构建链五落点不全 → 404 / CI 前端闸红 | 采纳（§1 五落点） |
| 3 | worker 两处条件只认 review → Brief 草稿被固化（违反 098 红线） | 采纳（§4 两处条件 + 测试要求） |
| 4 | V02 真机例时序冲突：113 自己不能用 Brief 开工 | 采纳（验收第 2 条改「Brief 落地后的下一个真实任务」） |
| 可选 1 | 过时判定给依据，不只丢日期 | 采纳（§2 补强一） |
| 可选 2 | 外部仓明示读 docs/decisions 等 | 采纳（§2 补强二） |
| 可选 3 | 预算尽收尾纪律（未核实不编造） | 采纳（§2 补强三） |
| 待验证 | learned 注入是否保留日期出处 | 已核实：日期保留（render），sid 标签不注入（053）→ 出处用 ADR 编号 |

**V02 摩擦记录（110/V02 第一例）**：材料准备 ≈0（ADR 草案即材料，附文件路径
供评审直读）；意见核实 ≈10 分钟（四条必改全部成立，无需驳回）；评审引用了
注入的项目决定与仓库原文（`app.py`/`assemble.py`/`vite.config.mjs`/CI 工作流
逐条对上）——**learned 注入 + 仓库核实双通道真实生效**。结论：愿意在下一个
真实任务继续使用（098 记录项「是否形成复用动机」的首例答案为是）。

## 验收（修订）

- [x] 本 ADR 过开工前评审，意见逐条记录处置（见上）。
- [ ] 结构测试：随 114/M2 skill 机制一并落地（mode=brief 分派、工具集与预算、
      跳固化与标题补写、/brief 页 200、CI 前端闸）。
- [ ] 真机双轨例（110/V02）：**Brief 落地后的下一个真实任务**（如 D03 或 2.0
      M1 的某改动）——手工 Brief 与生成 Brief 同一时点独立写，对照维度：
      引用的决定是否真实存在于 learned/ADR、是否与任务相关、是否已被后续
      ADR 取代、两稿长度与准备耗时。

## 与已有记录的关系

- **098**：D02 第二优先的落实；验证条件（与手工 Brief 对照）写进验收。
- **110**：本评审构成 V02 自用第一例；外部试用不在本案范围。
- **111/112**：骨架与预算先例全复用；本 ADR 落地后 mode 家族为
  chat/review/brief 三态。
- **114**：本案实现载体归入其 M2（skill 数据化），Brief 为首个 data skill。
