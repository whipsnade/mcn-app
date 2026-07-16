# KOL 最新轮次完整数据 Excel 导出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** 在每轮会话分析中按 Excel 字段契约采集完整 KOL 候选数据，并在右侧 BI 报告导出当前会话最新一轮的全部候选 Excel。

**Architecture:** 在现有异步流式模块化单体中增加版本化导出字段契约、任务完整候选池和服务端模板导出器。规划器继续接收全部已审核 MCP 工具，由大模型按契约规划调用；规范化候选池同时驱动 Top10 页面、BI 和 Excel，导出请求只读取已持久化快照。

**Tech Stack:** FastAPI、SQLAlchemy 2 async、Alembic、Pydantic、React、TypeScript、Vite、MySQL、openpyxl；Python Excel 生成器加载模板并保留附件的工作表和格式。

## Global Constraints

- 只导出当前会话最新一轮，不回退到历史轮次，不合并多轮结果。
- Excel 导出该轮完整候选池，排名表新增“平台”字段；页面和 BI 仍展示最终 Top10。
- 每轮大模型规划上下文必须包含版本化 `ExcelExportFieldContract`、本轮筛选条件和全部已审核 MCP 工具定义。
- MCP 成功调用扣 10 积分，失败不扣费；补采重新规划最多一次。
- 缺失数据显示“数据缺失”，不能由模型编造；若评分取中间分必须在理由和方法论中标注。
- Excel 和日志不得包含达人内部 ID、任务/报告/MCP 调用 ID、密钥、接口地址或原始 MCP 响应。
- 右侧 BI 顶部“导出 PDF”改为“导出 Excel”，保留原型按钮样式。
- 使用 TDD：每个行为先添加失败测试，再写最小实现，再运行相关测试和全量回归。

## File Map

- Create `backend/app/orchestration/export_contract.py`: 字段契约版本、字段定义、动态行业/地区/年龄标签。
- Modify `backend/app/orchestration/schemas.py`, `backend/app/orchestration/context.py`, `backend/app/orchestration/planner.py`, `backend/app/model/prompts.py`: 将契约放入规划器上下文并校验 MCP 计划覆盖所选平台。
- Modify `backend/pyproject.toml`: 添加生产依赖 `openpyxl>=3.1,<4`。
- Create `backend/app/reporting/exporter.py`: 从最新候选池生成 4 张工作表并返回 XLSX 字节流。
- Create `backend/app/reporting/templates/KOL匹配度分析报告.xlsx`: 用户提供的模板资产，作为服务端导出样式基线。
- Modify `backend/app/reporting/models.py`, `backend/app/db/models.py`, `backend/app/reporting/service.py`, `backend/app/reporting/normalizers.py`: 保存完整候选池和模板所需规范化字段。
- Create `backend/migrations/versions/0008_candidate_pool_export.py`: 新增候选池表和候选池明细表。
- Modify `backend/app/reporting/router.py`, `backend/app/api/router.py`: 增加当前会话最新轮次 Excel 下载接口。
- Modify `src/api/contracts.ts`, `src/api/tasks.ts`, `src/components/BiReport.tsx`, `src/App.tsx`: 添加下载 API、导出按钮状态和中文错误反馈。
- Create `backend/tests/orchestration/test_export_contract.py`, `backend/tests/reporting/test_candidate_pool.py`, `backend/tests/reporting/test_exporter.py`, `backend/tests/reporting/test_export_route.py`, `src/components/BiReport.test.tsx` cases: 覆盖契约、完整候选、工作簿、权限和按钮行为。

### Task 1: 定义导出字段契约并接入规划器上下文

**Files:**
- Create: `backend/app/orchestration/export_contract.py`
- Modify: `backend/app/orchestration/schemas.py`, `backend/app/orchestration/context.py`, `backend/app/orchestration/planner.py`, `backend/app/model/prompts.py`
- Test: `backend/tests/orchestration/test_export_contract.py`, `backend/tests/model/test_prompts.py`

**Interfaces:**
- Produces `EXPORT_FIELD_CONTRACT_VERSION: str`, `build_export_field_contract(brief: SessionBrief) -> ExportFieldContract`.
- `PlannerContext` gains `export_contract: ExportFieldContract`.
- `ExportFieldContract.required_field_names` is the deterministic list sent in planner JSON.

- [ ] **Step 1: Write the failing tests**

