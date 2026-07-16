# 会话删除、BI 数据分析、AI 后续建议与 Excel 导出修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有真实腾讯模型 + DataTap MCP 主链路上，实现会话软删除、最新一轮 BI 数据分析、可恢复的 AI Top5 后续建议，并修复最新一轮 Excel 导出。

**Architecture:** 保持 FastAPI + SQLAlchemy 异步模块化单体与 React 流式工作区。所有 BI、建议和导出均由显式 `session_id/task_id` 绑定到当前会话最新任务；数据分析只消费 MCP 规范化字段并进行确定性聚合，建议在主汇总后通过独立腾讯模型结构化调用生成并持久化到汇总消息元数据。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy、Alembic、Pydantic、OpenAI-compatible Tencent Token Plan、DataTap MCP、openpyxl、React 19、TypeScript、Recharts、Vitest、Testing Library、Playwright。

---

## 实施前约束

- 先使用 `@superpowers:test-driven-development`：每个任务先写失败测试，再做最小实现。
- BI 截图还原使用 `@product-design:image-to-code`，必须保持现有原型的按钮、图标、圆角、阴影和约 420px 右栏布局。
- Excel 修复使用 `@spreadsheets:Spreadsheets`，必须加载、渲染并验证真实模板，不得只断言接口状态码。
- 每个任务完成后立即运行本任务测试并提交；禁止把所有改动堆到最后一次提交。
- 不新增 Fake 模型或 Fake MCP 生产路径；单元测试使用依赖注入 stub，不改变真实运行配置。
- 设计依据：`docs/superpowers/specs/2026-07-16-session-delete-bi-analytics-followups-export-design.md`。

## 文件结构与职责

### 新建文件

- `backend/migrations/versions/0011_session_soft_delete.py`：增加 `sessions.deleted_at` 和复合索引。
- `backend/app/orchestration/analytics_contract.py`：声明规划阶段需要获取的规范化 BI 字段契约。
- `backend/app/reporting/analytics.py`：只负责规范字段的确定性 BI 聚合和空数据结构。
- `backend/app/tasks/followups.py`：Top5 建议 Schema、输入裁剪和独立模型请求构造。
- `backend/tests/reporting/test_analytics.py`：BI 聚合口径、覆盖率和缺失字段测试。
- `backend/tests/tasks/test_followup_suggestions.py`：建议生成、持久化和非致命失败测试。
- `src/components/BiAnalytics.tsx`：右侧“数据分析”卡片视图。
- `src/components/BiAnalytics.test.tsx`：数据与空态渲染测试。

### 主要修改文件

- `backend/app/workspace/models.py`、`service.py`、`router.py`：软删除及统一过滤。
- `backend/app/tasks/service.py`：创建/重试任务时复用软删除校验和并发保护。
- `backend/app/reporting/normalizers.py`、`schemas.py`、`service.py`、`router.py`：规范分析字段、最新任务契约和 API 输出。
- `backend/app/model/contracts.py`、`prompts.py`：增加 `followup` 结构化模型用途和提示词。
- `backend/app/tasks/dependencies.py`、`executor.py`、`state.py`：建议生成与 SSE 事件。
- `backend/app/reporting/exporter.py`：解除候选明细区冲突合并单元格。
- `src/api/contracts.ts`、`sessions.ts`、`tasks.ts`：删除、消息元数据、BI DTO。
- `src/hooks/useWorkspace.ts`、`useTaskStream.ts`、`src/state/taskEvents.ts`：会话删除、建议恢复和事件合并。
- `src/components/SessionList.tsx`、`ChatArea.tsx`、`BiReport.tsx`、`src/App.tsx`：界面交互。

## Task 1：后端会话软删除与访问隔离

**Files:**

- Create: `backend/migrations/versions/0011_session_soft_delete.py`
- Modify: `backend/app/workspace/models.py`
- Modify: `backend/app/workspace/service.py`
- Modify: `backend/app/workspace/router.py`
- Modify: `backend/app/tasks/service.py`
- Modify: `backend/app/tasks/repository.py`
- Modify: `backend/app/tasks/router.py`
- Modify: `backend/app/tasks/events.py`
- Modify: `backend/app/reporting/service.py`
- Test: `backend/tests/workspace/test_sessions.py`
- Test: `backend/tests/tasks/test_task_events.py`
- Test: `backend/tests/tasks/test_task_state.py`
- Test: `backend/tests/reporting/test_candidate_selection.py`
- Test: `backend/tests/reporting/test_export_route.py`
- Test: `backend/tests/test_schema.py`

- [ ] **Step 1: 写软删除失败测试**

覆盖：删除成功、他人会话 404、列表刷新不可见、详情/更新/追加消息不可见、已删除会话不能创建/重试任务、任务详情/取消/SSE 不可访问、候选/报告/导出不可访问、重复删除幂等。

