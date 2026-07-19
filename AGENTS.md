# AGENTS.md

本文件面向 AI 编码代理，概述项目结构、开发命令与必须遵守的约定。详细信息以仓库内的 `README.md`、`docs/runbooks/phase-2-runtime.md` 和各模块源码为准。

## 项目概述

KOL Insight AI：面向品牌用户的网红 KOL 与 MCN 营销效果智能筛选、分析与 BI 报告平台。

- 前端：React 19 + TypeScript + Vite + Tailwind CSS 4 + Motion + Recharts，端口 5173。
- 后端：Python 3.11/3.12 + FastAPI 模块化单体 + SQLAlchemy Async（asyncmy）+ Alembic，端口 8000。
- 数据库：MySQL 8，字符集 `utf8mb4`。
- 外部服务：腾讯 Token Plan 大模型（`deepseek-v4-pro`）与 DataTap MCP 网关。除登录外，模型与 MCP 只使用真实服务，不做 mock。
- 测试：Vitest（前端单测）、pytest（后端）、Playwright（E2E）。

业务要点：模拟短信/微信登录（访问令牌在内存，刷新令牌走 HttpOnly Cookie）、新用户一次性 1000 积分、不可变账本、会话按用户隔离、积分预留/结算/失败释放状态机、每次 MCP 工具调用固定计费 10 积分。充值与真实支付未开放。

任务双模式（`analysis_tasks.kind`）：每条消息创建任务时由 `orchestration/routing.py` 的 `classify_task_kind` 按意图路由——命中 KOL 意图词且会话有品类走 `pipeline`（固定 DAG 规划 → 候选 Top10 → 版本化 BI 报告 → xlsx 导出），其余（行业/品牌/开放分析、无品类会话）走 `agent`（迭代式工具调用循环 → 版本化自由分析报告）。agent 模式复用同一套计费、租约、恢复与 SSE 底座，轨迹持久化在 `plan_json`（`agent_trajectory_v1`）。

## 项目结构

```text
backend/            FastAPI 后端
  app/
    api/router.py   /api/v1 路由聚合（auth、users、wallet、sessions、tasks、reporting）
    core/           配置（pydantic-settings）、错误、安全、日志脱敏
    db/             SQLAlchemy Base、引擎与会话
    identity/       用户、模拟认证提供商、JWT
    billing/        钱包、账本、积分预留/结算
    workspace/      KOL 会话与消息（新建、标星、重命名、恢复）
    tasks/          异步流式任务运行时、事件、恢复、幂等
    model/          腾讯 Token Plan 模型适配层
    mcp_gateway/    DataTap MCP 客户端、工具注册/校验、计费记账
    orchestration/  计划器、路由、上下文与导出契约；loop.py 是 agent 迭代循环契约
    reporting/      候选清单、评分、收藏/对比、版本化 BI 报告、导出；blocks.py/analysis_reports.py 是自由报告
  migrations/       Alembic 迁移（0001_… 顺序编号）
  tests/            pytest，目录结构与 app/ 对齐
src/                React 前端
  api/              API Client 与类型契约
  auth/             AuthProvider
  components/       页面组件（组件名.test.tsx 同目录单测）
  hooks/            useWorkspace、useTaskStream
  state/            任务事件状态
  test/             Vitest setup、fixtures、SSE 模拟
e2e/                Playwright 端到端测试
docs/               架构设计、分阶段计划、运行手册（runbooks）、QA 记录
server.ts           旧的 Express/Gemini 原型，仅 dev:legacy 保留，不是当前架构
```

`package.json` 中的 `server.ts`、`@google/genai` 属于遗留原型；当前系统以后端 FastAPI 为准，新功能不要改 server.ts。

## 本地启动

前置：Node.js + npm、Python 3.11/3.12、运行中的 MySQL 8。

