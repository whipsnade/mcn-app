# 品牌与 KOL 动态分析 Implementation Plan

> 本文的早期任务清单已由模型主导联合分析方案修订：新任务必须至少包含 KOL
> 证据，品牌指标问题使用 `hybrid`；请以
> `docs/superpowers/specs/2026-07-17-model-led-mcp-planning-design.md` 和当前代码为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让品牌、KOL 和联合问题根据用户输入选择真实 DataTap MCP 工具，并生成不依赖候选清单的品牌 BI 或联合 BI。

**Architecture:** 在现有模块化单体中增加确定性的范围路由提示和证据类型字段；DeepSeek 从完整的已审核目录中规划调用，后端校验覆盖范围。MCP 成功响应先按 KOL/品牌分别归一化，ReportingService 根据范围生成候选版本、品牌分析或二者联合的 `BiReport`，不新增跨服务队列或微服务。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy/MySQL、Pydantic、现有 Tencent Plan DeepSeek-V4-Pro 适配器、React/TypeScript、Vitest。

## Global Constraints

- 不实现外部动态工具检索；每轮规划都把当前用户可用的全部已审核工具放入 PlannerContext。
- 未选择渠道使用用户已授权全部平台，明确选择时只使用所选平台。
- MCP 每个成功工具响应仍计 10 积分，失败不计费；最大 10 次调用。
- 任何数字只能来自成功且通过 Schema 校验的真实 DataTap 响应；无数据使用 `available=false`，不填零值。
- 诊断只保存字段名、类型、长度和 Schema 路径；不保存密钥、接口地址或未投影的达人原始数据。

### Task 1: 增加分析范围与证据类型契约

**Files:**
- Modify: `backend/app/orchestration/schemas.py`
- Modify: `backend/app/orchestration/context.py`
- Create: `backend/app/orchestration/routing.py`
- Test: `backend/tests/orchestration/test_routing.py`
- Test: `backend/tests/orchestration/test_context.py`

**Interfaces:**
- `routing.classify_analysis_request(text, brief) -> AnalysisRouting`
- `AnalysisRouting.scope: Literal["brand", "kol", "hybrid"]`
- `AnalysisRouting.objectives: tuple[str, ...]`
- `PlannerContext.analysis_scope`, `analysis_objectives`, `requested_period`
- `ToolPlanStep.evidence_kind: Literal["brand", "kol"]`

- [ ] **Step 1: Write the failing tests**

```python
def test_brand_volume_question_routes_to_brand() -> None:
    result = classify_analysis_request("分析科颜氏最近3个月在各平台的声量变化和用户情感趋势", _brief("科颜氏"))
    assert result.scope == "brand"
    assert set(result.objectives) >= {"volume_trend", "sentiment_trend"}

def test_brand_question_that_requests_active_creators_routes_to_hybrid() -> None:
    result = classify_analysis_request("分析科颜氏声量并找出相关活跃达人", _brief("科颜氏"))
    assert result.scope == "hybrid"

def test_plain_creator_query_routes_to_kol() -> None:
    result = classify_analysis_request("找最近30天活跃top10达人", _brief("科颜氏"))
    assert result.scope == "kol"

def test_tool_plan_step_requires_evidence_kind() -> None:
    with pytest.raises(ValidationError):
        ToolPlanStep.model_validate({"id": "step_1", "internal_tool_name": "x", "arguments": {}, "evidence_goal": "x"})
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/orchestration/test_routing.py tests/orchestration/test_context.py -q`

Expected: FAIL because routing types and `evidence_kind` do not exist.

- [ ] **Step 3: Implement the minimal routing and context projection**