```python
@pytest.mark.asyncio
async def test_soft_deleted_session_is_hidden_from_all_workspace_reads(client, auth_headers, session_id):
    response = await client.delete(f"/api/v1/sessions/{session_id}", headers=auth_headers)
    assert response.status_code == 204
    assert all(item["id"] != session_id for item in (await client.get("/api/v1/sessions", headers=auth_headers)).json())
    assert (await client.get(f"/api/v1/sessions/{session_id}", headers=auth_headers)).status_code == 404
    assert (await client.post(f"/api/v1/sessions/{session_id}/tasks", json={"content": "重跑"}, headers=auth_headers)).status_code == 404
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend && .venv/bin/pytest tests/workspace/test_sessions.py tests/tasks/test_task_events.py tests/tasks/test_task_state.py tests/reporting/test_candidate_selection.py tests/reporting/test_export_route.py tests/test_schema.py -q`

Expected: FAIL，删除路由不存在或已删除记录仍能查询。

- [ ] **Step 3: 增加数据库迁移和模型字段**

迁移核心：

```python
revision = "0011_session_soft_delete"
down_revision = "0010_message_error_idempotency"

def upgrade() -> None:
    op.add_column("sessions", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_sessions_user_deleted_last_accessed",
        "sessions",
        ["user_id", "deleted_at", "last_accessed_at"],
    )
```

`WorkspaceSession` 增加 `deleted_at: Mapped[datetime | None]`。降级先删除索引再删除字段。`create_session()` 的后端默认标题固定为 `title -> 品牌/项目组合 -> 行业 KOL 分析 -> 未命名会话` 的同义规则，不依赖后续任务内容。

- [ ] **Step 4: 统一过滤并增加删除接口**

`WorkspaceService.get_owned_session()` 和 `list_sessions()` 都加入 `WorkspaceSession.deleted_at.is_(None)`；新增：

```python
async def delete_session(self, user_id: str, session_id: str) -> None:
    workspace = await self.get_owned_session(user_id, session_id, for_update=True)
    workspace.deleted_at = utc_now()
    workspace.updated_at = workspace.deleted_at
    await self.db.flush()
```

路由新增 `DELETE /{session_id}`，返回 204。`TaskService.create()` 已通过 `get_owned_session(..., for_update=True)` 做并发检查，保留该入口。

所有派生资源所有权查询必须 join `WorkspaceSession` 并加入 `deleted_at IS NULL`：

- `TaskRepository.get_owned()` 与 `list_owned_events_after()`，从而覆盖任务详情、取消、重试与 SSE；
- `ReportingService._owned_task()`、`get_owned_report()`、`list_candidates()`、`latest_session_analysis()` 与 `latest_candidate_pool()`；
- Excel 导出入口。

删除后即使持有历史 `task_id/report_id` 也返回 404。

- [ ] **Step 5: 运行迁移与测试**

Run: `cd backend && .venv/bin/alembic upgrade head`

Expected: 当前数据库升级到 `0011_session_soft_delete`。

Run: `mysql -u root -p -D kol_insight -e "EXPLAIN SELECT id FROM sessions WHERE user_id='00000000-0000-0000-0000-000000000000' AND deleted_at IS NULL ORDER BY last_accessed_at DESC LIMIT 50"`

Expected: `key` 为 `ix_sessions_user_deleted_last_accessed`，`Extra` 不出现全表扫描。若优化器仍需要旧索引则保留；若确认复合索引完全覆盖，则先执行下面的 downgrade，再编辑尚未最终提交的 `0011` migration 加入旧索引删除，之后重新 upgrade。

Run: `cd backend && .venv/bin/alembic downgrade 0010_message_error_idempotency`

Expected: 数据库回到 `0010`，`deleted_at` 与新索引消失，旧索引存在。

完成最终 migration 内容后运行：

Run: `cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade 0010_message_error_idempotency && .venv/bin/alembic upgrade head`

Expected: 修改后的最终 migration 完成两次 upgrade 和一次 downgrade，最终停在 `0011`。再次执行上述 `EXPLAIN`，预期 `key` 仍为新复合索引；不得在已应用状态下修改 migration 而不重新 downgrade/upgrade。

Run: `cd backend && .venv/bin/pytest tests/workspace/test_sessions.py tests/tasks/test_task_events.py tests/tasks/test_task_state.py tests/reporting/test_candidate_selection.py tests/reporting/test_export_route.py tests/test_schema.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/migrations/versions/0011_session_soft_delete.py backend/app/workspace backend/app/tasks backend/app/reporting/service.py backend/tests/workspace/test_sessions.py backend/tests/tasks backend/tests/reporting/test_candidate_selection.py backend/tests/reporting/test_export_route.py backend/tests/test_schema.py
git commit -m "feat: add soft deletion for sessions"
```

## Task 2：前端删除交互与会话名称恢复

**Files:**

