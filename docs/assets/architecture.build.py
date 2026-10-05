"""按 docs/assets/architecture.prompt.md 规范重建 facta 架构图（纯 SVG，字号加大版）。"""
import html
from pathlib import Path

W, H = 2560, 1820
BG, CARD, BORDER = "#0B1220", "#121E30", "#2A3A52"
TXT, SUB = "#F2F5F9", "#A8B6C8"
AMBER, CYAN, TEAL = "#E8A33D", "#4FC3F7", "#2BB3A3"
FONT = "PingFang SC, Hiragino Sans GB, Arial Unicode MS, sans-serif"

svg = []
def add(s): svg.append(s)
def esc(t): return html.escape(t)

def rect(x, y, w, h, fill=CARD, stroke=BORDER, sw=2, rx=10, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

def text(x, y, t, size=22, fill=TXT, weight="normal", anchor="start", spacing=None):
    sp = f' letter-spacing="{spacing}"' if spacing else ''
    add(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" fill="{fill}" font-weight="{weight}" text-anchor="{anchor}"{sp}>{esc(t)}</text>')

def line(x1, y1, x2, y2, stroke, sw=3, dash=None, marker=True):
    m = f' marker-end="url(#arr-{stroke.strip("#")})"' if marker else ''
    d = f' stroke-dasharray="{dash}"' if dash else ''
    add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}"{d}{m}/>')

def path(d, stroke, sw=3, dash=None, marker=True):
    m = f' marker-end="url(#arr-{stroke.strip("#")})"' if marker else ''
    dd = f' stroke-dasharray="{dash}"' if dash else ''
    add(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{sw}"{dd}{m}/>')

def card_title(x, y, t, accent=AMBER):
    add(f'<rect x="{x}" y="{y-24}" width="6" height="26" rx="3" fill="{accent}"/>')
    text(x+18, y, t, 27, TXT, "bold")

add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
add(f'<defs>'
    f'<marker id="arr-E8A33D" markerWidth="12" markerHeight="12" refX="9" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{AMBER}"/></marker>'
    f'<marker id="arr-4FC3F7" markerWidth="12" markerHeight="12" refX="9" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{CYAN}"/></marker>'
    f'<marker id="arr-2BB3A3" markerWidth="12" markerHeight="12" refX="9" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{TEAL}"/></marker>'
    f'<marker id="arr-start-4FC3F7" markerWidth="12" markerHeight="12" refX="1" refY="5" orient="auto"><path d="M10,0 L0,5 L10,10 z" fill="{CYAN}"/></marker>'
    f'<marker id="arr-start-2BB3A3" markerWidth="12" markerHeight="12" refX="1" refY="5" orient="auto"><path d="M10,0 L0,5 L10,10 z" fill="{TEAL}"/></marker>'
    f'</defs>')
add(f'<rect width="{W}" height="{H}" fill="{BG}"/>')

# ---------- 标题区 ----------
text(60, 84, "facta · 系统架构", 48, TXT, "bold")
text(60, 128, "本地个人 AI Agent｜执行循环、长期记忆与工具扩展", 26, SUB)
# 图例
lx = 2330
add(f'<line x1="{lx-330}" y1="66" x2="{lx-280}" y2="66" stroke="{AMBER}" stroke-width="4"/>')
text(lx-266, 74, "实线：调用与返回", 21, SUB)
add(f'<line x1="{lx-60}" y1="66" x2="{lx-10}" y2="66" stroke="{SUB}" stroke-width="3" stroke-dasharray="8 6"/>')
text(lx+4, 74, "虚线：装配与上下文", 21, SUB)

def band_head(y, label):
    text(60, y, label, 30, TXT, "bold", spacing="2")

# ---------- 01 交互入口 ----------
band_head(196, "01　交互入口")
rect(60, 224, 760, 118)
card_title(88, 268, "CLI")
text(88, 306, "python -m facta", 22, CYAN)
text(88, 340, "多轮对话 · 操作确认", 22, SUB)
rect(860, 224, 1640, 118)
card_title(888, 268, "Web · FastAPI + SSE")
text(888, 306, "对话 / 任务计划 / 记忆 / 知识语料 / 知识图谱", 22, TXT)
text(888, 340, "会话准入 · 流式事件 · 取消与确认", 22, SUB)

# 入口 → 装配
line(1280, 342, 1280, 496, AMBER, 4)

# ---------- 02 统一装配 ----------
band_head(478, "02　统一装配 · orchestrator/assemble.py")
rect(60, 500, 2440, 84)
text(88, 552, "AppContext 共享资源 · 按会话构建 Agent 与工具注册表 · 可选能力按配置启用", 24, TXT)

# 装配 → 执行核心（构建 Agent）；虚线供给记忆 / 模型
line(1280, 584, 1280, 676, AMBER, 4)
text(1300, 640, "构建 Agent", 21, AMBER)
path("M900 584 L460 584 L460 676", SUB, 2.5, dash="8 6")
path("M1660 584 L2120 584 L2120 676", SUB, 2.5, dash="8 6")

# ---------- 中行三卡 ----------
MY, MH = 676, 436          # 中行 y / 高
# 左：记忆与会话
rect(60, MY, 700, MH, stroke=TEAL, sw=2.5)
card_title(88, MY+46, "记忆与会话 · memory/", TEAL)
ml = ["Session：原文 · 滚动摘要 · PlanBoard",
      "项目记忆 + 用户记忆 → 主 Agent 上下文",
      "固化：萃取 → 枚举审查 → 硬校验",
      "Web 每轮重读记忆快照"]
for i, s in enumerate(ml):
    text(88, MY+100+i*44, s, 22, TXT if i<2 else SUB)
add(f'<line x1="88" y1="{MY+286}" x2="732" y2="{MY+286}" stroke="{BORDER}" stroke-width="1.5"/>')
text(88, MY+326, "sessions/*.json · learned/*.md", 20, SUB)
text(88, MY+362, "~/.facta/user.md", 20, SUB)
text(88, MY+410, "并行支撑服务 · 非必经检索级", 19, "#6E7F95")

# 右：模型与网关
rect(1800, MY, 700, MH, stroke=CYAN, sw=2.5)
card_title(1828, MY+46, "模型与网关 · core/", CYAN)
rl = ["LLM 接口 · OpenAI 兼容供应商",
      "RobustLLM：重试 · 超时 · 精确缓存 · 熔断",
      "FallbackLLM：模型降级",
      "UsageLedger：调用用量记录",
      "用户链 / 内部链"]
for i, s in enumerate(rl):
    text(1828, MY+100+i*44, s, 22, TXT if i<2 else SUB)
add(f'<line x1="1828" y1="{MY+330}" x2="2472" y2="{MY+330}" stroke="{BORDER}" stroke-width="1.5"/>')
text(1828, MY+370, "语义缓存默认关闭；场景路由可选", 20, SUB)

# 中：执行核心（amber 边框强调）
CX, CY, CW, CH = 820, MY, 920, MH
rect(CX, CY, CW, CH, stroke=AMBER, sw=3.5)
card_title(CX+28, CY+46, "03　执行核心 · orchestrator/")
text(CX+28, CY+88, "Agent：系统提示 · 工具范围 · 轮次预算 · run_turn · ReAct", 21, SUB)

# 四节点闭环
NW, NH, NY = 188, 62, CY+116
nx = [CX+34, CX+250, CX+466, CX+682]
labels4 = ["构建上下文", "模型决策", "执行工具", "回填结果"]
for x, lb in zip(nx, labels4, strict=True):
    rect(x, NY, NW, NH, fill="#0E1830", stroke=AMBER, sw=2, rx=8)
    text(x+NW/2, NY+NH/2+8, lb, 21, TXT, "bold", anchor="middle")
for i in range(3):
    line(nx[i]+NW+4, NY+NH/2, nx[i+1]-6, NY+NH/2, AMBER, 3)
# 回填结果 → 模型决策 回边
ry = NY + NH + 46
path(f"M{nx[3]+NW-30} {NY+NH+6} L{nx[3]+NW-30} {ry} L{nx[1]+NW/2} {ry} L{nx[1]+NW/2} {NY+NH+8}", AMBER, 3)
text(CX+CW/2+180, ry+34, "结果驱动下一轮决策", 19, AMBER)

cl = ["计划审批 → 步骤执行 → 状态回写 → 收官",
      "可选场景路由：调整首轮工具菜单",
      "循环限制 · 协作式取消 · 事件回调"]
for i, s in enumerate(cl):
    text(CX+28, CY+296+i*44, s, 22, TXT if i==0 else SUB)

# 记忆 → 执行核心（虚线，上下文）
line(760, MY+150, CX-8, MY+150, SUB, 2.5, dash="8 6")
text(782, MY+136, "上下文", 20, SUB)
# 执行核心 ↔ 模型（青色双向）——走下方空档，避开右侧卡片文字
add(f'<line x1="{CX+CW+6}" y1="{MY+270}" x2="1794" y2="{MY+270}" stroke="{CYAN}" stroke-width="3" marker-start="url(#arr-start-4FC3F7)" marker-end="url(#arr-4FC3F7)"/>')
text(CX+CW-6, MY+296, "模型调用 / 回复", 20, CYAN, anchor="end")

# ---------- 04 行两卡 ----------
TY = MY + MH + 88          # 1090+... 实际 = 676+436+88 = 1200
TH = 356
# 工具执行（左宽卡）
rect(60, TY, 1380, TH)
band_head(TY-24, "04　工具执行 · tools/")
# band_head 会画到 x=60 y=TY-24；标题色块叠在卡外
text(88, TY+56, "ToolRegistry：参数校验 · 计划范围 · 操作确认 · 审计", 23, TXT)
tl = ["内置工具：time / history / notes / files / terminal / web / todo / plan / spawn / graph",
      "子 Agent：复用 run_turn · 并行派发 · 可选 worktree 隔离",
      "MCP 客户端：stdio / HTTP → 外部工具服务器"]
for i, s in enumerate(tl):
    text(88, TY+108+i*46, s, 22, SUB)
text(88, TY+282, "文件访问围栏 · macOS seatbelt（其他平台无此后端）", 21, TEAL)

# 知识检索（右卡）
rect(1520, TY, 980, TH, stroke=TEAL, sw=2.5)
card_title(1548, TY+46, "知识检索 · knowledge/", TEAL)
kl = ["笔记 → 切块 → Embedding → 向量检索",
      "Chroma 持久化 · 内容指纹增量同步",
      "缺依赖或密钥：词袋 + 内存库",
      "知识图谱：抽取 · 同步 · 关系查询"]
for i, s in enumerate(kl):
    text(1548, TY+100+i*44, s, 22, TXT if i<2 else SUB)
add(f'<line x1="1548" y1="{TY+286}" x2="2472" y2="{TY+286}" stroke="{BORDER}" stroke-width="1.5"/>')
text(1548, TY+326, "检索带出处；模型按需调用", 20, SUB)

# 执行核心 ⇄ 工具：两条分离 amber 箭头
ax1, ax2 = 1000, 1180
line(ax1, MY+MH+6, ax1, TY-8, AMBER, 4)
text(ax1+14, (MY+MH+TY)/2, "工具调用", 21, AMBER)
line(ax2, TY-8, ax2, MY+MH+6, AMBER, 4)
text(ax2+14, (MY+MH+TY)/2+30, "结果回填", 21, AMBER)

# 工具 ⇄ 知识：teal 双向
ky = TY + 150
add(f'<line x1="1446" y1="{ky}" x2="1514" y2="{ky}" stroke="{TEAL}" stroke-width="3.5" marker-start="url(#arr-start-2BB3A3)" marker-end="url(#arr-2BB3A3)"/>')
text(1290, ky-14, "notes / graph", 20, TEAL)

# ---------- 底部支撑 ----------
SY = TY + TH + 90          # 1200+356+90 = 1646
add(f'<line x1="60" y1="{SY-46}" x2="2500" y2="{SY-46}" stroke="{BORDER}" stroke-width="1.5"/>')
text(60, SY-12, "运行支撑与验证（支撑摘要 · 非运行顺序）", 24, SUB, "bold")
BH = 128
rect(60, SY, 1200, BH)
card_title(88, SY+44, "持久化与恢复")
text(88, SY+84, "会话 JSON · 记忆 Markdown · 向量库 · 图谱 JSON", 21, TXT)
text(88, SY+114, "工具边界检查点 / heal · 审计 JSONL · 未知副作用需核验", 21, SUB)
rect(1300, SY, 1200, BH)
card_title(1328, SY+44, "评估与外部接入")
text(1328, SY+84, "tests / evals：检索 · 回答 · 消融 · 冻结任务与恢复评估", 21, TXT)
text(1328, SY+114, "evalkit 评估复用组件 · memory_server.py 只读记忆 MCP", 21, SUB)

# ---------- 脚注 ----------
text(60, H-30, "图示当前实现。项目运行记忆不随示例发布；外部服务和可选能力依配置启用。", 20, "#6E7F95")

add('</svg>')

page = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:{BG};}} svg{{display:block;}}</style>
</head><body>{"".join(svg)}</body></html>'''

out = str(Path(__file__).resolve().parent / "architecture.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(page)
print("written", out)