Use normalized Chinese text and explicit keyword groups. `brand` keywords include `声量、舆情、情感、趋势、热词、品牌提及、曝光`; `kol` keywords include `达人、KOL、网红、粉丝、候选、活跃达人`; brand questions default to `hybrid` so brand trends also return active KOL context, while explicit “仅分析品牌/不需要达人” remains `brand`; both groups produce `hybrid`. Extract `最近N天/月/季度` into a safe `requested_period` object with `start` and `end` ISO dates; use current date only for period calculation, never for metrics. Extend `PlannerContext` and set `evidence_kind` as a required plan field.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run the same command; expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/orchestration/schemas.py backend/app/orchestration/context.py backend/app/orchestration/routing.py backend/tests/orchestration/test_routing.py backend/tests/orchestration/test_context.py
git commit -m "feat: add brand kol analysis routing contract"
```

### Task 2: 扩展真实 DataTap 已审核工具目录

**Files:**
- Modify: `backend/app/mcp_gateway/registry.py`
- Modify: `backend/app/tasks/dependencies.py`
- Modify: `backend/app/mcp_gateway/approved_tools.json`
- Test: `backend/tests/mcp_gateway/test_registry.py`
- Test: `backend/tests/tasks/test_dependencies.py`

**Interfaces:**
- `ToolRegistryService.refresh_service` refreshes all enabled DataTap services.
- Allowlisted real remote tools include `match_best_tag`, `query_analysis_data`, `social_statistic_trend`, `social_statistic_user_profile`, `social_statistic_hot_user`, `social_statistic_overview`, `kol_match_mentions_tag`, `kol_detail`, and the five `kol_*_search` tools for `xiaohongshu`, `douyin`, `bilibili`, `weibo`, `wechat`.
- `PlannerTool.description` exposes reviewed capability descriptions and exact input/output schemas.

- [ ] **Step 1: Write failing registry tests**

```python
async def test_refresh_approves_allowlisted_brand_and_all_channel_kol_tools(db_session, transport):
    report = await ToolRegistryService(db_session, transport).refresh_service(DataTapService.INSIGHT_CUBE)
    enabled = await ToolRegistryService(db_session, transport).list_enabled()
    assert "query_analysis_data" in {item.remote_name for item in enabled}

async def test_refresh_quarantines_unknown_remote_tool(db_session, transport):
    await ToolRegistryService(db_session, transport).refresh_service(DataTapService.INSIGHT_CUBE)
    assert "unknown_tool" in report.quarantined_remote_names
```

- [ ] **Step 2: Run tests to verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/mcp_gateway/test_registry.py tests/tasks/test_dependencies.py -q`

Expected: FAIL because only the two legacy KOL tools are in the manifest and startup refreshes only `social-grow-mcp`.

- [ ] **Step 3: Add the real allowlist and startup refresh**

Use the live DataTap tool names above, retain the current discovery digest/schema quarantine behavior, and add manifest entries from the provider’s current schemas. Keep `aktools-mcp` out of this product route because it is unrelated to brand/KOL analysis. Refresh `INSIGHT_CUBE`, `SOCIAL_GROW`, and `BILIBILI` at startup; refresh `SOCIAL_GROW_CONTENT` only when its approved entries are present. Unknown tools remain quarantined.

- [ ] **Step 4: Run registry tests and provider-schema validation**

Run: `cd backend && .venv/bin/python -m pytest tests/mcp_gateway/test_registry.py tests/mcp_gateway/test_transport_policy.py tests/tasks/test_dependencies.py -q`

Expected: PASS and no provider URL/token appears in test output.

- [ ] **Step 5: Commit**

```bash
git add backend/app/mcp_gateway/approved_tools.json backend/app/mcp_gateway/registry.py backend/app/tasks/dependencies.py backend/tests/mcp_gateway/test_registry.py backend/tests/tasks/test_dependencies.py
git commit -m "feat: approve real brand and all-channel datatap tools"
```

### Task 3: 让 DeepSeek 按范围规划真实工具

**Files:**
- Modify: `backend/app/model/prompts.py`
- Modify: `backend/app/orchestration/planner.py`
- Modify: `backend/app/orchestration/analytics_contract.py`
- Test: `backend/tests/orchestration/test_planner.py`
- Test: `backend/tests/model/test_prompts.py`

**Interfaces:**
- `Planner._validate` rejects a plan that does not cover required evidence kinds.
- Brand plan uses `social_statistic_trend` or `query_analysis_data`; hybrid plan contains at least one `brand` and one `kol` step.
- KOL plans include one search step per effective channel when that channel has an approved search tool.