```python
def test_contract_labels_industry_and_platform_fields():
    contract = build_export_field_contract(
        SessionBrief(
            session_id="s1", brand="品牌", campaign_name=None,
            platforms=("xiaohongshu", "douyin"), category="美妆",
            target_audience="20-30女性", budget_min=None, budget_max=None,
            filters={"target_fan_locations": ["浙江", "湖州"]},
        )
    )
    assert contract.version == EXPORT_FIELD_CONTRACT_VERSION
    assert contract.required_field_names[0] == "platform"
    assert "美妆兴趣占比" in contract.required_field_names
    assert "抖音平台口径" in contract.notes

def test_planner_context_serializes_contract_with_all_tools():
    payload = PlannerContext(
        brief=brief, recent_messages=(), existing_results={},
        tools=(tool_a, tool_b), allowed_channels=("xiaohongshu", "douyin"),
        export_contract=contract,
    ).model_dump(mode="json")
    assert payload["export_contract"]["version"] == EXPORT_FIELD_CONTRACT_VERSION
    assert {item["internal_name"] for item in payload["tools"]} == {tool_a.internal_name, tool_b.internal_name}
```

- [ ] **Step 2: Run tests and verify the contract is missing**

Run: `cd backend && pytest tests/orchestration/test_export_contract.py -q`

Expected: FAIL because the contract type and `PlannerContext.export_contract` do not exist.

- [ ] **Step 3: Implement the minimal contract and prompt wording**

```python
EXPORT_FIELD_CONTRACT_VERSION = "kol_excel_v1"

class ExportFieldContract(BaseModel):
    version: str
    required_field_names: tuple[str, ...]
    labels: dict[str, str]
    notes: tuple[str, ...] = ()

def build_export_field_contract(brief: SessionBrief) -> ExportFieldContract:
    region = ",".join(str(v) for v in brief.filters.get("target_fan_locations", [])) or "目标地区"
    age = brief.target_audience or "目标年龄段"
    return ExportFieldContract(
        version=EXPORT_FIELD_CONTRACT_VERSION,
        required_field_names=("platform", "nickname", "followers", "city", f"{brief.category}兴趣占比", f"{region}粉丝占比", f"{age}占比", "engagement_rate", "active_follower_rate", "content_tags", "score"),
        labels={"platform": "平台", "nickname": "昵称", "followers": "粉丝数", "city": "城市"},
        notes=("不得编造缺失数据", "缺失字段显示数据缺失", "每个选中平台必须执行检索"),
    )
```

Append the contract to the planner system prompt and JSON context; retain the complete `tools` tuple unchanged. Build it in the existing context factory with `SessionBrief.from_workspace(workspace)`.

- [ ] **Step 4: Run focused and existing planner tests**

Run: `cd backend && pytest tests/orchestration/test_export_contract.py tests/orchestration/test_context.py tests/orchestration/test_batching.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/orchestration backend/app/model/prompts.py backend/tests/orchestration/test_export_contract.py backend/tests/model/test_prompts.py
git commit -m "feat: add export field contract to planning"
```

### Task 2: Persist the complete candidate pool

**Files:**
- Modify: `backend/app/reporting/models.py`, `backend/app/db/models.py`, `backend/app/reporting/service.py`, `backend/app/reporting/normalizers.py`
- Create: `backend/migrations/versions/0008_candidate_pool_export.py`
- Test: `backend/tests/reporting/test_candidate_pool.py`, `backend/tests/test_phase2_migrations.py`

**Interfaces:**
- Add `TaskCandidatePool` and `TaskCandidatePoolItem` SQLAlchemy models.
- `ReportingService.build_candidate_version(task_id: str, profile: str, *, lease_owner: str | None = None)` persists every normalized row in the pool, then persists only the selected Top10 in `TaskCandidate`.
- `ReportingService.latest_candidate_pool(user_id, session_id)` returns `(task, pool, items)` after ownership validation.

- [ ] **Step 1: Write failing tests for all-candidate persistence**

```python
async def test_build_candidate_version_keeps_all_candidates_and_marks_top10(db, task, evidence):
    result = await ReportingService(db).build_candidate_version(task.id, "balanced", lease_owner="worker")
    pool_items = (await db.execute(select(TaskCandidatePoolItem))).scalars().all()
    top_items = (await db.execute(select(TaskCandidate))).scalars().all()
    assert len(pool_items) == len(evidence.candidates)
    assert len(top_items) == min(10, len(evidence.candidates))
    assert sum(item.is_shortlisted for item in pool_items) == len(top_items)
```

- [ ] **Step 2: Run the focused test and verify the failure**

Run: `cd backend && pytest tests/reporting/test_candidate_pool.py::test_build_candidate_version_keeps_all_candidates_and_marks_top10 -q`

Expected: FAIL because no pool models/table exist and current service discards non-Top10 rows.

- [ ] **Step 3: Add models and migration**

Create `task_candidate_pools` with `id`, `task_id`, `pool_version`, `field_contract_version`, `candidate_count`, `created_at`; create `task_candidate_pool_items` with `id`, `pool_id`, `kol_id`, `snapshot_id`, `full_rank`, `is_shortlisted`, `total_score`, `score_breakdown_json`, `risk_flags_json`, `evidence_json`, `created_at`; add task/pool and pool/KOL/snapshot foreign keys and unique/index constraints.

