**整体架构方向是对的，下一步最值得做的是“统一执行上下文和安全边界”，因为现在模块虽然分开了，但会话、工具权限、子任务和取消机制还没有完全隔离。**

作为“从零搭建 Agent”的教学项目，它已经有较完整的工程骨架；如果要进一步支持长期任务、多会话或实际业务使用，建议先修下面这些边界问题，再扩功能。

本次评审针对附件重新解压的 **0.9.0 版本**，没有沿用此前版本的结论，未修改项目源码。

## 一、架构判断：保留模块化单体，不急着拆

当前实际执行链大致是：

```text
CLI / Web
    ↓
assemble：装配模型、会话、工具、Agent
    ↓
run_turn：路由 → 历史压缩 → 模型调用 → 工具循环
    ├── LLM Gateway：缓存、重试、熔断、降级
    ├── Session：对话、摘要、计划
    └── ToolRegistry：参数校验、确认、执行、审计
          ├── 文件 / 终端 / 检索 / MCP
          └── 子 Agent：临时会话 + 共享工具注册表

Web：RunStore → worker → SSE → 前端
```

值得保留的设计：
- **CLI/Web 共用执行内核**，没有各维护一套逻辑。
- **完整历史与模型上下文分离**，压缩不等于删除原始对话。
- **工具注册、计划状态转换有代码约束**，不是完全依赖提示词。
- **具备离线测试替身**，核心机制可以不调用真实模型就验证。

需要明确：当前子 Agent 是同步委派，不是独立 worker；计划是状态板，不是自动调度引擎。这对教学项目是合理取舍，暂时不必引入微服务或复杂 DAG 框架。

## 二、最重要的七项改进

### 1. 工具安全策略需要集中管理，不能每个工具各防各的

**当前问题：一个入口禁止的操作，其他入口仍然能做。**

例如：
- `read_notes` 直接拼接路径，没有目录边界检查。
- `search_code` 没有复用文件读取的路径限制，也没有排除 `.env`。
- 终端对白名单命令的判断不能限制实际访问路径，`cwd` 也不是沙箱。