- [ ] **Step 1: Write failing planner tests**

```python
async def test_brand_plan_keeps_brand_tools_and_does_not_inject_kol_search(context, model):
    model.value = ToolPlan(objective="brand", steps=(ToolPlanStep(
        id="step_1", internal_tool_name="datatap.insight.social.statistic.trend.v1",
        arguments={"target_type": "tag", "name": "科颜氏", "tag_type": "品牌标签", "datasource": ["小红书", "抖音", "微博"], "start_time": "2026-04-17", "end_time": "2026-07-17", "dimension": "date"},
        evidence_kind="brand", evidence_goal="品牌声量趋势与情感趋势"),))
    plan = await Planner(model=model).plan(context_for("brand"))
    assert all(step.evidence_kind == "brand" for step in plan.steps)

async def test_hybrid_plan_requires_both_evidence_kinds(context, model):
    model.value = kol_only_plan()
    with pytest.raises(PlanValidationError, match="EVIDENCE_SCOPE_NOT_COVERED"):
        await Planner(model=model).plan(context_for("hybrid"))
```

- [ ] **Step 2: Run focused tests to verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/orchestration/test_planner.py tests/model/test_prompts.py -q`

Expected: FAIL because the prompt does not describe scope/evidence rules and planner validation does not check them.

- [ ] **Step 3: Update prompt, deterministic defaults, and validation**

Add scope/objective instructions, capability descriptions, period/channel mapping, and explicit “brand 不得仅调用 KOL 搜索” rule to `PLANNER_SYSTEM_TEXT` and `REPLANNER_SYSTEM_TEXT`. Map real remote names to stable internal names. Keep all tools in the JSON user message; do not add provider-native function calling. `_compile_supported_search_defaults` should only add missing KOL channel searches for `kol`/`hybrid`, and should not inject them for `brand`. Validate `evidence_kind`, tool capability, channel support, maximum ten steps, and required scope coverage.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run the same command; expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/model/prompts.py backend/app/orchestration/planner.py backend/app/orchestration/analytics_contract.py backend/tests/orchestration/test_planner.py backend/tests/model/test_prompts.py
git commit -m "feat: route planner by brand kol evidence scope"
```

### Task 4: 增加品牌证据归一化和确定性 BI 聚合

**Files:**
- Modify: `backend/app/reporting/schemas.py`
- Modify: `backend/app/reporting/normalizers.py`
- Modify: `backend/app/reporting/analytics.py`
- Create: `backend/app/reporting/brand_analytics.py`
- Test: `backend/tests/reporting/test_brand_normalizers.py`
- Test: `backend/tests/reporting/test_brand_analytics.py`

**Interfaces:**
- `NormalizedBrandEvidence` stores only `platform`, `period`, `brand_mentions`, `exposure`, `interactions`, `sentiment_counts`, `hot_words`, `audience_*`, `evidence_references`.
- `normalize_brand_evidence(evidence) -> tuple[NormalizedBrandEvidence, ...]`.
- `aggregate_brand_analytics(records) -> dict` returns the existing BI analytics DTO shape plus `data_availability` and warnings.

- [ ] **Step 1: Write failing normalizer/aggregation tests**

```python
def test_brand_tool_result_projects_volume_sentiment_and_platform() -> None:
    evidence = ToolEvidence("datatap.insight.query.analysis.v1", {"result": json.dumps({"data": [{"平台": "小红书", "月份": "2026-06", "声量": 12, "情感指数": 0.8}]}, ensure_ascii=False)}, "call-1", NOW)
    rows = normalize_brand_evidence((evidence,))
    assert rows[0].platform == "xiaohongshu"
    assert rows[0].analytics_fields["brand_mentions"] == 12

def test_empty_brand_result_is_available_false_not_zero() -> None:
    result = aggregate_brand_analytics(())
    assert result["overview"]["brand_volume"]["available"] is False
    assert result["overview"]["brand_volume"]["value"] is None
```