- Modify: `src/api/sessions.ts`
- Modify: `src/components/SessionList.tsx`
- Modify: `src/components/SessionList.test.tsx`
- Modify: `src/hooks/useWorkspace.ts`
- Modify: `src/hooks/useWorkspace.test.tsx`
- Modify: `src/App.tsx`

- [ ] **Step 1: 写删除与标题回退失败测试**

覆盖悬停删除按钮、确认/取消、删除当前会话后选最近会话、删除最后一个会话后清空工作区、API 失败恢复、标题优先级及项目为空可重命名。

```tsx
it('shows a stable fallback title and confirms deletion', async () => {
  render(<SessionList sessions={[{ ...session, title: '', brand: '', campaignName: null, category: '餐饮' }]} onDeleteSession={onDelete} />);
  expect(screen.getByText('餐饮 KOL 分析')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '删除会话' }));
  fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(onDelete).toHaveBeenCalledWith(session.id));
});
```

- [ ] **Step 2: 运行前端测试并确认失败**

Run: `npm test -- src/components/SessionList.test.tsx src/hooks/useWorkspace.test.tsx`

Expected: FAIL，缺少 `onDeleteSession/deleteSession`。

- [ ] **Step 3: 实现 API 与状态切换**

`src/api/sessions.ts`：

```ts
export async function deleteSession(id: string): Promise<void> {
  await request(`/api/v1/sessions/${id}`, { method: 'DELETE' });
}
```

`useWorkspace.deleteSession(id)` 成功后从列表移除；若删除当前会话，选择剩余列表第一项并调用现有详情加载，若为空则清除 `activeSession/activeTaskId/candidates/biReport`。请求失败不提前永久移除列表。

- [ ] **Step 4: 实现列表交互和稳定名称**

使用现有 Lucide `Trash2`，会话行 hover/focus 时出现删除按钮；使用现有原型样式的确认浮层或对话框。名称函数固定为：

```ts
const displayName = session.title.trim()
  || [session.brand.trim(), session.campaignName?.trim()].filter(Boolean).join(' - ')
  || (session.category.trim() ? `${session.category.trim()} KOL 分析` : '')
  || '未命名会话';
```

重命名提交只要求标题非空，不再要求 `campaignName` 非空。

- [ ] **Step 5: 运行测试和类型检查**

Run: `npm test -- src/components/SessionList.test.tsx src/hooks/useWorkspace.test.tsx`

Expected: PASS。

Run: `npm run lint`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/api/sessions.ts src/components/SessionList.tsx src/components/SessionList.test.tsx src/hooks/useWorkspace.ts src/hooks/useWorkspace.test.tsx src/App.tsx
git commit -m "feat: add session deletion and stable titles"
```

## Task 3：把 BI 字段纳入每轮模型规划与 MCP 规范化

**Files:**

- Create: `backend/app/orchestration/analytics_contract.py`
- Modify: `backend/app/orchestration/schemas.py`
- Modify: `backend/app/orchestration/context.py`
- Modify: `backend/app/model/prompts.py`
- Modify: `backend/app/reporting/schemas.py`
- Modify: `backend/app/reporting/normalizers.py`
- Test: `backend/tests/orchestration/test_export_contract.py`
- Test: `backend/tests/reporting/test_normalizers.py`

- [ ] **Step 1: 写规划字段契约和规范化失败测试**

要求每轮 `PlannerContext` 同时包含 Excel 和 BI 字段契约；验证小红书/抖音别名能规范化为：`brand_mentions`、`exposure`、`interactions`、`published_at`、`sentiment_counts`、`hot_words`、`audience_age`、`audience_gender`、`audience_regions`。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend && .venv/bin/pytest tests/orchestration/test_export_contract.py tests/reporting/test_normalizers.py -q`

Expected: FAIL，`analytics_contract/analytics_fields` 不存在。

- [ ] **Step 3: 新增 BI 字段契约并传给规划模型**

```python
ANALYTICS_FIELD_NAMES = (
    "brand_mentions", "exposure", "interactions", "published_at",
    "sentiment_counts", "hot_words", "audience_age",
    "audience_gender", "audience_regions",
)
```

在 `PlannerContext` 增加 `analytics_contract`，由 `ContextBuilder` 固定注入；更新 planner system prompt：每个用户选择的平台都应规划可用工具获取 Excel + BI 字段，无法获得时标记缺失，禁止猜测。

- [ ] **Step 4: 扩展规范化快照**

`NormalizedKolEvidence` 增加 `analytics_fields: dict[str, Any]`；适配器只从审核过的字段别名提取并做类型校验、脱敏、统一日期/百分比/地域格式。`as_dict()` 写入规范字段，`_merge()` 仅合并非空规范值。未知原始字段继续丢弃。

- [ ] **Step 5: 运行测试**

