# 思考滑动窗口 + BI 体验修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 思考超限改滑动窗口（单块保尾、turn 折叠最旧块、snapshot 节流）；report.updated 事件带 report_type 修复 KOL/品牌串台；BI 报告面板动画加载态；圈选达人按互动率 Top20。

**Architecture:** 后端 sanitizer 保尾截断 + service turn 级折叠；report.updated 两发射点 payload 加 report_type；前端 reducer 过滤 + useWorkspace 双拉取点防线；TypedReportPanel 加 detailLoading 动画态；KolPanel 排序截断。

**Tech Stack:** FastAPI（后端），React + TypeScript + Vitest（前端）。

**Spec:** `docs/superpowers/specs/2026-07-27-thinking-sliding-window-and-bi-ux-design.md`

**验证命令（每个 Task 的 Commit 前必跑）：**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/thinking tests/tasks -q
# 前端（仓库根目录）
npm run test && npm run lint
```

---

### Task 1: 思考滑动窗口（sanitizer 保尾 + turn 折叠 + 节流）

**Files:**
- Modify: `backend/app/thinking/sanitizer.py`（`_limit_length` 保尾 + 前缀标记）
- Modify: `backend/app/thinking/service.py`（`_delta` 节流、`_fit_turn_budget` 改折叠入口）
- Test: `backend/tests/thinking/test_sanitizer.py`、`backend/tests/thinking/test_service.py`（按新语义更新 + 新增）

**实现要点（spec 评审已确认的取舍）：**

- `sanitize_thinking` 的 `_limit_length`：从「保头 + 后缀『思考内容过长，已截断』」改为
  「保尾 + 前缀标记 `…（早期内容已折叠）`」，标记长度计入 max_chars。
- `_delta`：保尾后 public_text 不再前缀递增，走既有 snapshot 路径；**节流**——块已
  truncated 时，仅当距上次发布的 raw 增长 ≥ 1000 字符才发新 snapshot（state 仍每次
  更新，终态内容是最新的；不发期间客户端看到略旧内容，可接受）。这是有意的带宽
  取舍（否则每个 chunk 一次 ~12k 全量 snapshot）。
- `_fit_turn_budget` 改为折叠入口：预算不足时按完成顺序折叠最旧已完成块
  （content 替换为 `「早期思考已折叠」`（9 字符），置 truncated=True，幂等——
  已是占位符的跳过），直到剩余额度足够。折叠只改 content，不改
  (operation_id, attempt)，`mark_blocks_persisted` 游标不受影响；落库为折叠后内容。
- **不向在线客户端补发折叠事件**（live 客户端已收全文，刷新后以落库折叠态为准，
  属确认行为）。
- 空块防御：折叠后预算必足（单块 12k < turn 30k），但防御性地 content 为空且
  truncated 时写占位文案而非空串。

- [ ] **Step 1: 写失败测试**

```python
# sanitizer：超限保尾部 + 前缀标记，长度 ≤ max_chars，truncated=True。
# service：
# 1. 单块超 12k：delta snapshot 内容为尾部、带前缀标记；truncated 后小额增长
#    （<1000）不发新事件，大额增长发 snapshot。
# 2. turn 超 30k：完成 3 块各 12k 后第 4 块完成——最旧块 content 为「早期思考已折叠」、
#    最新块完整、总长度有界；重复终态折叠幂等。
# 3. 既有「保头+后缀」断言的用例按新语义更新（不得删测试）。
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/thinking -q`
Expected: FAIL（保尾/折叠未实现）

- [ ] **Step 3: 实现**（按上述要点；`_completed` 折叠用 `dataclasses.replace` 生成新块替换列表元素）

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/thinking tests/brainstorm tests/tasks -q`
Expected: PASS（既有用例的保头断言全部改为保尾语义）

- [ ] **Step 5: Commit**

```bash
git add backend/app/thinking/ backend/tests/thinking/
git commit -m "feat: 思考内容超限改滑动窗口（保尾 + turn 折叠 + snapshot 节流）"
```

---

### Task 2: report.updated payload 增加 report_type

**Files:**
- Modify: `backend/app/tasks/dependencies.py:266-276`（auto_kol_analysis）与 `:486-496`（goal 报告）
- Test: `backend/tests/tasks/`（就近，找 report.updated 断言的既有用例补充 report_type）

- [ ] **Step 1: 写失败测试**

```python
# auto_kol_analysis 事件 payload["report_type"] == "kol_analysis"
# brand/campaign goal 报告事件 payload["report_type"] == goal 对应类型（brand_analysis/campaign_analysis）
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL（payload 无 report_type 键）

- [ ] **Step 3: 实现**：两处 payload dict 各加一行 `"report_type": ...`（goal 路径从 `_ANALYSIS_GOAL_TABLE` 或 goal.goal_type 取）。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/tasks -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/dependencies.py backend/tests/tasks/
git commit -m "feat: report.updated 事件 payload 增加 report_type"
```

---

### Task 3: 前端串台防线（reducer 过滤 + useWorkspace 双拉取点校验）

