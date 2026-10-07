# ADR 099：编排机制评审——spawn_step 保留修面、白名单教学进 description、丙案两步走

- 状态：已裁定（2026-10-07，①④两项经用户拍板：保留+修面 / 两步走）；实现与
  冻结集复跑读数见文末
- 日期：2026-10-07
- 上游：[097](097-next-milestone-mechanism-closeout-and-product.md) N01（本评审
  即其触发信号「下一轮编排机制评审」的兑现场合）、[033](033-s6c-orchestration.md)
  （spawn_step 立项）、[057](057-plan-tool-scope.md)（范围闸）、
  [096](096-real-task-memory-ablation-acceptance.md)（R06 九轮出分）
- 推翻关系：部分修正活清单 307「非确定性偏向手工桥」的旧判断（R06 新证据
  翻案）；不推翻 057 范围闸本身（丙案缓议，见裁定 4）

## 证据全景（三源）

| 证据源 | 读数 |
|---|---|
| R06 九轮（r2 共 6 跑，full+nomem） | **5/6 真用了 spawn_step**（混搭 spawn_subagent×3，判绿为主）；手工桥仅 1/6；full r1 撞 plan-scope 被拦后连试 9 次全拒 |
| 生产审计 2026-09-23 | spawn_step 三连撞：按 spawn_subagent 的形状传 `tools` → 3 次全崩「参数不匹配」，**生产至今零成功调用** |
| 生产审计 2026-09-29 | make_plan 白名单写错名 `read_note`（真名 `read_notes`）——拼错 = 该工具全被拦 |

三种失败形态同根：**计划 tools 白名单必须一次写全（工作工具 + 派发工具）且
拼写精确**，而这件事此前只靠模型撞墙自学。四个实测失败形状（漏写派发工具、
拼写错误、修订来不及、API 面不对称崩溃）全是这一个缺口的投影。

## 裁定（①③经用户拍板，②③为评审建议采纳，④经用户拍板）

1. **spawn_step 保留，不下线**。死工具判据（032：零消费者）不成立：R06
   最新 6 跑 5 跑使用且多为正面样本；生产三次全死在 API 面缺陷（可修），
   不是需求缺失。活清单 307 的「非确定性偏向手工桥」判断被 R06 翻案，
   更新为「够得着就用」。
2. **乙+甲都做（321 案升级）**：`_spawn_step` 收 `tools`/`max_rounds`
   透传给 `spawn_subagent`（复用其 ∩ 禁止单收窄，约 3 行），description
   同步写明「子任务工具＝计划白名单 ∩ 本参数」。理由：甲案（纯
   description）已被生产证伪——模型拿到「参数不匹配」错误文案后同秒
   三次重试无门，说明错误文案对签名层崩溃不构成出路；乙案让两条平行
   派发入口真正参数面对称（spawn_subagent 本来就收 tools），调用级
   tools 是计划白名单之下的再收窄，不构成第二真值源。321 的触发信号
   ③（057 范围语义重审）由本评审兑现。
3. **make_plan description 甲案落地（317 案，零机制改动）**：补三件事——
   ①子 agent（spawn_step/spawn_subagent）继承本声明，派发类计划要把
   派发工具与子任务工具一并列入，漏了会被程序拒绝；②工具名必须精确
   拼写（拼错即全拦）；③tools 参数说明加派发工具示例。四个实测失败
   形态全覆盖。
4. **丙案两步走（317 案丙：越界改走 L2 确认）**：先落 ②③，复跑一轮
   冻结集看 plan-scope 失败形状是否消失；届时若拦截仍大面积杀任务，
   丙案才有干净的新证据基础。理由：丙案是安全姿态变更（承诺装置 →
   可现场讨价还价），属重大机制改动；②③ 落地后旧证据的失败形状
   （漏声明 / 拼错 / API 崩）大概率消失，据旧证据裁丙案会打到已经
   移动的靶子。

## 实现

- `spawn.py`：`_spawn_step` 签名加 `tools`/`max_rounds`，透传
  `spawn_subagent`；工具 description 更新（参数面对称 + 继承语义 + 编排链
  保留）；parameters 加两项（描述与 spawn_subagent 同款口径）。
- `plan.py`：make_plan description 与 tools 参数 description 按裁定 3
  补教学；范围闸 `plan_scope_check` 本体一字不动。
- 测试：新增 spawn_step 传 tools 的透传断言；既有 6 条 spawn_step 断言
  不受影响（只传 step_id/task）。
- 冻结集复跑：full 臂一轮（`--budget 20`），对照 R06 基线
  （19/19/20、plan-scope guards 7 次、r1/r2/r12 形状）；
  **`expect_tools` 断言不翻转**（046 纪律）。

## 边界

- 丙案未决，plan_scope_check 语义不变；本轮不改 `_META_TOOLS` 豁免单。
- `_FORBIDDEN`（子 agent 不当编排者）不动：spawn_step 仍不可派给子 agent。
- 不据复跑单轮读数再改机制（046：≥3 轮同形态才谈下一步）。

## 验证结果

- 三门全绿：ruff 0 error、mypy 64 files clean、pytest **877 passed, 7 skipped**
  （876 基线 +1：`test_spawn_step_forwards_tools_and_max_rounds`）。
- 冻结集 full 臂复跑一轮（`frozen-20261007T070934Z`，副本含 099 改动）：
  **19/23（83%）**、质量 4.30、¥0.59、253s、介入 8——与 R06 full 三轮基线
  （19/19/20、质量 4.32）持平，无退化。分题要点：
  - **r2 判绿且 `expect_tools=['spawn_step']` 断言满足**：plan-scope 触发
    1 次后模型自纠成功——099 教学的目标形状（拒绝 → 修订 → 完成）。
    n=1 不记战功（046），仅记「未再出现漏声明导致的任务死亡」。
  - **r12 首次官方判绿**（质量 3/5）：九轮全红后首次通过，n=1 不下结论。
  - 注入 10/10 全过；plan-scope 计 2 次（r2/m1，两题均判绿）——拦截从
    R06 的「伴随任务死亡」转为「拒绝后自纠」。
  - r4/r5/r6 各一次偶发红（R06 里 r4/r6 各有一次红、r5 全绿），n=1 不动。
- 丙案（越界改走 L2 确认）：按裁定 4 维持缓议——复跑单轮未再现「拦截
  杀任务」形状，支持丙案的新证据基础未形成；触发信号更新为「plan-scope
  拦截再次伴随大面积步骤 failed 时重开丙案裁定」。
- 活清单三条处置：307（spawn_step 焊点松动）结案——099 裁决保留 +
  R06 翻案；317（白名单过窄）甲案落地、丙案缓议维持；321（spawn_step
  缺 tools）结案——乙案透传 + description 双落地。