Run: `cd backend && .venv/bin/pytest tests/orchestration/test_export_contract.py tests/reporting/test_normalizers.py -q`

Expected: PASS，且测试确认原始评论、URL、密钥与未映射字段不进入快照。

- [ ] **Step 6: 提交**

```bash
git add backend/app/orchestration backend/app/model/prompts.py backend/app/reporting/schemas.py backend/app/reporting/normalizers.py backend/tests/orchestration/test_export_contract.py backend/tests/reporting/test_normalizers.py
git commit -m "feat: plan and normalize BI analytics fields"
```

## Task 4：确定性生成最新一轮数据分析 DTO

**Files:**

- Create: `backend/app/reporting/analytics.py`
- Create: `backend/tests/reporting/test_analytics.py`
- Modify: `backend/app/reporting/service.py`
- Modify: `backend/app/reporting/schemas.py`
- Modify: `backend/app/reporting/router.py`
- Modify: `backend/tests/reporting/test_candidate_selection.py`
- Modify: `src/api/contracts.ts`
- Modify: `src/api/tasks.ts`
- Modify: `src/types.ts`
- Modify: `src/test/fixtures.ts`

- [ ] **Step 1: 写聚合口径失败测试**

测试内容去重、曝光求和、加权互动率、情感归一化、热词排序、日期趋势、年龄/性别/地域聚合、覆盖率和全部缺失空结构；另建最新任务已存在报告但状态仍为 running 的用例，断言 API 不返回该报告数值。

```python
def test_aggregate_analytics_never_invents_missing_values() -> None:
    result = aggregate_analytics([])
    assert result["overview"]["brand_volume"]["available"] is False
    assert result["sentiment"]["items"] == []
    assert result["exposure_trend"] == []
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend && .venv/bin/pytest tests/reporting/test_analytics.py -q`

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现纯函数聚合器**

`aggregate_analytics()` 只接受已经规范化的候选分析字段，输出：

```python
{
  "overview": {
    "brand_volume": metric(..., unit="条"),
    "total_exposure": metric(..., unit="次"),
    "average_engagement_rate": metric(..., unit="%"),
  },
  "sentiment": {"available": bool, "items": [], "hot_words": []},
  "exposure_trend": [],
  "audience": {"age": [], "gender": [], "regions": []},
}
```

每个 metric 包含 `value/unit/available/coverage/source_fields`。任何无法证明的值保持 `None`。

- [ ] **Step 4: 把分析结果写入报告并透传 API**

`ReportingService._build_bi_payload()` 把聚合结果写入 `chart_data_json["analytics"]`。`BiReportRead` 增加 `analytics`，`bi_report_read()` 为旧报告返回 `empty_analytics()`。前端 DTO 增加严格类型，不在浏览器二次推算。

`latest_session_analysis()` 保持“按创建时间最新任务”规则，并在服务端强制终态门禁：只有最新任务状态为 `completed/completed_with_warnings` 才允许返回 `latest_candidates/latest_report` 和分析数值。`pending/running/interrupted/failed/cancelled` 即使数据库已提前生成 `BiReport` 也只返回最新任务状态，候选与报告字段置空，前端展示固定加载/错误空框；绝不回退旧报告或提前展示半成品。

- [ ] **Step 5: 运行后端与前端契约测试**

Run: `cd backend && .venv/bin/pytest tests/reporting/test_analytics.py tests/reporting/test_candidate_selection.py -q`

Expected: PASS。

