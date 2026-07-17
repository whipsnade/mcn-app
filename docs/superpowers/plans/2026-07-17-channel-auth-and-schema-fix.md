# 渠道授权与数据库结构修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复历史会话渠道校验导致的分析失败，为 Mock 登录用户补齐全部平台授权，并将本地数据库升级到当前代码所需的迁移版本。

**Architecture:** 在 ContextBuilder 构建规划上下文时，将会话平台与用户已授权平台取交集，规划器只接收有效渠道；IdentityService 在每次登录时幂等补齐平台权限；数据库通过现有 Alembic 0013 迁移补齐任务创建幂等字段。不会自动执行远程 MCP 调用或自动迁移生产数据库。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、pytest、MySQL。

## Task 1：历史会话渠道清洗

**Files:**
- Modify: `backend/app/orchestration/context.py`
- Test: `backend/tests/orchestration/test_context.py`

- [ ] 写测试：会话包含未授权微博、用户仅授权小红书/抖音时，上下文只保留小红书/抖音。
- [ ] 运行测试确认当前实现失败。
- [ ] 在 ContextBuilder 中构建有效渠道并同步更新 SessionBrief。
- [ ] 运行上下文测试确认通过。

## Task 2：Mock 登录补齐全部平台

**Files:**
- Modify: `backend/app/identity/service.py`
- Test: `backend/tests/identity/test_mock_auth.py`

- [ ] 写测试：Mock 短信登录后的 `/users/me` 返回五个平台；重复登录不产生重复权限。
- [ ] 运行测试确认当前默认三平台实现失败。
- [ ] 将默认平台扩展为小红书、抖音、哔哩哔哩、微博、微信，并在每次登录时幂等补齐缺失权限。
- [ ] 运行身份测试确认通过。

## Task 3：本地数据库升级与回归

**Files:**
- Existing migration: `backend/migrations/versions/0013_task_create_idempotency.py`
- Test evidence: `docs/qa/task9-regression.md`

- [ ] 执行 `alembic upgrade head`，确认 `alembic_version=0013_task_create_idempotency`。
- [ ] 确认 `analysis_tasks` 存在两个幂等字段及唯一索引。
- [ ] 运行后端非真实供应商测试、身份/上下文专项测试与前端测试。
- [ ] 检查工作区并提交代码变更。
