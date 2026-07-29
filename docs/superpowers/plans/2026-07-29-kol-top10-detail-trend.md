# 跨平台 Top10 KOL 详情补全与趋势图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 圈选任务完成后按全平台近 30 天单篇平均互动量选取 Top20，按平台批量补全三类达人详情（300 积分预算上限），并在 KOL 分析页显示其中 Top10 的十条近四周互动趋势折线。

**Architecture:** 新增与 `kol_selection_sets` 绑定的详情快照表，保存 Top20 排名、三个 scope 的执行状态、受众/发帖事实与四周趋势序列。任务执行器在 KOL 圈选模型循环结束、自动 KOL 报告生成前按平台批量运行详情补全器；BI 通过新的只读趋势端点按名单版本加载 rank 1–10 快照，避免查看页面重新调用 MCP。

**Tech Stack:** FastAPI、SQLAlchemy Async、Alembic、现有 `McpGateway` 计费状态机、React 19、TypeScript、Recharts、pytest、Vitest。

**Scope note:** 本计划先实现详情补全和趋势事实层。`kol_score_v2` 的八维换算公式尚未逐项确认，不能在本计划中擅自改变评分和历史评级；补全后的事实数据会为下一项评分改造提供输入。

---

### Task 1: 建立 Top20 详情快照持久化契约

**Files:**
- Create: `backend/migrations/versions/0024_kol_selection_detail_snapshots.py`
- Modify: `backend/app/selection/models.py`
- Create: `backend/app/selection/detail_snapshots.py`
- Create: `backend/tests/selection/test_detail_snapshots.py`

- [ ] **Step 1: 写失败的模型与仓储测试**

覆盖：

```python
# 同一 selection_set/platform/kol_uid 的快照 upsert 不重复建行；
# rank、ranking_interaction、scope 状态和四周 trend_points 可读取；
# 不同 selection set 的相同达人互相隔离；
# rank > 20 被拒绝。
```

- [ ] **Step 2: 验证红灯**

Run: `cd backend && .venv/bin/pytest tests/selection/test_detail_snapshots.py -q`

Expected: FAIL，模型、迁移和仓储尚不存在。

- [ ] **Step 3: 最小实现**

新增 `KolSelectionDetailSnapshot`：

```text
id, selection_set_id, platform, kol_uid,
rank, ranking_interaction,
scope_status_json, facts_json, trend_points_json,
created_at, updated_at
UNIQUE(selection_set_id, platform, kol_uid)
INDEX(selection_set_id, rank)
```

`scope_status_json` 固定包含 `fansAudience`、`postSummaryStatistics`、`accountTrend` 的 `pending|succeeded|failed|skipped`；`facts_json` 只保存白名单事实；`trend_points_json` 只保存 `{week_start, average_interactions, post_count?}`。迁移不可修改历史迁移。

- [ ] **Step 4: 验证绿灯**

Run: `cd backend && .venv/bin/pytest tests/selection/test_detail_snapshots.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/migrations/versions/0024_kol_selection_detail_snapshots.py backend/app/selection/models.py backend/app/selection/detail_snapshots.py backend/tests/selection/test_detail_snapshots.py
git commit -m "feat: 保存达人详情趋势快照"
```

### Task 2: 规范化三类 kol.detail 事实与周趋势

**Files:**
- Modify: `backend/app/selection/normalizers.py`
- Modify: `backend/app/selection/schemas.py`
- Create: `backend/tests/selection/test_kol_detail_normalization.py`

- [ ] **Step 1: 写失败测试**

分别构造 `fansAudience`、`postSummaryStatistics`、`accountTrend` 响应，断言：

```python
# fansAudience -> 年龄、地区、兴趣白名单字段
# postSummaryStatistics -> 作品数、平均互动、平均阅读/播放、互动/粉丝比所需事实
# accountTrend -> 最近 30 天按自然周聚合的最多四个 trend points
# 缺日期、非数值、未来数据和空列表不会形成伪造趋势点
```

- [ ] **Step 2: 验证红灯**

Run: `cd backend && .venv/bin/pytest tests/selection/test_kol_detail_normalization.py -q`

Expected: FAIL，现有适配器只把 detail 当普通候选合并，不区分 scope 或趋势序列。

- [ ] **Step 3: 最小实现**

新增 scope-aware 的纯函数，输入为 `internal_tool_name`、调用 arguments 与 `structured_content`，输出受控 `KolDetailFacts`。只接受 `kol.detail`、合法平台和三种白名单 scope；趋势以 ISO 周一作为 `week_start`，按原始时间升序输出。不得把 URL、接口字段或 MCP 原始响应写入快照。