Run: `npm test -- src/api src/test/fixtures.ts`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/reporting backend/tests/reporting src/api src/types.ts src/test/fixtures.ts
git commit -m "feat: expose deterministic latest-round analytics"
```

## Task 5：实现右侧“数据分析”Tab

**Files:**

- Create: `src/components/BiAnalytics.tsx`
- Create: `src/components/BiAnalytics.test.tsx`
- Modify: `src/components/BiReport.tsx`
- Modify: `src/components/BiReport.test.tsx`
- Modify: `src/index.css`（仅在现有 Tailwind 类不足时做最小补充）

- [ ] **Step 1: 按截图写 UI 失败测试**

验证两个 Tab、三项 KPI、情感图、热词、曝光趋势、年龄、性别、地域 Top5；所有数据缺失时卡片标题仍存在且显示“数据不足”。

- [ ] **Step 2: 运行测试并确认失败**

Run: `npm test -- src/components/BiAnalytics.test.tsx src/components/BiReport.test.tsx`

Expected: FAIL，组件和 Tab 不存在。

- [ ] **Step 3: 实现 420px 紧凑数据分析组件**

使用 Recharts 的 `PieChart/LineChart/BarChart/ResponsiveContainer`，使用 Lucide 现有图标。不得手写 SVG 图表。保持白色卡片、浅灰边框、紫色主色、现有圆角阴影和纵向滚动。

组件接口：

```ts
interface BiAnalyticsProps {
  analytics?: BiAnalyticsData;
  taskStatus?: string;
}
```

任务运行中显示固定卡片骨架；失败或缺字段显示“数据不足”；不以 `0` 代替未知。

- [ ] **Step 4: 在 BiReport 中增加 Tab 且保持导出按钮**

Tab 状态只控制内容，不改变 `sessionId/candidateVersion` 数据绑定。切换会话时由父级新 props 立即渲染该会话空/加载状态，不能保留旧图表局部 state。

- [ ] **Step 5: 运行测试、类型检查和视觉截图**

Run: `npm test -- src/components/BiAnalytics.test.tsx src/components/BiReport.test.tsx`

Expected: PASS。

Run: `npm run lint`

Expected: PASS。

用真实浏览器在 1440px 桌面宽度截图，确认右栏约 420px、无横向溢出、卡片顺序与附件一致、空态仍保留全部卡片。

- [ ] **Step 6: 提交**

```bash
git add src/components/BiAnalytics.tsx src/components/BiAnalytics.test.tsx src/components/BiReport.tsx src/components/BiReport.test.tsx src/index.css
git commit -m "feat: add latest-round BI analytics tab"
```

## Task 6：后端独立生成并持久化 AI Top5 建议

**Files:**

- Create: `backend/app/tasks/followups.py`
- Create: `backend/tests/tasks/test_followup_suggestions.py`
- Modify: `backend/app/model/contracts.py`
- Modify: `backend/app/model/prompts.py`
- Modify: `backend/app/tasks/state.py`
- Modify: `backend/app/tasks/dependencies.py`
- Modify: `backend/app/tasks/executor.py`
- Modify: `backend/app/tasks/events.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/workspace/router.py`
- Test: `backend/tests/workspace/test_sessions.py`
- Modify: `backend/tests/tasks/test_executor.py`
- Modify: `backend/tests/tasks/test_task_events.py`

- [ ] **Step 1: 写 Schema、生成和非致命失败测试**

测试必须恰好 5 条专业中文建议、英文/混合英文/内部引用触发校验失败、`complete_json()` 的一次自动修复、消息元数据持久化、SSE started/updated/failed，以及 prepare 或模型调用失败后主任务仍为 completed/completed_with_warnings。另测“任务先终态、模型后调用”、进程中断后的 pending 恢复、生产执行锁不重复调用，以及旧任务没有建议元数据时 SSE 仍按原规则关闭。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend && .venv/bin/pytest tests/tasks/test_followup_suggestions.py tests/tasks/test_executor.py tests/tasks/test_task_events.py -q`

Expected: FAIL，缺少 followup artifact 和事件。

- [ ] **Step 3: 定义严格 Schema 与 Prompt**

```python
class FollowupSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=2, max_length=40)
    prompt: str = Field(min_length=5, max_length=500)

    @field_validator("title", "prompt")
    @classmethod
    def require_chinese(cls, value: str) -> str:
        visible = [char for char in value if char.isalnum()]
        chinese = re.findall(r"[\u4e00-\u9fff]", value)
        if not visible or len(chinese) / len(visible) < 0.6:
            raise ValueError("chinese_text_required")
        if contains_internal_reference(value):
            raise ValueError("internal_reference_forbidden")
        return value

class FollowupSuggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestions: tuple[FollowupSuggestion, ...] = Field(min_length=5, max_length=5)
```

把 `StructuredModelRequest.purpose` 和 `ModelRequestMetadata.purpose` 扩展 `followup`，增加 `FOLLOWUP_PROMPT(name="followup_v1")`。`contains_internal_reference()` 拒绝 URL、UUID、`Bearer`/`sk-` 密钥形态、`/api/`、`datatap`、`mcp`、`step_`、审核工具内部名以及本轮工具内部名。

模型输入明确包含：当前用户问题、会话筛选条件、安全工具成功/失败概况、最新候选数量、报告 overview、analytics 可用性和本轮结论；不包含原始达人数据、密钥、接口地址或完整 MCP 响应。

- [ ] **Step 4: 在汇总消息元数据中原子持久化**

新增 `FollowupSuggestionService`，以 `task_id` 查找本轮 assistant 汇总消息：查询限定 `session_id/user_id/role=assistant`，对候选消息 `SELECT ... FOR UPDATE` 后匹配 `metadata_json.task_id`。每次更新都通过“复制旧字典后整体重新赋值”保留已有 `status/task_id` 等元数据，禁止原位修改 JSON。

生产环境增加 `FollowupExecutionLock`：在独占数据库连接上使用 MySQL advisory lock（锁名为 task_id 的 SHA-256 摘要）覆盖整个腾讯模型调用，连接关闭/进程退出会自动释放。恢复进程只有拿到锁才允许调用模型；原调用仍存活或超时时，锁保持占用，因此不会重复产生付费调用。模型客户端自身超时负责终止悬挂请求。单元测试通过依赖注入的内存锁验证互斥，不把 Fake 路径带入生产配置。

