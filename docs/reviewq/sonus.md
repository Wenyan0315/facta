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