- [ ] **Step 2: Run tests to verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/reporting/test_brand_normalizers.py tests/reporting/test_brand_analytics.py -q`

Expected: FAIL because brand evidence types and adapters do not exist.

- [ ] **Step 3: Implement safe projection and aggregation**

Parse provider-wrapped `result` strings and accept only documented aliases for date, platform, volume, exposure, interaction, sentiment and distribution fields. For `query_analysis_data` and `social_statistic_trend`, flatten date/platform rows; for user-profile and hot-user outputs, project audience/KOL-safe fields. Unknown shapes return an empty row with a warning, never the raw payload. Reuse `aggregate_analytics` where the normalized fields match, and add explicit `data_availability` with source tool names and coverage.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/reporting/test_brand_normalizers.py tests/reporting/test_brand_analytics.py tests/reporting/test_analytics.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/reporting/schemas.py backend/app/reporting/normalizers.py backend/app/reporting/analytics.py backend/app/reporting/brand_analytics.py backend/tests/reporting/test_brand_normalizers.py backend/tests/reporting/test_brand_analytics.py
git commit -m "feat: normalize and aggregate brand datatap evidence"
```

### Task 5: 让 ReportingService 支持 brand/kol/hybrid 和空结果

**Files:**
- Modify: `backend/app/reporting/service.py`
- Modify: `backend/app/tasks/dependencies.py`
- Modify: `backend/app/tasks/executor.py`
- Modify: `backend/app/tasks/repository.py`
- Test: `backend/tests/reporting/test_service_brand_report.py`
- Test: `backend/tests/tasks/test_executor.py`

**Interfaces:**
- `ReportingService.build_candidate_version` returns an empty, repeatable version for brand-only tasks and continues normal KOL ranking for KOL/hybrid tasks.
- `ReportingService.build_bi_report` accepts a task with `candidate_version=0` and creates brand-only BI from settled evidence.
- `chart_data_json` contains `analysis_scope`, `brand_analytics`, `kol_analytics`, `data_availability`, `warnings`.

- [ ] **Step 1: Write failing service/executor tests**

```python
async def test_brand_task_with_successful_empty_result_completes_with_report(db_session):
    task = make_task(scope="brand")
    add_settled_call(task, tool="datatap.insight.social.statistic.trend.v1", result={"data": []})
    report = await ReportingService(db_session).build_bi_report(task.id, lease_owner=WORKER)
    assert report.chart_data_json["analysis_scope"] == "brand"
    assert report.chart_data_json["brand_analytics"]["overview"]["brand_volume"]["available"] is False

async def test_hybrid_task_keeps_brand_report_when_kol_result_is_empty(db_session):
    report = await run_hybrid_with_empty_kol_and_brand_evidence(db_session)
    assert report.chart_data_json["brand_analytics"]["overview"]["brand_volume"]["available"] is True
    assert report.chart_data_json["kol_analytics"]["candidate_count"] == 0
```

- [ ] **Step 2: Run tests to verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/reporting/test_service_brand_report.py tests/tasks/test_executor.py -q`

Expected: FAIL with `candidate_version_not_found` or `no_successful_tool_evidence` for brand-only/empty-KOL tasks.

- [ ] **Step 3: Implement range-aware artifact generation**

Read `analysis_scope` from `plan_json` (legacy plans default to `kol`). Partition `_successful_evidence` by `ToolPlanStep.evidence_kind`. Build KOL candidates only when scope requires KOL and keep zero candidate versions valid. Build brand analytics directly from settled evidence; do not add synthetic records. If all calls succeeded but normalized data is empty, emit a warning and allow report creation. Update terminal warning selection so partial MCP failures and empty real results produce `completed_with_warnings` and a Chinese assistant error/warning message.

- [ ] **Step 4: Run focused and regression tests to verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/reporting/test_service_brand_report.py tests/tasks/test_executor.py tests/tasks/test_repository.py tests/reporting/test_analytics.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/reporting/service.py backend/app/tasks/dependencies.py backend/app/tasks/executor.py backend/app/tasks/repository.py backend/tests/reporting/test_service_brand_report.py backend/tests/tasks/test_executor.py
git commit -m "feat: generate brand and hybrid reports without kol-only gate"
```

