# 新建会话可选字段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让新建会话仅要求行业与初始提问，并新增 KOL 名称搜索。

**Architecture:** 前端将行业改为固定选择器，提交时规范化可选字段；后端允许品牌、渠道和目标人群为空并保留 KOL 名称于筛选快照；Planner 只对已提供的可选条件生成默认筛选参数。

**Tech Stack:** React/TypeScript、FastAPI/Pydantic、SQLAlchemy、Alembic、Vitest、pytest。

## Task 1: 表单与前端请求契约

**Files:** `src/components/NewSessionModal.tsx`、`src/components/NewSessionModal.test.tsx`、`src/App.tsx`、`src/api/contracts.ts`、`src/hooks/useWorkspace.test.tsx`

- [ ] 先新增失败测试：只填写行业“美妆”和提问可提交；缺行业或提问不可提交；KOL 名称随 `filters.kol_name` 发送。
- [ ] 将 `category` 渲染为餐饮、茶饮、美妆、护肤的必填 `<select>`；新增可选 KOL 名称输入；移除品牌、渠道和目标人群的 `required` 与渠道最少选择限制。
- [ ] 在 `App.handleCreateSession` 发送 `brand ?? ""`、空渠道数组、`target_audience ?? ""` 和 `filters: { kol_name }`，保持现有紫色视觉组件。
- [ ] 运行 `npm run test -- NewSessionModal useWorkspace`，预期通过。

## Task 2: Python 会话契约、模型与迁移

**Files:** `backend/app/workspace/schemas.py`、`backend/app/workspace/models.py`、`backend/app/workspace/service.py`、`backend/migrations/versions/0008_optional_session_filters.py`、`backend/tests/workspace/test_sessions.py`

- [ ] 先写 API 失败测试：仅行业与提问成功；遗漏行业或提问返回 422；筛选快照保留 `kol_name`。
- [ ] 将 `SessionCreate.brand`、`platforms`、`target_audience` 改为可选（服务层规范为空字符串/空数组），`category` 与 `initial_query` 保持必填。
- [ ] 修改数据库列使品牌和目标人群可空，迁移可逆；标题依次使用“品牌-活动”、“品牌”、“活动”、`{行业} KOL 筛选`。
- [ ] 运行 `MODEL_PROVIDER=fake .venv/bin/pytest tests/workspace/test_sessions.py -q` 与 `alembic upgrade head`。

## Task 3: Planner 空值兼容与全链路验证

**Files:** `backend/app/orchestration/schemas.py`、`backend/app/orchestration/planner.py`、`backend/tests/orchestration/test_context.py`、`backend/tests/orchestration/test_planner.py`

- [ ] 先写失败测试：空品牌、空渠道、空目标人群仍可构建 PlannerContext，且不会产生品牌或人群默认筛选参数。
- [ ] 放宽 `SessionBrief` 的可选字段约束；只在字段有值时生成对应默认条件；保持行业进入上下文。
- [ ] 运行相关 pytest、Ruff、前端 Vitest，并用“美妆 + 初始提问”创建会话进行本地验证。