**Files:**
- Modify: `src/state/taskEvents.ts:293-303`（report.updated 分支）
- Modify: `src/hooks/useWorkspace.ts`（hydrateAnalysis :109-126 与 visibleAnalysisReportId effect :609-624 两处）
- Test: `src/state/taskEvents.test.ts`、`src/hooks/useWorkspace.test.tsx`

- [ ] **Step 1: 写失败测试**

```ts
// reducer：report.updated 且 payload.report_type='brand_analysis' → 不设 visibleAnalysisReportId，
//   但 phase/phaseLabel/activity 仍更新；report_type='kol_analysis' → 设置；无 report_type（历史事件）→ 设置（过渡近似）。
// useWorkspace：visibleAnalysisReportId 指向 brand_analysis 报告（getAnalysisReport mock 返回
//   report_type='brand_analysis'）→ analysisReport 不挂载。
```

注意 payload key 用既有 helper `valueOf(event.payload, 'reportType', 'report_type')`。

- [ ] **Step 2: 跑测试确认失败**

Run: `npm run test -- src/state/taskEvents.test.ts src/hooks/useWorkspace.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现**

`taskEvents.ts` report.updated 分支：先取 reportId 与 reportType；`reportType === undefined || reportType === 'kol_analysis'` 才设 `visibleAnalysisReportId`，phase/label/activity 更新不受影响。

`useWorkspace.ts` 两处拉取详情后：`analysisReportResponse.report_type !== 'kol_analysis'`（字段 optional，缺省视为通过——后端 AnalysisReportRead 有 server_default kol_analysis）时不挂载（置 undefined）。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `npm run test && npm run lint`
Expected: PASS、tsc 退出 0

- [ ] **Step 5: Commit**

```bash
git add src/state/taskEvents.ts src/state/taskEvents.test.ts src/hooks/useWorkspace.ts src/hooks/useWorkspace.test.tsx
git commit -m "fix: KOL 分析子 Tab 不再误挂品牌/活动报告"
```

---

### Task 4: TypedReportPanel 加载动态 + KolPanel Top20

**Files:**
- Modify: `src/components/UniversalReport.tsx`（`TypedReportPanel` :454-553、`KolPanel` 名单渲染 :813-870）
- Test: `src/components/UniversalReport.test.tsx`（追加）

- [ ] **Step 1: 写失败测试**

```tsx
// TypedReportPanel：
// 1. 版本列表加载中 → 动画加载态（role=status，含 Loader2 旋转图标与分阶段文案 hook 的首段文案）；
// 2. 列表已到、详情拉取中 → 仍是加载态而非 emptyText；详情到达 → 渲染报告；
// 3. 详情 fetch 失败 → detailLoading 复位（不永久卡加载态）。
// KolPanel：
// 4. >20 条名单 → 只渲染 20 条、按 engagement_rate 倒序、null 在最后、摘要行
//    「共 N 位达人，按互动率展示 Top 20」（N=selectionItems.length）；
// 5. ≤20 条 → 全部渲染、无摘要行；子 Tab 标签计数（selectedCount）不变。
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npm run test -- src/components/UniversalReport.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现**

`TypedReportPanel`：
- 新 state `detailLoading`；详情 effect：`setDetailLoading(true)` → then/catch 都复位
  （`.catch(() => { if (!cancelled) setDetailLoading(false); })`，失败置 report undefined）。
- `useLoadingMessage(loading || detailLoading)` + `Loader2` 渲染加载态
  （版本列表与详情任一在途即显示；切版本也闪加载态，属确认行为）。

`KolPanel`：`selectionItems` 渲染前计算 `topItems`——按 `selectionMetric(item, 'engagement_rate')`
倒序（null/非数值排最后，稳定排序）取前 20；`selectionItems.length > 20` 时列表顶部渲染
摘要行「共 {selectionItems.length} 位达人，按互动率展示 Top 20」。收藏 toggle/导出不改。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `npm run test && npm run lint`
Expected: PASS、tsc 退出 0

- [ ] **Step 5: Commit**

```bash
git add src/components/UniversalReport.tsx src/components/UniversalReport.test.tsx
git commit -m "feat: 报告面板动画加载态与圈选达人按互动率 Top20"
```

---

### Task 5: 全量验证 + changelog

- [ ] **Step 1: 全量验证**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest -q
cd .. && npm run test && npm run lint && npm run build
```

- [ ] **Step 2: changelog 并提交**

`changelog/2026-07-27.md` 追加「思考滑动窗口 + BI 体验修复」：四项改动（滑动窗口/串台/
加载态/Top20）、取舍声明（snapshot 节流、不补发折叠事件、历史事件缺 report_type 的
过渡近似、恢复后早期思考折叠）、验证结果、遗留事项。

```bash
git add -f changelog/2026-07-27.md
git commit -m "docs: 记录思考滑动窗口与 BI 体验修复"
```

---

## 备注

- 实现中如行号/字段名与计划不一致，以源码为准；不得改无关逻辑。
- Task 1 的既有测试更新是本次唯一允许改断言方向的地方（保头 → 保尾），其余测试不得改弱。
