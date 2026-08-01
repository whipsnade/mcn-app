# Brainstorm 最终意图路由修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 仅在 GoalPlanner 明确返回 `execute` 时创建分析任务，Brainstorm 与已 ready 会话的任务路由中，任何无法执行的规划结果均转为会话追问。

**Architecture:** Brainstorm 和任务路由均保留 Planner 的原始动作，调用方分别处理 `execute`、`clarify`、`respond` 与异常。`clarify` 和无法规划时只生成 assistant 消息，不再以空 goals 隐式触发 `kol_selection` 默认任务；幂等命中继续复用既有任务。

**Tech Stack:** FastAPI、SQLAlchemy Async、Pydantic、pytest。

---

### Task 1: 锁定默认 KOL 回退回归

**Files:**

- Modify: `backend/tests/brainstorm/test_brainstorm.py`

- [x] **Step 1: 写失败测试**

覆盖 Planner 的 `clarify`、`respond`、异常、关闭开关且意图不明的路径，断言 `task_id` 为空、没有 `TaskGoal`，并在需追问时检查选项；另保留关闭开关但画像明确达人意图的正常执行覆盖。

- [x] **Step 2: 验证失败**

运行：`.venv/bin/pytest -q tests/brainstorm/test_brainstorm.py -k 'ready_plan and (clarify or respond or error or disabled)'`

结果：4 个断言均因旧逻辑创建默认 `kol_selection` 任务而失败。

### Task 2: 仅接受明确 execute

**Files:**

- Modify: `backend/app/brainstorm/service.py`
- Modify: `backend/app/tasks/router.py`
- Test: `backend/tests/brainstorm/test_brainstorm.py`
- Test: `backend/tests/tasks/test_enforce_create_task.py`

- [x] **Step 1: 保留 Planner 原始动作**

`_plan_task_goals` 返回 `GoalPlannerOutput | None`，不再把 `clarify/respond` 压缩为 `None`。

- [x] **Step 2: 按动作创建或追问**

仅 `execute` 生成 Planner goal specs 并调用 `TaskService.create`；`clarify` 使用 Planner 问题；`respond` 仅写回复标记；异常或空结果统一询问品牌分析、活动分析或达人圈选。关闭 enforce 开关时仅画像明确为达人意图才生成显式 KOL Goal，不能将未知意图默认成 KOL。

已 ready 会话经 `/tasks` 再次发送消息时，也使用同一安全语义：Planner 异常或关闭时返回 `TaskOutcomeClarify`，不会落入旧单 Goal 默认值。

- [x] **Step 3: 验证通过**

运行：`.venv/bin/pytest -q tests/brainstorm/test_brainstorm.py -k 'ready_plan and (execute or multi_goal or clarify or respond or error or disabled)'`

结果：定向 6 passed；完整 Brainstorm 36 passed；加任务规划路由回归共 55 passed。