Update `Base` imports and the service transaction so all normalized rows get snapshots and pool items. Sort the full draft once, enumerate `full_rank`, select the first 10 for existing `TaskCandidate`, and keep existing API/BI Top10 queries unchanged.

- [ ] **Step 4: Extend normalized snapshots with safe export fields**

Add only typed business fields needed by the contract (`city`, `total_likes`, `total_favorites`, `average_reads`, `average_interactions`, age buckets, target-region percentage, active-follower percentage, interest percentage, content tags, gender and field status). Run every field through `redact_evidence_for_storage`; never persist the raw tool payload.

- [ ] **Step 5: Run migration and reporting tests**

Run: `cd backend && pytest tests/reporting/test_candidate_pool.py tests/test_phase2_migrations.py -q`

Expected: PASS and Alembic head is `0008`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/reporting backend/app/db/models.py backend/migrations/versions/0008_candidate_pool_export.py backend/tests/reporting/test_candidate_pool.py backend/tests/test_phase2_migrations.py
git commit -m "feat: persist complete task candidate pools"
```

### Task 3: Generate the four-sheet workbook

**Files:**
- Create: `backend/app/reporting/exporter.py`, `backend/app/reporting/templates/KOL匹配度分析报告.xlsx`
- Test: `backend/tests/reporting/test_exporter.py`

**Interfaces:**
- `async def export_latest_task_xlsx(db: AsyncSession, user_id: str, session_id: str) -> ExportedWorkbook`.
- `ExportedWorkbook.content: bytes`, `filename: str`, `content_type: str`.

- [ ] **Step 1: Write failing workbook tests**

```python
async def test_export_contains_all_pool_items_and_template_sheets(db, task_with_pool):
    output = await export_latest_task_xlsx(db, task_with_pool.user_id, task_with_pool.session_id)
    workbook = load_workbook(BytesIO(output.content), read_only=True)
    assert workbook.sheetnames == ["KOL匹配度筛选", "达人详细画像", "粉丝画像详情", "评分方法论与数据来源"]
    assert workbook["KOL匹配度筛选"].max_row == task_with_pool.pool_count + 4
    assert "平台" in [cell.value for cell in workbook["KOL匹配度筛选"][4]]