1. 建库（开发库 `kol_insight` 与测试库 `kol_insight_test`，均 `utf8mb4`），并按 README 创建只能访问测试库的 `kol_test` 账号。
2. `cp .env.example .env`，填写 MySQL 密码、随机 JWT 密钥（≥32 字符）、`TENCENT_PLAN_API_KEY`、`DATATAP_MCP_TOKEN`。
3. 后端依赖：`cd backend && python -m venv .venv && .venv/bin/pip install -e '.[dev]'`
4. 迁移：`cd backend && .venv/bin/alembic upgrade head`（测试库用 README 中的 `APP_ENV=test … alembic upgrade head` 命令单独迁移）。
5. 启动后端：`cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000`
6. 启动前端：`npm install && npm run dev`，访问 `http://127.0.0.1:5173`。Vite 将 `/api` 代理到 `127.0.0.1:8000`。
7. 开发环境短信验证码固定为 `000000`。

## 验证命令

改动后必须运行与改动范围对应的检查，全部通过才算完成。

```bash
# 后端（在 backend/ 目录下）
.venv/bin/ruff check app tests
.venv/bin/pytest -q

# 前端（仓库根目录）
npm run test     # Vitest 单测，范围是 src/
npm run lint     # 实际是 tsc --noEmit 类型检查
npm run build    # 生产构建

# E2E（首次需 npx playwright install chromium）
npm run test:e2e
```

## 代码约定

- Python：ruff，行宽 100，目标 `py311`。后端使用 async SQLAlchemy 2.0 风格与 pydantic-settings 配置。
- TypeScript：`tsc --noEmit` 作为 lint；路径别名 `@/*` 指向仓库根目录。React 组件与其测试文件同目录（`Xxx.tsx` / `Xxx.test.tsx`）。
- 数据库变更必须新增 Alembic 迁移（`backend/migrations/versions/`，沿用 `NNNN_描述.py` 编号格式），不可手改已合入的迁移。
- API 契约：前端类型集中在 `src/api/contracts.ts`，后端 schema 在各模块 `schemas.py`，两端改动需保持一致。
- agent 模式契约：自由报告块类型定义在 `backend/app/reporting/blocks.py`（前端 `ReportBlock` 与之镜像）；新事件 `report.updated`；查询端点 `GET /api/v1/analysis-reports/{id}`；会话 DTO 带 `latest_analysis_report`，任务 DTO 带 `kind`。
- 注释与文档：仓库内 Markdown 文档使用中文；代码注释可中英混用，保持与所在文件一致。

## 测试策略

- 后端 pytest：`backend/tests/conftest.py` 默认注入测试环境变量，固定使用独立测试库 `kol_insight_test` 与专用账号 `kol_test`；数据库 fixture 以事务回滚方式隔离每个用例，绝不写开发库。运行 pytest 前测试库需已迁移到 head。
- 前端 Vitest：jsdom 环境，setup 在 `src/test/setup.ts`，SSE 用 `src/test/fakeSse.ts` 模拟。
- Playwright：自动拉起 8000 端口 FastAPI（注入测试环境变量）与 5173 端口 Vite，覆盖 1440×900、1024×768、390×844 三种视口；`reuseExistingServer: false`，端口被占用会直接失败——运行前确认两个端口空闲。

## 配置与安全

- 所有密钥（MySQL 密码、JWT 密钥、`TENCENT_PLAN_API_KEY`、`DATATAP_MCP_TOKEN`）只放在未跟踪的 `.env`；`.env.example` 仅保留占位符，严禁写入真实凭证。
- `app/core/config.py` 在启动时做硬性校验：`TENCENT_PLAN_BASE_URL` 必须为已确认的腾讯端点、`MCP_CALL_POINTS` 与 `MCP_MAX_CALLS_PER_TASK` 必须为 10、密钥不得为空。
- `AUTH_MODE=mock` 仅允许 `development` 与 `test`；`production` 下检测到 mock 认证会拒绝启动。
- 测试账号 `kol_test` 只能访问 `kol_insight_test`，禁止授予开发库或生产库权限。
- 工具启用流程：远程发现的工具默认 quarantined；启用 = 在 `mcp_gateway/registry.py` 的 `DYNAMIC_TOOL_ALLOWLIST` 登记（内部名、审核描述、输出 Schema）并将 `review_status` 置 approved，启动时按实时签名复核，digest 变化会重新隔离。
- 普通用户的会话、消息、钱包查询必须始终带当前认证用户条件（用户数据隔离），新增查询时不得遗漏。

## 运行手册

第二阶段（模型 + MCP + BI 报告）的运行、恢复、回滚与供应商授权步骤见 `docs/runbooks/phase-2-runtime.md`。