执行分两阶段：

1. `prepare(task_id)` 在主任务终态前，仅原子写入 `task_id`、`followup_suggestions_status=pending`、空数组和 `started_at`，不调用模型；
2. 主任务写入 `completed/completed_with_warnings` 后，`generate(task_id)` 再验证数据库终态并调用腾讯模型。

成功时再次锁定同一消息并原子写入：

```python
{
  "task_id": task.id,
  "followup_suggestions_status": "completed",
  "followup_suggestions": result.value.model_dump(mode="json")["suggestions"],
  "followup_suggestions_generated_at": utc_now().isoformat(),
}
```

失败只写 `failed` + 空数组，并发送安全失败事件。测试覆盖 advisory lock 竞争、写入异常和原有 metadata 不被覆盖。

`message_read()` 不再透传整个内部字典，而是通过 `public_message_metadata()` 仅返回现有前端所需键及 `task_id/followup_suggestions_status/followup_suggestions/followup_suggestions_generated_at`；任何内部锁信息都不进入消息 metadata，也不会通过 API 暴露。

- [ ] **Step 5: 接入执行器成功路径**

执行顺序固定为：

1. `stream_summary()` 完成；
2. 在局部 `try/except` 中执行 `prepare_followups()`，写 pending 失败只记安全日志，不能进入外层任务失败分支；
3. `mark_completed*()` 先把任务状态和完成事件持久化；
4. `generate_followups()` 确认任务已是 `completed/completed_with_warnings` 后进行独立腾讯模型调用。

`generate_followups()` 使用局部 `try/except`，不能进入执行器外层失败分支，因此不延迟主任务进入终态，也不能把成功任务改成失败。

为保持 SSE：当成功完成事件对应的汇总消息为 `pending` 时，`TaskEventStream` 不立即关闭，继续等待 `followup.suggestions_updated/failed`，并把这两个事件视为成功任务流的最终事件；旧历史任务没有 followup metadata 时仍在原 completed 事件关闭。所有 SSE data 必须显式携带 `session_id`、`task_id`，SSE `id` 继续作为事件序号，相关测试覆盖断线续传和跨会话过滤。

- [ ] **Step 6: 增加 pending 恢复机制**

在现有应用启动/30 秒恢复循环中，join `WorkspaceSession` 扫描未软删除会话内 `completed/completed_with_warnings` 且汇总消息 followup 状态为 `pending` 的任务；`completed/failed`、已软删除会话和没有 followup metadata 的上线前历史任务均跳过。恢复先竞争 MySQL advisory lock，只有获得锁的进程调用模型；锁由原调用持有直到模型返回、超时或连接终止，因此不会为同一轮产生并行重复付费调用，也不会为旧/已删除会话产生额外费用。

- [ ] **Step 7: 运行测试**

Run: `cd backend && .venv/bin/pytest tests/tasks/test_followup_suggestions.py tests/tasks/test_executor.py tests/tasks/test_task_events.py tests/model/test_runs.py -q`

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add backend/app/model backend/app/tasks backend/app/main.py backend/app/workspace/router.py backend/tests/tasks backend/tests/workspace/test_sessions.py backend/tests/model/test_runs.py
git commit -m "feat: generate persistent AI follow-up suggestions"
```

## Task 7：前端恢复、展示并点击执行 Top5 建议

**Files:**

- Modify: `backend/app/tasks/router.py`
- Modify: `backend/app/tasks/service.py`
- Modify: `backend/tests/tasks/test_task_state.py`
- Modify: `src/types.ts`
- Modify: `src/api/contracts.ts`
- Modify: `src/api/sessions.ts`
- Modify: `src/api/tasks.ts`
- Modify: `src/state/taskEvents.ts`
- Modify: `src/state/taskEvents.test.ts`
- Modify: `src/hooks/useTaskStream.ts`
- Modify: `src/hooks/useWorkspace.ts`
- Modify: `src/hooks/useWorkspace.test.tsx`
- Modify: `src/components/ChatArea.tsx`
- Modify: `src/components/ChatArea.test.tsx`
- Modify: `src/App.tsx`

- [ ] **Step 1: 写刷新恢复、事件更新和点击执行失败测试**

覆盖普通会话详情从消息 metadata 恢复建议；SSE updated 替换当前任务建议；切换会话只显示匹配 `taskId` 的建议；点击建议调用现有 `appendMessage(prompt)`；运行中禁用；失败显示空态；相同 `Idempotency-Key` 的 HTTP 重放只产生一条消息、一个任务和一次 runner submit。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend && .venv/bin/pytest tests/tasks/test_task_state.py -q && cd .. && npm test -- src/state/taskEvents.test.ts src/hooks/useWorkspace.test.tsx src/components/ChatArea.test.tsx`