```

- [ ] **Step 2: Run test to verify the exporter is absent**

Run: `cd backend && pytest tests/reporting/test_exporter.py -q`

Expected: FAIL because `backend/app/reporting/exporter.py` does not exist.

- [ ] **Step 3: Implement exporter using the reference template style**

Load the committed template asset, copy its four worksheets, preserve fills/fonts/borders/merged titles/column widths, clear only data ranges, and write typed values. Insert the “平台” column after “序号”; use `None`/`数据缺失` based on field status, numeric values for counts/scores, and percentage number formats for percentages. Generate detail blocks for every pool item, a fan-profile row for every pool item, dynamic industry/region/age labels, score distribution and a non-sensitive source/methodology section.

Use the session's `brand`, `category`, `platforms`, `target_audience`, `filters_snapshot`, task completion time and contract version for metadata. Exclude all internal IDs and source URLs from cell values.

- [ ] **Step 4: Verify workbook values and visual output**

Run: `cd backend && pytest tests/reporting/test_exporter.py -q`

Then render the four sheets with the project spreadsheet verification tool and inspect headers, row counts, widths, wrapped reasons and score colors. Expected: all sheets readable, no formula errors, every pool item present.

- [ ] **Step 5: Commit**

```bash
git add backend/app/reporting/exporter.py backend/app/reporting/templates/KOL匹配度分析报告.xlsx backend/tests/reporting/test_exporter.py
git commit -m "feat: generate complete KOL Excel workbook"
```

### Task 4: Add secure latest-round download API

**Files:**
- Modify: `backend/app/reporting/router.py`, `backend/app/reporting/service.py`
- Test: `backend/tests/reporting/test_export_route.py`

**Interfaces:**
- `GET /api/v1/sessions/{session_id}/exports/latest.xlsx` returns `StreamingResponse` with XLSX bytes and a sanitized attachment filename.

- [ ] **Step 1: Write failing route tests**

```python
async def test_latest_export_uses_latest_task_only(client, auth, session, old_task, latest_task):
    response = await client.get(f"/api/v1/sessions/{session.id}/exports/latest.xlsx", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "latest_task" in response.headers["content-disposition"]

async def test_latest_export_rejects_other_users_session(client, other_auth, session):
    response = await client.get(f"/api/v1/sessions/{session.id}/exports/latest.xlsx", headers=other_auth)
    assert response.status_code == 404
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `cd backend && pytest tests/reporting/test_export_route.py -q`

Expected: FAIL with 404 because the route does not exist.

- [ ] **Step 3: Implement ownership and status checks**

Resolve the latest task ordered by `created_at DESC` for the owned session. Return `409 latest_task_in_progress` while it is non-terminal, `422 no_candidate_pool` when terminal without candidates, and call `export_latest_task_xlsx` only for a completed/partially completed task with pool items. Set `Content-Disposition` with sanitized Chinese filename and `Cache-Control: no-store`.

- [ ] **Step 4: Run route, security and full backend tests**

Run: `cd backend && pytest tests/reporting/test_export_route.py tests/security/test_log_redaction.py tests/test_schema.py -q`.

Expected: PASS; no response contains internal IDs or secrets.

- [ ] **Step 5: Commit**

```bash
git add backend/app/reporting/router.py backend/app/reporting/service.py backend/tests/reporting/test_export_route.py
git commit -m "feat: add latest-session Excel export endpoint"
```

### Task 5: Replace BI PDF action with Excel download

**Files:**
- Modify: `src/api/contracts.ts`, `src/api/tasks.ts`, `src/components/BiReport.tsx`, `src/App.tsx`
- Test: `src/components/BiReport.test.tsx`, `src/api/client.test.ts`

**Interfaces:**
- Add `downloadLatestSessionExport(sessionId: string): Promise<Blob>`.
- `BiReport` receives `sessionId?: string`, `taskStatus?: ApiTaskStatus`, `hasCandidateData: boolean` and `onExportError(message: string): void`.

- [ ] **Step 1: Write failing component/API tests**

```tsx
it('shows Excel export and disables it while the latest task is running', () => {
  render(<BiReport report={report} sessionId="s1" taskStatus="running" hasCandidateData={false} />);
  expect(screen.getByRole('button', { name: '导出 Excel' })).toBeDisabled();
});

it('downloads the latest session workbook after completion', async () => {
  const download = vi.spyOn(tasksApi, 'downloadLatestSessionExport').mockResolvedValue(new Blob());
  render(<BiReport report={report} sessionId="s1" taskStatus="completed" hasCandidateData />);
  fireEvent.click(screen.getByRole('button', { name: '导出 Excel' }));
  await waitFor(() => expect(download).toHaveBeenCalledWith('s1'));
});
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `npm run test -- src/components/BiReport.test.tsx src/api/client.test.ts`

Expected: FAIL because the Excel API helper and button props do not exist.

- [ ] **Step 3: Implement download helper and button state**

Use `authorizedFetch` for the binary response, throw the Chinese `detail` on non-2xx, create an object URL, click a temporary anchor, then revoke it. Replace `Printer` with the existing download-compatible icon style and label the button `导出 Excel`. Disable when `taskStatus` is non-terminal, `sessionId` is missing, or `hasCandidateData` is false; show `导出失败，请稍后重试` on failure. Keep the existing panel and button classes.

- [ ] **Step 4: Wire current session and latest task state from `App`**

Pass `workspace.activeSession.id`, `workspace.activeSession.latest_task?.status` and `Boolean(candidatePage?.total)` to `BiReport`. Do not use `isMockMode` or a stale task ID for export.

- [ ] **Step 5: Run frontend tests and build**

Run: `npm run test -- src/components/BiReport.test.tsx src/api/client.test.ts && npm run test && npm run build`

Expected: all frontend tests pass and Vite build succeeds.

- [ ] **Step 6: Commit**

```bash
git add src/api/contracts.ts src/api/tasks.ts src/components/BiReport.tsx src/components/BiReport.test.tsx src/App.tsx src/api/client.test.ts
git commit -m "feat: add latest-session Excel export button"
```

### Task 6: End-to-end verification and integration

**Files:**
- Modify: `backend/tests/integration/test_real_providers.py` only if a safe contract assertion is needed.
- Test: existing backend and frontend suites.

- [ ] **Step 1: Apply Alembic migrations to the local MySQL instance**

Run: `cd backend && alembic upgrade head`

Expected: migration reaches `0008` without dropping existing data.

- [ ] **Step 2: Run full backend verification**

Run: `cd backend && pytest -q`

Expected: all backend tests pass; provider integration tests remain skipped unless credentials are explicitly available.

- [ ] **Step 3: Run full frontend verification**

Run: `npm run test && npm run build`

Expected: all tests pass and production build completes.

- [ ] **Step 4: Perform a local UI/API smoke test**

Start the existing backend and Vite services, create a test session with two platforms, wait for the latest task to settle, click `导出 Excel`, open the downloaded workbook, and verify row count equals the full candidate pool (not 10), the “平台” column contains both platforms, and all four sheets exist.

- [ ] **Step 5: Commit any verification-only fixes and report evidence**

Run: `git status --short && git log -6 --oneline`

Expected: only intentional implementation commits remain; report test counts, migration head, endpoint status and workbook sheet/row verification.