证据：[notes.py#L56-L62](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/tools/notes.py#L56-L62)、[files.py#L92-L107](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/tools/files.py#L92-L107)、[terminal.py#L55-L73](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/tools/terminal.py#L55-L73)。

**建议：**抽出统一的资源访问策略，所有工具在真正读写前检查规范化路径、符号链接、敏感文件和允许操作。终端执行单独管理；运行 `pytest` 同样是在执行项目代码，不能简单视为安全只读操作。

这是投入实际使用前的第一优先级。

### 2. 前端必须把模型输出视为不可信内容

当前 `marked.parse(text)` 的结果直接进入 `innerHTML`，历史回放也使用同一路径。模型可能复述用户输入或外部网页中的恶意 HTML，因此 assistant 身份不代表内容安全。

证据：[app.js#L24-L34](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/static/app.js#L24-L34)。

**建议：**短期改为纯文本渲染；保留 Markdown 时引入可靠的 HTML 清洗，并让实时输出与历史回放共用安全入口。不能只在提示词里要求“不输出 HTML”。

### 3. Session 应当整体管理生命周期，不能由入口逐个字段拷贝

发现两类状态不一致：
- 新会话清空消息，却没有清空旧计划；恢复归档，也没有恢复对应计划。
- 新 Agent 已构建最新系统提示，但恢复的 Session 保留旧 system，实际调用仍可能使用旧策略与旧记忆。

证据：[app.py#L166-L191](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/app.py#L166-L191)、[agent.py#L158-L170](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/orchestrator/agent.py#L158-L170)。

**建议：**
- 集中实现新建、重置、恢复会话，统一处理消息、摘要、游标、标题及计划。
- 把“历史对话”和“当前生效的系统策略”分开，明确策略何时更新。
- 避免工具注册时永久捕获某个会话的可变对象。

这不是简单漏清一个字段，而是**会话生命周期分散在多个入口**造成的结构性问题。

### 4. 语义缓存不应默认用于有上下文的 Agent 对话

当前语义缓存只比较最后一条用户消息，忽略历史、system、时间和计划。同一句“继续”，放在不同任务里，含义完全不同。

离线验证中，不同 system 上下文拿到了同一个缓存回复，底层模型只调用了一次。

证据：[gateway.py#L360-L385](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/core/gateway.py#L360-L385)。

**建议：**先停用对话主链的语义缓存，保留完整请求维度的精确缓存。语义缓存仅在明确无状态的 FAQ 等场景显式开启。

这里需要先保证“回答的是当前任务”，再讨论减少模型调用。

### 5. 子 Agent 应共享工具定义，而不是共享父任务的运行状态

当前子 Agent 虽然新建 Session，却复用了绑定父会话历史的工具注册表；默认允许的历史工具能把父会话内容带进子任务。同时，子循环没有继承父任务取消信号。

证据：[spawn.py#L76-L107](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/tools/spawn.py#L76-L107)、[history.py#L105-L123](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/tools/history.py#L105-L123)。

**建议：**引入轻量的执行上下文：

```text
ExecutionContext
├── 当前 Session
├── run_id / parent_run_id
├── 工具与资源权限
├── 取消信号 / 截止时间
└── 审计关联信息
```

工具定义可以共享，但执行时必须使用当前上下文。子任务只继承明确授权的信息和权限，并继承取消信号。

短期可先禁止子任务使用父历史工具。**解决这层隔离后，再开放多会话并发。**

### 6. SSE 应使用广播模型，现在是竞争消费模型

多个订阅者拿到的是同一个 Queue。聊天页和任务详情页同时监听时，一个页面取走的事件，另一个页面就收不到。

这会影响确认请求、计划进度和结束状态的展示。

证据：[run_store.py#L76-L96](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/run_store.py#L76-L96)。

**建议：**利用已有事件列表，为每个连接维护独立读取游标，并统一处理历史补发和实时更新。单进程内即可实现，不需要为此引入 Redis。

### 7. 区分“回答结束”和“任务完成”，再考虑断点恢复

当前 `COMPLETED` 表示 Agent 产生了最终回复，不代表代码测试通过、文件确实生成，或其他交付物已验收；Run 状态也只保存在内存里。

证据：[loop.py#L204-L301](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/orchestrator/loop.py#L204-L301)、[run_store.py](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/run_store.py)。

**建议：**
- 保留执行结束状态，额外记录交付物及验证结果，避免把模型自述当验收。
- 确有跨重启任务需求时，再持久化 Run 和工具执行记录。
- 自动恢复前，先定义写操作的幂等与“结果未知”处理，否则恢复可能变成重复执行。

对教学项目，这可以作为后续章节，不必现在就建设完整工作流平台。

## 三、推荐落地顺序

| 阶段 | 建议内容 |
|---|---|
| 第一批：修确定性问题 | 工具访问边界、HTML 清洗、计划重置/恢复、关闭对话语义缓存、限制子任务历史访问 |
| 第二批：收敛架构 | 统一 Session 生命周期、引入执行上下文、贯通取消机制、修复 SSE 广播 |
| 第三批：按需求升级 | 交付验收、持久任务、幂等恢复、多会话并发 |

**不建议当前优先做：**微服务化、通用多 Agent 框架、复杂 DAG 调度、完整事件溯源平台。当前问题主要来自共享状态与契约不一致，增加组件不会自动解决。

## 四、验证范围

本次运行了缓存、子 Agent、文件、执行循环、RunStore、计划六个相关测试文件，**73 项通过**；另用假数据探针确认了访问绕过、缓存串上下文、父历史进入子任务、SSE 队列竞争及旧 system 保留问题。

未运行完整 Web 集成、全量测试或真实模型评估；部分依赖缺失，未联网安装。

**一句话建议：保留现在的单体结构，把下一阶段主题定为“执行隔离、状态一致性和结果验证”，比再增加几个 Agent 能力更有价值。**


-----

**还有。三个视角共同指向一个问题：功能已经比较丰富，但功能之间的衔接还不够完整，用户容易在跳页、恢复任务、编辑内容这些日常操作中遇到断点。**

这一轮重点看了开发流程、产品流程和前端交互，不重复上一轮的架构问题。以下基于代码核查，尚未做浏览器视觉和可用性实测。

## 一、研发工程师：让项目“容易启动、容易调试、容易安全修改”

### 1. 做一条真正独立的离线上手路径

当前假模型模式仍会装配远程 MCP，而附件默认启用了远程 Context7。新人即使不调用真实模型，启动过程仍可能被网络和外部服务影响。

建议把启动方式明确分成三档：
- **离线体验**：假模型＋本地工具，不需要密钥和网络。
- **真实模型体验**：只配置模型即可。
- **扩展工具体验**：按需开启 MCP，不与基础启动绑定。

验收标准：禁网、无密钥的新环境，也能完成一次“提出任务→调用本地工具→返回结果”。

证据：[assemble.py](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/orchestrator/assemble.py#L195-L203)。

### 2. 前后端接口需要共同验证的契约

发现一个具体问题：后端发送 `tool.started/tool.result`，任务页监听的却是 `tool_started/tool_result`。代码各自看起来没问题，接起来就收不到对应事件。

建议：
- 统一事件名称、字段和版本定义。
- 用后端生成的真实事件样本测试前端消费逻辑。
- 在已有 Python CI 基础上，补前端构建和关键事件回归。

不必马上全面迁移 TypeScript；先让“后端发什么，前端确实收得到”进入自动测试。

证据：[后端事件编码](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/app.py#L54-L60)、[前端订阅](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/frontend/src/tasks/RunDetail.jsx#L83-L87)。

### 3. 列表对象需要稳定身份，不能把行号当身份

记忆条目以文件行号作为组件 key，但编辑草稿、删除确认状态保存在组件内部。删除前面的条目后，后续行号移动，状态可能被另一条内容继承。

建议给记忆条目稳定 ID，将“内容身份”和“文件存储位置”分开。

最有价值的回归用例是：**编辑第二条时删除第一条，第二条的草稿不能跑到第三条上。**这类交叉操作，比继续补正常路径测试更能发现实际问题。

证据：[memory/App.jsx](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/frontend/src/memory/App.jsx#L100-L112)。

## 二、产品经理：让用户“知道怎么开始、随时接得回来、相信修改会保留”

### 1. 第一屏应该引导完成一个成功任务，而不是展示能力清单

项目定位是“从零搭建 Agent”，因此首次体验最重要的不是告诉用户支持多少工具，而是让他看懂一次完整运行。

建议提供一条无需外部配置的示例任务，展示：

```text
用户提出目标 → Agent 选择工具 → 展示执行结果 → 用户检查结果
```

同时清楚区分“体验模式”和“真实模型模式”，避免用户把假模型效果误认为项目能力上限。

教学解释可以渐进展开；不要把里程碑、底层配置和所有工具都塞进首次体验。

### 2. 用户离开页面后，应该还能接管原任务

当前从聊天页进入任务页再返回，聊天页没有主动找回正在运行的 Run。后台任务可能还在执行，前台却失去了对应的取消或待确认入口；再次发送又会被后端拒绝。

建议把“接回当前任务”作为明确产品能力：
- 刷新、跳页后自动恢复正在运行的任务。
- 等待确认时恢复同一个确认请求。
- 断网显示“连接中断，任务状态待确认”，不要表现得像任务已结束。
- 始终提供“查看当前任务”的入口。

验收不只是页面能打开，而是：**等待确认时跳页再回来，仍然可以拒绝或取消同一个任务。**

证据：[聊天页初始化](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/static/app.js#L518-L521)。

### 3. 用户手工整理的内容，要比自动生成更优先

目前用户重命名会话后，归档逻辑仍会重新生成标题，覆盖手工名称。

这会让用户觉得“我整理了也没用”。

建议明确一条产品规则：**自动生成用于填空，不覆盖用户主动编辑。**会话标题如此，后续计划名称、记忆标签也应遵循同样原则。

验收：重命名→新建会话→切回→再次归档，名称始终保留。

证据：[归档标题逻辑](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/app.py#L160-L165)。

### 4. 不知道的状态，不要展示成确定结论

任务页连接事件流失败后，会落入“本次运行没有计划”的提示。但“没拿到计划”和“没有计划”是两回事。

建议至少区分：
- 正在获取；
- 暂时断开，等待恢复；
- 已确认没有计划；
- 已有计划。

这类文案不是小修饰，它决定用户会不会沿错误方向排查。

证据：[RunDetail.jsx](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/frontend/src/tasks/RunDetail.jsx#L88-L101)。

## 三、UED 设计师：让用户“找得到操作、掌握阅读节奏、理解确认后果”

### 1. 核心操作不能依赖鼠标悬停

当前部分会话、记忆操作只在 hover 时显示；任务展开和会话切换也存在仅绑定点击的非按钮元素。

建议：
- 核心操作保持可发现，次要操作收进“更多”菜单。
- 展开、切换等操作使用原生按钮。
- 提供清晰的键盘焦点与展开状态。
- 触屏场景不能依赖 hover 才出现入口。

验收：只用 Tab、Enter、Space，就能切换会话、展开任务、编辑和删除记忆。

证据：[任务条目](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/frontend/src/tasks/App.jsx#L22-L35)、[操作按钮样式](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/static/style.css#L88-L96)。

### 2. 确认弹层需要真正接管交互，并区分两种取消

当前确认层只是显示出来，没有完整的焦点移入、背景隔离和焦点恢复；全局 Escape 又会取消整轮运行。

建议：
- 使用真正的模态交互，打开后焦点进入弹层。
- 明确区分“拒绝这次操作”和“取消整个任务”。
- 将“要做什么、影响哪些资源”放在主要位置，原始参数作为详情展开。
- Escape 的行为明确且只触发一次，不与全局取消叠加。

验收：不使用鼠标也能完成批准或拒绝，且不会误取消整个任务。

证据：[确认层结构](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/static/index.html#L37-L48)、[交互处理](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/static/app.js#L103-L121)。

### 3. 流式输出不能抢走用户的阅读位置

当前每次流式渲染和新增工具卡片都会滚到底部。用户想回看上面的代码或依据时，会被持续拉回最新位置。

建议：
- 用户本来就在底部时，自动跟随。
- 用户向上滚动后，停止跟随。
- 展示“有新内容／回到最新”按钮，由用户恢复跟随。
- 文本和工具卡片遵循同一规则。

这比先换配色、圆角或动效更值得投入，因为它直接影响长任务能不能看得下去。

证据：[流式渲染与滚动](file:///Users/eleme/SQL/output/cortex_arch_review_upload3/utf8_extracted/cortex-from-scratch-main/src/agent/server/static/app.js#L37-L45)。

## 四、如果只安排一轮迭代

建议把三个角色的工作合成三个可验收的小闭环：

| 优先级 | 交付目标 | 验收标准 |
|---|---|---|
| 1 | 任务随时接得回来 | 刷新、跳页、断网恢复后，仍能查看、确认和取消同一任务 |
| 2 | 用户修改不串、不丢 | 记忆删除不迁移编辑状态，会话手工标题不被覆盖 |
| 3 | 新人离线完成首个任务 | 无密钥、禁网可运行，工具事件正确展示，阅读位置不被抢走 |

**研发侧先补契约，产品侧先补流程，UED 侧先补控制感。暂时不用重写整个前端，也不需要继续增加多 Agent、拖拽编排或复杂基础设施。**
