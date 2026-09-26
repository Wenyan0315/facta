"""记忆消融场景集（B 路）：从真实 learned 条目反推的问答题（2026-09-26）。

与 dataset.py 的分工：dataset.py 考「语料检索」（答案在 data/notes/evals/corpus 里，
走 search_notes）；本文件考「记忆注入」（答案只在 data/learned 四桶里，只能通过
build_default_agent 的 _learned_block/_user_memory_block 注入进 prompt——检索路径
够不着，learned 目录不在语料源里）。这正是 B 路消融的对照点：
  有记忆（learned_dir=真目录）→ 答得出；无记忆（learned_dir=None）→ 答不出。

题目来源：data/learned 现有 24 条逐条人工盘点，**13 条可反推**（答案稳定的事实），
**11 条剔除**，剔除分两类（清单在文件末尾对账，防「挑着出」）：
  ① 过期临时状态（价值已随时间归零，考了反而奖励「记住该忘的」）
  ② **已腐化的易腐事实**——首轮跑测时实证：other.md「run_turn 在 loop.py 第 113 行，
     该文件共 227 行」（2026-09-17）对照现实是**第 340 行、共 501 行**，两个数字全错。
     9 天代码演进就腐化，且系统无任何机制能发现（固化只追加、无校验、无过期、
     无 tombstone）。留着这题等于奖励模型背错答案，故剔除。该条目已于同日修正为
     只留文件路径（行号/行数不再入库）。
     同批 o-terminal 的「（未跟踪）」注记也已腐化（该测试文件现已入 git），
     但两个文件路径仍正确，作为记忆召回题依然成立，保留。
user.md 为空，本轮只测项目级三桶。

每行 = (id, 问题, 答案指纹列表, 来源桶)。
判定：回答含任一指纹即「用上了记忆」。指纹取条目里的独特短语，足够冷僻，
无记忆时模型不可能瞎编命中（如「inv_ prefix」）。
"""

CASES: list[tuple[str, str, list[str], str]] = [
    # ---- decisions（2 条）----
    ("d-php", "我们这个项目里 PHP 的定位是什么？", ["工具层", "行动部"], "decisions"),
    ("d-refactor", "判断要不要重构之前，要先分清什么？", ["重构对象"], "decisions"),
    # ---- constraints（8 条）----
    ("c-arch-doc", "项目里的架构决定都必须记录在哪里？", ["docs/architecture.md"], "constraints"),
    ("c-inv-prefix", "向量库一致性校验的测试，函数名要用什么前缀？", ["inv_"], "constraints"),
    ("c-tool-log", "Agent 工程里怎么判断它是真干活还是嘴上说做了？", ["调用序列"], "constraints"),
    ("c-no-readfile", "我们有 read_local_file 这个工具吗？文件读写能力是哪种？", ["笔记读写"], "constraints"),
    ("c-no-refactor", "没有具体症状的时候，对代码应该采取什么态度？", ["不重构", "别动"], "constraints"),
    ("c-session-trap", "run_turn 的 session 参数有个什么坑？", ["列表身份", "原地变异"], "constraints"),
    ("c-summarizer", "摘要压缩走的是哪条链？经不经过用户链的语义档？", ["内部链"], "constraints"),
    ("c-spawn-tools", "spawn_step 接受 tools 参数吗？工具限制要怎么写？", ["任务书"], "constraints"),
    # ---- other（3 条，o-run-turn 因条目腐化剔除，见上）----
    ("o-terminal", "终端工具的源码在哪个文件？对应的测试文件呢？", ["terminal.py", "test_terminal.py"], "other"),
    ("o-three-layer", "我们的架构分哪三层？", ["模型层", "工具层", "数据层"], "other"),
    ("o-two-search", "search_notes 和 search_history 分别搜什么？", ["语义", "逐字"], "other"),
]

# 剔除清单（11 条，对账用）：
# ① 已腐化易腐事实 1 条：run_turn 行号/文件行数（other，见文件头实证）
# ② 过期临时状态 10 条：气象预报窗口、PHP 函数封装下一步、Agent 规划需求未明确、
#    RAG.md 缺内容、RAG.md 待补、笔记库 14 篇盘点、spawn 检索 RAG 的用户要求、
#    OAuth 未找到（以上 other 8 条）+ run_command 白名单确认、（constraints 已在
#    C1-9 真机验收实测过，不重复考）。decisions 无剔除。
