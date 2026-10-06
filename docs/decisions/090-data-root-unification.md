# ADR 090：统一数据目录配置——运行时路径推导收口 + 围栏同源（R03）

状态：已落地（2026-10-06）
上游：087 R03（P0）

## 背景

`paths.DATA_ROOT` / `FACTA_DATA_DIR` 环境覆盖早已就位（071 触发信号兑现，
pip 安装场景数据目录与代码目录分离），但只覆盖了一半：

1. `assemble.py` 的 `TODOS_PATH` / `AUDIT_DIR` / `VECTOR_DB_DIR` 仍锚
   `WORKSPACE_ROOT / "data"`——设了 `FACTA_DATA_DIR` 启动，待办/审计/向量库
   照旧写进默认目录，「数据根搬家」是假的。
2. 迁移源 `MEMORY_PATH`（S8a 退役的老 active 固定位）不能顺手一起改锚——
   改了 env 场景下迁移源指向空新根，老会话静默「消失」。
3. 写入围栏两份字面量：`files.py` 与 `sandbox.py` 各抄一份
   `_BLACKLIST_DIRS`（`data/audit`、`data/vector_db` 等六项），且只认
   「data/ 在仓库内」一种布局——数据根迁出后 deny 规则空转，真目录裸奔
   （087 评审的原话病：「几处副本恰好没漂移」式的侥幸）。

## 裁定（Option A：推导收口 + 迁移源锚死）

1. **运行时路径统一从 `DATA_ROOT` 推导**：`TODOS_PATH = DATA_ROOT/"todos.json"`、
   `AUDIT_DIR = DATA_ROOT/"audit"`、`VECTOR_DB_DIR = DATA_ROOT/"vector_db"`。
   按 paths.py 自己的规则（「只被一个模块用的路径留在消费者本地」），三者
   只有 assemble 一个消费方，**不搬进 paths.py**，只换锚点。
2. **迁移源锚死默认旧位**：`MEMORY_PATH` 保持 `WORKSPACE_ROOT/data/memory/
   session.json` 不变。旧布局物理上只可能存在于仓库内安装（env 覆盖的真
   消费者是 pip 安装全新启动，无旧数据可迁）；env 覆盖 = 全新数据根，启动
   **不做跨根搬迁**——记忆不可逆，不猜不合并（045/064/`migrate_legacy_active`
   同款保守）。`migrate_legacy_active` 对不存在的源返回 None，幂等安全。
3. **围栏同源同推导**：`paths.py` 新增 `_fence_entry()` 助手——在
   WORKSPACE_ROOT 内的条目保持相对 posix（`data/notes`），根外一律绝对路径。
   `MEMORY_WRITE_FENCE` 改由 `NOTES_DIR/LEARNED_DIR/GRAPH_PATH` 推导；
   两份 `_BLACKLIST_DIRS` 字面量收成 `paths.BLACKLIST_DIRS` 单一对象，
   消费方 import-as（`files.py` / `sandbox.py`，052 同款同源模式）。
   默认布局下输出与旧字面量逐字节一致——seatbelt profile、bwrap argv、
   files.py 比对全部零变化。
4. **sandbox 臂补绝对条目语义**：seatbelt 拼接从字符串 `"{r}/{d}"` 改
   Path join（`root / d`，pathlib 对绝对右操作数返回其本身）；bwrap 臂
   本就是 `root / rel` + `exists()` 判断，不用改。

为什么不收进 files.py 的 `_BLACKLIST_PARTS`（`.env`/`.git`）：那是「路径
部件」黑名单不是「数据目录」清单，与数据根无关，不动。

## 边界（显式记录）

- **tmp 族数据根**：seatbelt 写白名单含 TMPDIR//private/var/folders——
  `FACTA_DATA_DIR` 设在其下时，相对布局时代的 deny 会整圈空转；绝对条目
  正是补这个缺口（R03 核对围栏的实质收益）。
- **files.py 臂对根外数据是空转而非漏洞**：`_resolve_in_workspace` 先拒
  workspace 外的写，绝对围栏条目永远比对不上；workspace 内的自定义
  DATA_ROOT 仍被相对条目拦住。
- **DATA_ROOT 仍是模块加载期一次性结算**（ADR 口径见 paths.py 注释）：env
  改后不热生效，测试走 `importlib.reload`；evals 离线脚本不设 env，语料
  继续钉仓库。
- 「配置临时数据目录启动时，不往默认目录产生新数据」靠单一真值源保证：
  全部运行时路径的锚点只有 `DATA_ROOT` 一处推导（grep `WORKSPACE_ROOT /
  "data"` 应只剩迁移源一行），测试钉推导关系而非起全量 assemble。

## 验收清单（对应 087 R03 行）

- [x] 待办、审计、向量库统一从 `DATA_ROOT` 推导（assemble.py 三常量换锚）
- [x] 迁移源核对：`MEMORY_PATH` 锚死默认旧位，env 场景不跟随、幂等空转
- [x] 写入围栏核对：围栏单一真值源 + 根外绝对条目，tmp 族数据根缺口补上
- [x] 测试：默认布局逐字节不变 + env 重载后全部运行时路径落在临时根、
      默认根零引用 + seatbelt profile 含绝对 deny
- [x] `architecture.md` 版本升 v0.108，索引表加本行
