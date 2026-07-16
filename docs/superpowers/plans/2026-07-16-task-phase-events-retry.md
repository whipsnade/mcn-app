# 任务阶段事件与会话重跑实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 基于后台真实任务事件展示阶段、持久化安全错误，并支持同一会话从已发送用户消息幂等重跑。

**Architecture:** 后端在任务执行器和 MCP 批次边界写入 canonical SSE 事件；错误通过白名单文案生成持久化 assistant 消息；重跑使用数据库幂等键创建新任务。前端 reducer 只消费事件，按 task/version 隔离候选和 BI，并在终态用户消息上提供“再次执行”。

**Tech Stack:** FastAPI、SQLAlchemy/MySQL、Alembic、SSE、React、TypeScript、Vitest、pytest。

---

### Task 1: 扩展任务事件和安全错误契约

**Files:**
- Modify: `backend/app/tasks/state.py`
- Modify: `backend/app/tasks/schemas.py`
- Create: `backend/app/tasks/errors.py`
- Test: `backend/tests/tasks/test_task_events.py`

- [ ] **Step 1: 写失败测试**：覆盖 canonical 阶段名称、平台白名单、错误码到中文文案映射、错误文案长度和敏感文本剔除。
- [ ] **Step 2: 运行测试确认失败**：`./.venv/bin/pytest tests/tasks/test_task_events.py -q`。
- [ ] **Step 3: 实现最小契约**：新增 `phase.changed`（如需要）、安全平台转换和中心化错误 sanitizer；扩展 `TaskRead` 的 `error_message`。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "feat: define safe task phase event contract"`。

### Task 2: 后端发出真实 MCP 阶段事件并持久化错误

**Files:**
- Modify: `backend/app/tasks/executor.py`
- Modify: `backend/app/tasks/repository.py`
- Modify: `backend/app/tasks/dependencies.py`
- Modify: `backend/app/tasks/events.py`
- Modify: `backend/app/mcp_gateway/service.py`
- Modify: `backend/app/mcp_gateway/accounting.py`
- Test: `backend/tests/tasks/test_executor.py`
- Test: `backend/tests/mcp_gateway/test_billing_lifecycle.py`

- [ ] **Step 1: 写失败测试**：覆盖 plan/replan/tool/candidate/BI/summary 事件顺序、success/failed/unknown、多平台实际 `step_total`、错误 assistant 消息幂等。
- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现事件写入**：执行器在真实批次边界写入安全 canonical 事件；repository 以短事务追加事件；`TaskEventStream` 增加 broker 通知与数据库轮询兜底，保证落库后实时 SSE 到达并支持 `Last-Event-ID` 重放。
- [ ] **Step 4: 实现 MCP 账务事实到 canonical 事件映射**，移除 `accounting.py` 向对外 `task_events` 写入 `mcp_call_*` 内部事件；不暴露内部 ID、工具名或原始诊断。
- [ ] **Step 5: 运行任务/MCP 测试确认通过**。
- [ ] **Step 6: 提交**：`git commit -m "feat: emit real task phase events and safe errors"`。

### Task 3: 实现重跑 API 与数据库幂等

**Files:**
- Modify: `backend/app/tasks/models.py`
- Modify: `backend/app/tasks/service.py`
- Modify: `backend/app/tasks/router.py`
- Modify: `backend/app/tasks/schemas.py`
- Modify: `backend/app/workspace/service.py`
- Modify: `backend/app/workspace/router.py`
- Modify: `backend/app/workspace/schemas.py`
- Create: `backend/migrations/versions/0009_task_retry_idempotency.py`
- Test: `backend/tests/tasks/test_retry.py`
- Test: `backend/tests/test_phase2_migrations.py`

- [ ] **Step 1: 写失败测试**：覆盖终态允许列表、归属校验、运行中冲突、并发 retry 返回同一任务、`analysis_task_ids` 追加且保留 `scoring_profile`。
- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 增加 retry 幂等键/唯一约束和事务逻辑**，新增 `POST /api/v1/tasks/{task_id}/retry`。
- [ ] **Step 4: 运行 retry 与 migration 测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "feat: add idempotent task retry endpoint"`。

### Task 4: 按任务/版本隔离查询结果

**Files:**
- Modify: `backend/app/reporting/router.py`
- Modify: `backend/app/reporting/service.py`
- Modify: `backend/app/workspace/router.py`
- Modify: `backend/app/workspace/schemas.py`
- Test: `backend/tests/reporting/test_task_isolation.py`
- Test: `backend/tests/workspace/test_sessions.py`

- [ ] **Step 1: 写失败测试**：新任务未生成候选/BI 时不得返回旧任务结果；跨用户/跨任务访问返回 404。
- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 为候选、报告、导出和 session summary 增加 task/version 约束，禁止旧版本回退。**
- [ ] **Step 4: 运行测试确认通过并提交**：`git commit -m "fix: isolate analysis artifacts by task"`。

### Task 5: 前端阶段状态、错误消息和重跑按钮

**Files:**
- Modify: `src/state/taskEvents.ts`
- Modify: `src/types.ts`
- Modify: `src/api/contracts.ts`
- Modify: `src/api/tasks.ts`
- Modify: `src/api/taskStream.ts`
- Modify: `src/hooks/useWorkspace.ts`
- Modify: `src/hooks/useTaskStream.ts`
- Modify: `src/components/ChatArea.tsx`
- Modify: `src/App.tsx`
- Test: `src/state/taskEvents.test.ts`
- Test: `src/hooks/useWorkspace.test.tsx`
- Test: `src/components/ChatArea.test.tsx`
- Test: `src/api/client.test.ts`

- [ ] **Step 1: 写失败测试**：真实事件映射阶段、平台进度、终态错误消息幂等、重跑按钮状态和旧 BI 清空隔离。
- [ ] **Step 2: 运行前端测试确认失败**：`npm run test -- src/state/taskEvents.test.ts src/hooks/useWorkspace.test.tsx src/components/ChatArea.test.tsx`。
- [ ] **Step 3: 实现 reducer 与 hook**：状态来自实际事件；按 task ID/version 清理和恢复数据；重跑调用 API 并切换新 SSE；`taskStream.ts`/`useTaskStream.ts` 保证 Last-Event-ID 重连、重复/过期事件幂等。
- [ ] **Step 4: 实现 UI**：阶段链、当前阶段、真实进度、错误 assistant 消息和用户消息“再次执行”，保持现有紫色按钮/图标风格。
- [ ] **Step 5: 运行前端测试、TypeScript 和构建确认通过**。
- [ ] **Step 6: 提交**：`git commit -m "feat: show task phases and retry messages"`。

### Task 6: 全量验证与本地迁移

**Files:**
- Verify: `backend/migrations/versions/0009_task_retry_idempotency.py`
- Verify: `backend/app/reporting/router.py`、`backend/app/reporting/service.py`、`backend/app/reporting/exporter.py`
- Verify: all changed files

- [ ] **Step 1:** 在本地 MySQL 执行 `./.venv/bin/alembic upgrade head`。
- [ ] **Step 2:** 执行后端全量 `./.venv/bin/pytest -q`。
- [ ] **Step 3:** 执行前端全量 `npm run test -- --run`、`npm run lint`、`npm run build`。
- [ ] **Step 4:** 检查 `git diff --check`、工作区状态和关键 SSE/重跑测试输出。
- [ ] **Step 5:** 汇总实际验证结果，不声称未验证的 MCP 外部服务结果。
