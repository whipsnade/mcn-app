# 2026-07-28 快捷页面状态缓存（QuickFeatureCache）

## 背景与目标

右侧四个快捷页面（小红书爆贴、抖音爆贴、达人推荐、活动评估）此前把查询结果与表单
状态放在面板组件本地 state：切换快捷 Tab 时面板被卸载，结果全部丢失，切回后用户需
重新查询（每次 MCP 调用固定 10 积分，达人推荐约 20 积分），既费积分又打断操作流。

目标：切 Tab 不丢状态、不重复请求，缓存按用户隔离，不做跨用户/跨会话持久化，无后端
变更。设计：`docs/superpowers/specs/2026-07-28-quick-tab-state-cache-design.md`；
计划：`docs/superpowers/plans/2026-07-28-quick-tab-state-cache.md`。
分支 `codex/quick-tabs-cache`。

## 主要改动

1. **新增 `src/state/QuickFeatureCache.tsx`**：`QuickFeatureCacheProvider` 以
   `userId` 为 key 持有缓存（userId 变化即渲染期同步重置，杜绝跨用户泄露），缓存
   范围四类状态——爆贴结果（`topPosts`，按 platform 分键，小红书/抖音独立）、
   达人推荐（预算 + 名单 + 积分消耗）、活动评估（活动名 + 达人名单 + 未提交草稿 +
   报告）。`queryTopPosts`/`queryKolRecommendations` 入 Provider，按平台/按预算递增
   请求序号抑制过期响应；查询失败保留旧列表只更新错误（与迁移前面板行为一致）。
2. **Provider 挂载**（`src/App.tsx`）：挂在快捷 Tab 容器外层、四个面板共同祖先处，
   面板仍按 active tab 条件渲染（卸载），状态全部经缓存恢复。
3. **三个面板迁移**：`TopPostsPanel`（爆贴两平台复用，读 `topPosts[platform]`）、
   `KolRecommendPanel`（预算/名单/积分入缓存，800ms 防抖保留，重挂载以缓存预算与
   查询序号为基准不自动重查）、`EvaluatePanel`（表单与报告入缓存，提交时快照参数，
   请求期间卸载不丢结果）；loading/submitting/收藏 busy 等瞬态仍留面板本地。
4. **集成回归**（`src/App.test.tsx`）：最小 Tab 容器 harness 复刻 App.tsx 的条件
   渲染结构 + 真实四面板 + 真实 Provider，覆盖：爆贴两平台分键独立且切回不重复
   请求、达人推荐切回预算/名单保留、活动评估切回报告/表单保留、四 Tab 状态共存
   循环切换请求数不变。注：行为在 Task 1-4 已修复，集成测试在现状下即为绿色，
   作为兜底回归网保留（不为红而红）。

无后端变更，无 API 契约变更。

## 验证结果

- `npx vitest run src/App.test.tsx`：4 条集成测试全绿。
- 全量：`npm run test`（Vitest 306+ 全绿，含各面板与 Provider 单测）、
  `npm run lint`（tsc --noEmit）、`npm run build` 全部通过。

## 遗留事项与注意事项

- 达人推荐存在「新预算 + 旧列表」的短暂不一致窗口：拖动预算条后 800ms 防抖期内
  切 Tab，列表仍是旧预算结果，切回后防抖查询才发出（序号护栏保证不错乱）。
- 活动评估 in-flight 期间可再次提交（重新评估 → 再点开始评估）存在重复提交竞态，
  后回包覆盖先回包，后续可加请求序号守卫（与爆贴/推荐一致）。
- 活动评估失败/成功路径均保留未提交的 kolDraft，行为已对齐迁移前旧版；如需
  「成功后清空草稿」属产品决策，另行变更。