Expected: FAIL，后端普通任务无幂等头处理，前端类型和 reducer 尚无建议字段。

- [ ] **Step 3: 扩展消息和运行时类型**

```ts
export interface FollowupSuggestion { title: string; prompt: string }

export interface Message {
  // existing fields
  followupSuggestions?: FollowupSuggestion[];
  followupSuggestionsStatus?: 'pending' | 'completed' | 'failed';
}
```

`toSession()` 仅从 metadata 中 `task_id === latest_task.id` 的 assistant message 读取建议。SSE reducer 处理 `followup.suggestions_started/updated/failed`，同时校验事件的 `session_id` 与 `task_id`。

- [ ] **Step 4: 为普通新轮次增加端到端幂等键**

`POST /sessions/{session_id}/tasks` 接收 `Idempotency-Key` header。`TaskService` 在活动任务检查之前，以 `sha256(user_id + session_id + header)` 生成 `request:` 前缀键并复用现有唯一 `AnalysisTask.retry_key` 存储；同键已有任务且用户、会话和触发内容一致时返回已有任务，内容不一致返回 409。

保持现有 `TaskService.create() -> AnalysisTask` 签名不变，避免破坏 `retry()`。新增 `TaskService.create_idempotent(...) -> TaskCreationResult(task, created)` 只供普通 `POST /sessions/{session_id}/tasks` 使用。

幂等创建必须先用 `WorkspaceService.get_owned_session(..., for_update=True)` 锁定会话行，再在锁内查询请求键；同一会话的两个并发请求会串行化，第二个请求获得锁后必须二次查询并返回第一个任务。新建前计算规范载荷摘要：

```python
payload_digest = sha256(json.dumps({
    "content": payload.content,
    "scoring_profile": payload.scoring_profile,
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
```

摘要写入触发消息 metadata 的服务端字段；同键重放时必须同时校验 `user_id/session_id/payload_digest`，完全一致才返回已有任务，否则 409。仍需捕获唯一约束 `IntegrityError` 作为跨事务防线：回滚到 savepoint 后重查已有任务并做相同摘要校验，不把数据库异常暴露给用户。

未命中时调用现有 `create(..., retry_key=request_key)`。路由只在 `created is True` 时 `task_runner.submit(task.id)`，从而避免完成后的 HTTP 重放再次创建消息、任务或计费调用。重试路由继续使用现有 `retry()` 和 `retry:{source.id}` 语义，增加回归测试确认返回类型不变；并发测试用两个同键请求断言得到同一 task id、只有一条用户消息和一次 runner submit。

前端 `createTask()` 必须传入调用开始时生成并在该请求生命周期内复用的 `crypto.randomUUID()`。

- [ ] **Step 5: 替换固定常用指令**

`ChatArea` 底部改为“AI 建议的进一步分析”：pending 显示骨架，completed 显示 5 个按钮，failed/空显示说明。点击执行 `onSendMessage(suggestion.prompt)`，当前任务运行或提交中时禁用。

- [ ] **Step 6: 运行测试和类型检查**

Run: `cd backend && .venv/bin/pytest tests/tasks/test_task_state.py -q`

Expected: PASS，普通新轮次 HTTP 重放幂等。

Run: `npm test -- src/state/taskEvents.test.ts src/hooks/useWorkspace.test.tsx src/components/ChatArea.test.tsx`

Expected: PASS。

Run: `npm run lint`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/tasks/router.py backend/app/tasks/service.py backend/tests/tasks/test_task_state.py src/types.ts src/api src/state src/hooks src/components/ChatArea.tsx src/components/ChatArea.test.tsx src/App.tsx
git commit -m "feat: continue conversations from AI suggestions"
```

## Task 8：修复 Excel 合并单元格写入并验证真实模板

**Files:**

- Modify: `backend/app/reporting/exporter.py`
- Modify: `backend/app/reporting/router.py`
- Modify: `backend/tests/reporting/test_exporter.py`
- Modify: `backend/tests/reporting/test_export_route.py`

- [ ] **Step 1: 写复现 `MergedCell` 的失败测试**

测试使用项目真实模板并导出 12 条以上候选；断言工作簿可重新打开、全部候选存在、平台列正确，且标题/汇总合并范围、公式、列宽与样式未损坏。

```python
def test_render_workbook_unmerges_only_candidate_detail_cells() -> None:
    content = render_workbook(metadata=metadata, candidates=[_candidate(i) for i in range(1, 13)])
    workbook = load_workbook(BytesIO(content), data_only=False)
    assert workbook["KOL匹配度筛选"].max_row >= 16
    assert all(not isinstance(workbook["KOL匹配度筛选"].cell(row, 2), MergedCell) for row in range(5, 17))