- [ ] **Step 4: 验证绿灯**

Run: `cd backend && .venv/bin/pytest tests/selection/test_kol_detail_normalization.py tests/selection/test_service.py -q`

Expected: PASS，原有名单归一化不回归。

- [ ] **Step 5: 提交**

```bash
git add backend/app/selection/normalizers.py backend/app/selection/schemas.py backend/tests/selection/test_kol_detail_normalization.py
git commit -m "feat: 规范化达人详情与互动趋势"
```

### Task 3: 在 KOL 任务收尾编排 Top20 的批量详情调用

**Files:**
- Create: `backend/app/selection/top10_enrichment.py`
- Modify: `backend/app/tasks/executor.py`
- Modify: `backend/app/tasks/dependencies.py`
- Modify: `backend/app/selection/service.py`
- Create: `backend/tests/selection/test_top10_enrichment.py`
- Modify: `backend/tests/tasks/test_executor.py`

- [ ] **Step 1: 写失败测试**

使用 fake `McpGateway` 与 12 个跨平台候选，断言：

```python
# 全局按 ranking_interaction 降序、platform+kol_uid 去重，仅选 20 位；
# 同平台达人组成 kwUidList，scope 一次传 fansAudience、postSummaryStatistics、accountTrend；
# 调用数等于覆盖平台数，参数带 platform、kwUidList、scope；
# 恢复执行时已 succeeded 的 snapshot scope 不重复调用；
# 单 scope 失败只标 failed 并继续其他平台；累计积分到 300 或余额不足后 pending scope 为 skipped；
# 自动 KOL 报告在补全器结算后才启动。
```

- [ ] **Step 2: 验证红灯**

Run: `cd backend && .venv/bin/pytest tests/selection/test_top10_enrichment.py tests/tasks/test_executor.py -q`

Expected: FAIL，当前执行器只运行模型决定的工具调用，收尾时不补详情。

- [ ] **Step 3: 最小实现**

`Top20KolDetailEnricher` 从本 Goal 的 `KolSelectionSet` 读取 items：

1. 从搜索/详情已保存的近 30 天平均互动字段取得 `ranking_interaction`；无法取得者排除。
2. 全平台排序并截取二十位，为每位创建或复用详情快照，再按平台分组。
3. 以现有 `McpGateway.execute_batch` 和任务已有 `goal_id`/计划步骤命名空间执行平台批量调用，`kwUidList` 传该平台 Top20 达人、`scope` 传三个固定 scope，复用预留、结算、SSE 工具事件及恢复语义。
4. settled 输出经 Task 2 的纯函数写入快照；失败和余额不足更新 scope 状态，不伪造数据。

在 `TaskExecutor` 的 `kol_selection` 成功收尾路径调用 enrichment，且必须在 `_TaskArtifacts.auto_kol_analysis` 前完成。所有步骤 id 使用 `top20_{platform}`，保证任务恢复可定位幂等调用；若供应商不接受多 scope 批量，降级为 `top20_{platform}_{scope}`。

- [ ] **Step 4: 验证绿灯**

Run: `cd backend && .venv/bin/pytest tests/selection/test_top10_enrichment.py tests/tasks/test_executor.py tests/tasks/test_goal_lifecycle.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/selection/top10_enrichment.py backend/app/selection/service.py backend/app/tasks/executor.py backend/app/tasks/dependencies.py backend/tests/selection/test_top10_enrichment.py backend/tests/tasks/test_executor.py
git commit -m "feat: 自动补全 Top20 达人详情"
```

### Task 4: 提供版本化 Top10 趋势读取 API

**Files:**
- Modify: `backend/app/selection/schemas.py`
- Modify: `backend/app/selection/service.py`
- Modify: `backend/app/selection/router.py`
- Create: `backend/tests/selection/test_top10_trend_api.py`

- [ ] **Step 1: 写失败 API 测试**

覆盖：

```python
# GET /sessions/{id}/kol-top10-trend?set_id= 返回指定 set 的 rank 1–10 及四周点；
# 缺省读取最新 set；历史 set 不串数据；
# 非所属用户 404；空名单返回 {items: []}；
# scope failed 的达人有 status 但 trend_points 为空。
```

- [ ] **Step 2: 验证红灯**

Run: `cd backend && .venv/bin/pytest tests/selection/test_top10_trend_api.py -q`

Expected: FAIL，路由与 DTO 尚不存在。

- [ ] **Step 3: 最小实现**

新增只读 DTO：