### Task 6: 扩展 API 与前端 BI 数据展示

**Files:**
- Modify: `backend/app/reporting/schemas.py`
- Modify: `backend/app/reporting/router.py`
- Modify: `src/api/contracts.ts`
- Modify: `src/components/BiReport.tsx`
- Modify: `src/components/BiAnalytics.tsx`
- Modify: `src/state/taskEvents.ts`
- Test: `backend/tests/reporting/test_router.py`
- Test: `src/components/BiReport.test.tsx`
- Test: `src/components/BiAnalytics.test.tsx`
- Test: `src/state/taskEvents.test.ts`

**Interfaces:**
- `BiReportRead.analysis_scope: "brand" | "kol" | "hybrid"`.
- `BiReportRead.brand_analytics`, `kol_analytics`, `data_availability`, `warnings` are always present (empty DTOs for legacy reports).
- `ApiBiReport` mirrors the new fields; old `analytics` remains for compatibility.

- [ ] **Step 1: Write failing API/UI tests**

```typescript
it('shows brand analytics when a brand report has no candidates', async () => {
  render(<BiReport report={brandReportFixture()} taskStatus="completed_with_warnings" />);
  expect(screen.getByText('品牌声量趋势')).toBeInTheDocument();
  expect(screen.getByText('暂无真实 MCP 数据')).toBeInTheDocument();
});
```

- [ ] **Step 2: Run tests to verify RED**

Run: `npm run test -- src/components/BiReport.test.tsx src/components/BiAnalytics.test.tsx src/state/taskEvents.test.ts`

Expected: FAIL because DTOs/UI do not distinguish brand/hybrid scope.

- [ ] **Step 3: Implement compatible API/UI**

Return scope and availability fields from `bi_report_read`; keep the existing report cards and tabs, adding the brand trend/sentiment labels and a Chinese no-data state without placeholder numbers. Use task event payload `evidence_kind` to label actual brand MCP vs KOL MCP progress. Keep candidate list/export hidden or disabled for pure brand reports while retaining the BI report shell.

- [ ] **Step 4: Run frontend tests and type checks**

Run: `npm run test -- src/components/BiReport.test.tsx src/components/BiAnalytics.test.tsx src/state/taskEvents.test.ts && npm run typecheck && npm run lint && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/reporting/schemas.py backend/app/reporting/router.py src/api/contracts.ts src/components/BiReport.tsx src/components/BiAnalytics.tsx src/state/taskEvents.ts backend/tests/reporting/test_router.py src/components/BiReport.test.tsx src/components/BiAnalytics.test.tsx src/state/taskEvents.test.ts
git commit -m "feat: display brand and hybrid bi reports"
```

### Task 7: 全量验证与真实链路回归

**Files:**
- Modify: `docs/qa/task9-regression.md`
- Create: `docs/qa/brand-kol-dynamic-analysis.md`

- [ ] **Step 1: Run backend non-provider regression**

Run: `cd backend && TENCENT_PLAN_API_KEY=test-only-key .venv/bin/python -m pytest tests -q --ignore=tests/integration/test_real_providers.py`

Expected: all tests pass.

- [ ] **Step 2: Run provider-safe real smoke checks**

Run the existing real-provider test target with the configured runtime, checking only status, selected tool names, evidence counts, and report availability. Never print tokens, URLs, raw MCP payloads or KOL IDs.

- [ ] **Step 3: Run frontend regression**

Run: `npm run test && npm run typecheck && npm run lint && npm run build`

Expected: all pass.

- [ ] **Step 4: Verify the three acceptance prompts**

Verify plans and report scope for:

1. `分析科颜氏最近3个月在各平台的声量变化和用户情感趋势` → brand;
2. `找出最近30天活跃top10的达人` → kol;
3. `分析科颜氏声量并找出相关活跃达人` → hybrid.

Record only scope, tool names, call statuses, warnings, and `available` flags in the QA note.

- [ ] **Step 5: Commit QA evidence**

```bash
git add docs/qa/task9-regression.md docs/qa/brand-kol-dynamic-analysis.md
git commit -m "test: verify brand kol dynamic analysis flow"
```
