# ADR 094：backlog 收口与前端 CI——待办表维护、README 刷新、浏览器冒烟进流水线

- 状态：已裁定（R08 实现方案）
- 日期：2026-10-06
- 上游：[087](087-project-review-and-next-priorities.md) R08（整理当前 backlog，补前端 CI）
- 推翻关系：无新增

## 背景

087 复评时登记的三处维护债：

1. R01–R09 的完成状态没有单一可见面——完成与否要翻 git log。
2. README 滞后：ADR 数量仍写 71（实际已 93），沙箱说明停在
   「macOS seatbelt、其他平台回退」的 048 时代——086 之后是三档配置
   + seatbelt/bwrap 双后端 + 诚实降级，093 起还有了界面徽章。
3. 前端零 CI 防线：`frontend/` 无 lint/test 脚本，fw 产物靠人肉记得
   `npm run build`——忘重建时仓库里的产物与源码漂移，没有任何机器检查。

## 裁定

1. **待办表就维护在 087 的 R 表里**：已完成项在 TODO 列前缀
   ✅ 并链接到落地 ADR，未完成项保持原样。不另开 backlog 文件——
   R01–R09 本就来路清晰（087 是单一真值源），再开一个表就是第二份
   要维护的真值。「短的当前待办表」= 这张表本身保持短（九行），
   完成项不删行、只标注，历史可追溯。
2. **README 刷新两处**：ADR 数量 71 → 94（含范围表述 001–094，落盘时含本文）；
   沙箱说明改写为三档配置（`FACTA_SANDBOX`）× 确认策略档
   （`FACTA_APPROVAL_POLICY`）× 后端探测（seatbelt/bwrap/无=诚实
   降级），与 086/093 口径一致。其余段落不动。
3. **CI 加前端构建闸**：ci.yml 新增 steps——setup-node +
   `npm ci` + `npm run build`（frontend/），随后
   `git diff --exit-code src/facta/server/static/fw/` 钉住
   「产物必须与源码同步提交」：忘重建的 PR 直接红。不引入前端 lint
   工具链（eslint/prettier）——四页 Preact 组件量级不配一套规则体系，
   触发信号=fw 页面数翻倍或多人协作。
4. **浏览器冒烟测试进 CI（少量、关键、可跳过）**：
   `tests/test_browser.py` 用 Playwright（sync API）起真实
   uvicorn 子进程（`FACTA_PROVIDER=mock` + 隔离 `FACTA_DATA_DIR`，
   随机空闲端口），跑三条关键流程：
   ①聊天页加载且环境徽章可见（093 的徽章回归网）；
   ②fork 按钮点击后会话清单 +1 且切到副本（093 的 fork 回归网）；
   ③memory 面板加载出分组标题（Preact 页挂载回归网）。
   **跳过纪律**：playwright 未安装或 chromium 二进制缺席 →
   `pytest.skip`——本地没装浏览器不影响 `pytest -q` 全绿；
   CI 显式 `playwright install --with-deps chromium` 后恒跑。
   playwright 进 `dev` extras（测试基础设施，与 pytest 同列）。

### 为什么不是更全的 E2E 套件

「少量关键浏览器流程」是 087 原话。三条用例各钉一个「这次不测就会
真漏」的回归面（徽章静默失败、fork 无入口、Preact 挂载白屏），
再多是给假想需求写测试。前端交互细节仍靠手测，触发信号=浏览器
冒烟拦住过一次真回归后再议扩充。

### 为什么不用 pytest-playwright 插件

直接用 `playwright.sync_api` 手写 fixture（server 拉起 + browser
生命周期共 ~40 行），比引入插件的 fixture 魔法透明，依赖面也更小。
第二次需要更复杂的 browser fixture 时再评插件。

## 边界（显式记录）

- 浏览器测试只跑 chromium——webkit/firefox 矩阵对个人项目无收益。
- CI 的 `git diff --exit-code` 钉产物一致，但不钉构建产物的字节级
  稳定（不同机器 npm 版本产生的细微差异由 lockfile 兜住大头）。
- README 的 ADR 数量从此是手工维护的硬编码——触发信号=再次滞后
  被注意到时，考虑改为「见 docs/decisions/ 目录」不写数字。
- 浏览器冒烟不测真实 LLM（mock provider），不覆盖流式渲染细节。

## 验收（087 的 R08 完成标准，逐条）

- [x] 维护短的当前待办表，已完成项链接到 ADR：087 R 表 ✅ 标注。
- [x] 更新 README 中滞后的 ADR 数量、沙箱等说明。
- [x] CI 增加前端构建：npm build + 产物一致性闸。
- [x] 少量关键浏览器流程测试：三条（徽章/fork/memory 挂载）进 CI，
  本地无浏览器可跳过。
- [x] 三门全绿。