```json
{
  "set_id": "…",
  "items": [{"rank": 1, "platform": "douyin", "kol_uid": "…", "nickname": "…", "followers": 0,
    "ranking_interaction": 0, "facts": {"effective_follower_rate": null, "quoted_price_cny": null, "active": null},
    "scope_status": {}, "trend_points": []}]
}
```

服务层通过 `resolve_selection_set` 做用户和版本校验；快照与 selection item 联结读取昵称/粉丝数，按 rank 返回。

- [ ] **Step 4: 验证绿灯**

Run: `cd backend && .venv/bin/pytest tests/selection/test_top10_trend_api.py tests/selection/test_selection_sets.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/selection/schemas.py backend/app/selection/service.py backend/app/selection/router.py backend/tests/selection/test_top10_trend_api.py
git commit -m "feat: 提供 Top10 达人趋势读取接口"
```

### Task 5: 在 KOL 分析页渲染十条趋势折线

**Files:**
- Modify: `src/api/contracts.ts`
- Modify: `src/api/kolSelection.ts`
- Modify: `src/api/kolSelection.test.ts`
- Create: `src/components/KolTop10TrendChart.tsx`
- Create: `src/components/KolTop10TrendChart.test.tsx`
- Modify: `src/components/UniversalReport.tsx`
- Modify: `src/components/UniversalReport.test.tsx`

- [ ] **Step 1: 写失败组件/API 测试**

覆盖：

```tsx
// KOL 分析 Tab 激活时按当前 selectedSetId 请求趋势；
// 返回补全 Top20 中的前 10 个达人时渲染 10 个 Recharts Line 与平台标记；
// legend 点击隐藏/显示指定达人；tooltip 含周均互动、环比、粉丝、有效粉丝率、报价、活跃状态；
// 趋势缺失显示“趋势数据待补充”，不渲染虚假零线；
// 切换名单版本后使用对应 set_id 的图表数据。
```

- [ ] **Step 2: 验证红灯**

Run: `npm run test -- src/api/kolSelection.test.ts src/components/KolTop10TrendChart.test.tsx src/components/UniversalReport.test.tsx`

Expected: FAIL，API 类型、请求和图表组件尚不存在。

- [ ] **Step 3: 最小实现**

新增 `getKolTop10Trend(sessionId, setId?)`；在 `KolPanel` 的 `report` 子 Tab 中，报告块上方加载并渲染 `KolTop10TrendChart`。用 `ResponsiveContainer + LineChart + Line + Legend + Tooltip` 实现，稳定颜色按 `platform:kol_uid` hash 分配；数据按四周 `week_start` 合并为一行，缺失点保持 `null`。窄屏图表容器设置最小宽度并允许横向滚动。

- [ ] **Step 4: 验证绿灯**

Run: `npm run test -- src/api/kolSelection.test.ts src/components/KolTop10TrendChart.test.tsx src/components/UniversalReport.test.tsx`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/api/contracts.ts src/api/kolSelection.ts src/api/kolSelection.test.ts src/components/KolTop10TrendChart.tsx src/components/KolTop10TrendChart.test.tsx src/components/UniversalReport.tsx src/components/UniversalReport.test.tsx
git commit -m "feat: 展示 Top10 达人互动趋势"
```

### Task 6: 端到端验证、变更日志与人工真实 MCP 验证

**Files:**
- Modify: `changelog/2026-07-29.md`
- Modify: `e2e/` 下与 KOL 分析相关的既有用例，或 Create: `e2e/kol-top10-trend.spec.ts`

- [ ] **Step 1: 写失败 E2E / 集成测试**

模拟跨平台 24 位候选与按平台批量详情结果，断言 Top20 详情快照、Top10 图例、四周折线和历史 set 切换；余额不足模拟断言部分补全警告。

- [ ] **Step 2: 验证红灯**

Run: `npm run test:e2e -- e2e/kol-top10-trend.spec.ts`

Expected: FAIL，功能尚未接线。

- [ ] **Step 3: 最小集成与文档**

补齐 task 事件状态文案（“正在补全 Top20 达人详情”）、changelog 中的 300 积分预算上限与实际调用结算规则、部分失败与历史版本行为；不记录任何真实供应商响应或凭证。

- [ ] **Step 4: 全量验证**

Run:

```bash
cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q
cd .. && npm run test && npm run lint && npm run build
```

Expected: 全部通过；真实 MCP 验证仅在用户明确授权、余额足够且使用真实 `.env` 时执行，需记录调用次数与总积分、不得输出原始响应。

- [ ] **Step 5: 提交**

```bash
git add changelog/2026-07-29.md e2e backend src
git commit -m "docs: 记录 Top10 达人趋势能力"
```