```

- [ ] **Step 2: 运行测试并确认真实异常**

Run: `cd backend && .venv/bin/pytest tests/reporting/test_exporter.py::test_render_workbook_unmerges_only_candidate_detail_cells -q`

Expected: FAIL，出现 `AttributeError: 'MergedCell' object attribute 'value' is read-only`。

- [ ] **Step 3: 实现候选数据区域定向解除合并**

新增纯辅助函数：

```python
def _unmerge_intersecting_ranges(sheet, *, min_row: int, max_row: int, min_col: int, max_col: int) -> None:
    for merged in tuple(sheet.merged_cells.ranges):
        if merged.max_row < min_row or merged.min_row > max_row:
            continue
        if merged.max_col < min_col or merged.min_col > max_col:
            continue
        sheet.unmerge_cells(str(merged))
```

在复制样式/写值前，只对每个候选明细区调用。标题和汇总区不在边界内。写入前还应防御性检测目标是否仍为 `MergedCell`，若是则抛稳定内部错误而不是静默破坏模板。

候选超过模板预留行时，以最后一条模板明细行为样板逐行复制：单元格样式、数字格式、边框、填充、对齐、行高和相对公式；公式用 openpyxl `Translator` 按目标行平移，不能把样板行公式原样复制。列宽属于 Sheet 维度，扩展前后保持不变。测试分别断言第一个扩展行的公式引用、样式 ID、行高和所有列宽。

- [ ] **Step 4: 改善导出错误映射**

最新任务运行中或无候选返回 409 中文业务错误；模板渲染异常记录安全结构日志并返回统一 `export_render_failed`，前端现有内联错误继续显示“导出失败，请稍后重试”。

- [ ] **Step 5: 运行 Excel 测试并渲染检查**

Run: `cd backend && .venv/bin/pytest tests/reporting/test_exporter.py tests/reporting/test_export_route.py -q`

Expected: PASS。

使用 Spreadsheets 技能打开生成文件，检查四个 Sheet、标题合并、表头、平台列、12 条以上候选、列宽和行样式。

- [ ] **Step 6: 提交**

```bash
git add backend/app/reporting/exporter.py backend/app/reporting/router.py backend/tests/reporting/test_exporter.py backend/tests/reporting/test_export_route.py
git commit -m "fix: export candidates across merged template rows"
```

## Task 9：全链路回归与真实服务验收

**Files:**

- Modify only if a verified defect is found; otherwise no production changes.
- Test: existing backend/frontend suites and browser flow.

- [ ] **Step 1: 运行后端全量测试与静态检查**

Run: `cd backend && .venv/bin/pytest -q`

Expected: PASS。

Run: `cd backend && .venv/bin/ruff check app tests`

Expected: PASS。

- [ ] **Step 2: 运行前端全量测试、类型检查和构建**

Run: `npm test`

Expected: PASS。

Run: `npm run lint && npm run build`

Expected: PASS。

- [ ] **Step 3: 用最新 main 启动前后端**

后端使用本地 MySQL、真实 `TENCENT_PLAN_API_KEY`、`https://tokenhub.tencentmaas.com/plan/v3`、模型 `DeepSeek-V4-Pro` 和真实 DataTap MCP 配置；前端仍使用现有模拟登录。

Run: `cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000`

Run: `npm run dev -- --host 127.0.0.1 --port 5173`

Expected: `/healthz` 返回 200，前端可访问。

- [ ] **Step 4: 浏览器验收最新一轮主链路**

创建会话并提交：行业“餐饮”，渠道“小红书 + 抖音”，提问“找出最近30天活跃top10的达人，要求粉丝以湖州和浙江为主，粉丝数量须大于2万，粉丝消费力超过100元人民币”。

确认：

1. 实际阶段事件持续更新，无假 BI 预填；
2. 两个平台结果按本轮可用数据汇总；
3. BI 报告和数据分析均只属于当前会话最新任务；
4. 缺失指标保留卡片并显示数据不足；
5. 完成后出现 5 条腾讯模型建议；
6. 点击建议启动同一会话下一轮真实模型 + MCP；
7. 最新一轮 Excel 能下载并打开；
8. 删除会话后刷新仍不可见，会话名称保持显示。

- [ ] **Step 5: 监控安全日志**

确认日志包含任务/阶段/工具状态和安全错误码，但不包含达人原始数据、完整 Prompt、密钥或模型/MCP 接口地址。

- [ ] **Step 6: 最终提交（仅在验收中产生修复时）**

```bash
git add <verified-fix-files>
git commit -m "fix: close end-to-end analytics regressions"
```

## 完成条件

- 所有任务复选项完成并逐任务提交。
- 后端、前端测试和构建通过。
- 数据库迁移在本地 MySQL 成功。
- 真实腾讯模型 + DataTap MCP 至少完成两轮同会话分析。
- 会话软删除刷新稳定、BI 不串会话/轮次、Top5 可恢复并可点击执行。
- Excel 使用真实模板导出成功且格式未损坏。
