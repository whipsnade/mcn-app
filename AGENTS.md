# AGENTS.md

本文件面向 AI 编码代理，概述项目结构、开发命令与必须遵守的约定。详细信息以仓库内的 `README.md`、`docs/runbooks/phase-2-runtime.md` 和各模块源码为准。

> **新会话预热**：开始工作前先读 `changelog/` 目录最新 2-3 篇按日期的变更日志（改了什么、为什么、遗留事项），可快速建立上下文；约定见 `changelog/README.md`。

## 项目概述

KOL Insight AI：面向品牌用户的网红 KOL 与 MCN 营销效果智能筛选、分析与 BI 报告平台。

- 前端：React 19 + TypeScript + Vite + Tailwind CSS 4 + Motion + Recharts，端口 5173。
- 后端：Python 3.11/3.12 + FastAPI 模块化单体 + SQLAlchemy Async（asyncmy）+ Alembic，端口 8000。
- 数据库：MySQL 8，字符集 `utf8mb4`。
- 外部服务：腾讯 Token Plan 大模型（`deepseek-v4-pro`）与 DataTap MCP 网关。除登录外，模型与 MCP 只使用真实服务，不做 mock。
- 测试：Vitest（前端单测）、pytest（后端）、Playwright（E2E）。

业务要点：模拟短信/微信登录（访问令牌在内存，刷新令牌走 HttpOnly Cookie）、新用户一次性 1000 积分、不可变账本、会话按用户隔离、积分预留/结算/失败释放状态机、每次 MCP 工具调用固定计费 10 积分。充值与真实支付未开放。

管理端能力：`/api/v1/admin` 提供管理员（`users.role == "admin"`，依赖 `identity/dependencies.py` 的 `require_admin`）账号管理接口——用户列表/搜索/渠道筛选、创建、编辑、软禁用（吊销全部刷新会话）、积分人工调整（账本 `kind="admin_adjust"`，支持 `Idempotency-Key`）与单用户积分流水；所有写操作落 `admin_audit_logs` 审计表（手机号掩码）。

任务单一 agent 模式（`analysis_tasks.kind` 固定为 `"agent"`）：每条消息创建的任务都走 `orchestration/loop.py` 的迭代式工具调用循环，产出版本化自由分析报告。循环由 BI 数据项驱动：`orchestration/bi_requirements.py` 定义 8 项数据项（brand_voice/exposure/engagement/sentiment/hot_words/voice_trend/audience_profile/kol_leaderboard）及其 `source_tools` 映射，`AgentLoopContext.required_metrics` 记录待覆盖项；模型 finish 前服务端做覆盖门禁，缺失数据项回喂继续采集（streak 上限 3 放行；工具 settled 但返回空视为已满足；覆盖判定除 `source_tools` 工具名映射外还支持 `content_signal` 内容特征兜底——如 voice_trend 认可任意 insight 统计工具返回的按天日期序列）。同一工具累计 2 次 settled 但返回空数据后熔断（拒绝重复调用并回喂，连续熔断 3 次按现有证据收尾）。循环不设调用次数上限，仅当钱包可用余额不足一次 10 积分调用（`InsufficientPointsError`）时停止，任务进入 `insufficient_balance` 终态（发 `task.failed`，code="insufficient_balance"）；余额不足前已采集证据时仍生成分析报告。轨迹持久化在 `plan_json`（`agent_trajectory_v1`）。历史表（bi_reports/task_candidates/kols 等）保留但不再写入；favorites 保留只读（列表/取消收藏）。

会话生命周期（空白会话 + brainstorm 澄清）：新建会话不再走表单，`POST /sessions` 空 body 直接创建空白会话（标题「新会话N」）。会话画像未 ready 前，用户消息走 `POST /sessions/{id}/brainstorm`（同步问答，零积分）：`brainstorm/parameters.py` 定义 8 项 MCP 参数关键字表（brand/category/platforms/audience/period/kol_filters/goal/region），`BRAINSTORM_PROMPT` 驱动模型逐项提炼，信息不足时一次一问（优先 2-4 个选项）；画像落 `sessions.filters_snapshot["brainstorm_profile"]`，title_suggestion 提炼会话名，`ready=true` 时后端内联创建 agent 任务并写回 brand/category/platforms/target_audience 标量列。画像 ready 后消息走原有 `POST /sessions/{id}/tasks`。任务循环上下文经 `AgentLoopContext.param_profile` 注入画像（period 合法时覆写 `requested_period`），优先级高于消息文本推断。

行业属性与快捷功能（/api/v1/quick）：用户带 `industries` JSON 多值字段（迁移 0018，存量回填「美食」，admin 可编辑）。会话列表 2x2 快捷按钮提供四个全局功能（按用户行业过滤、不绑定会话）：达人推荐（`GET /quick/kol-recommendations`，预算 1万~50万滑动条、top50、单达人报价 ≤ 预算）、达人/活动评估（`POST /quick/evaluate`，xlsx/csv ≤5MB 上传给模型分析，0 积分）、小红书/抖音前十爆贴（`GET /quick/top-posts`，近 30 天互动数倒序）。快捷调用是同步 HTTP 非任务路径：爆贴/达人推荐/达人详情由 `quick/agent.py` 的模型驱动同步小循环决策（代码只组装场景 prompt + 护栏：白名单/Schema 校验、上限 8 轮、连续 2 次无效决策报错；finish 结果按 feature 输出契约校验），单次成本随模型选择的调用次数浮动；每次 MCP 调用仍固定 10 积分，经 `QuickCallService.call_tool` 轻量计费（`quick_mcp_calls` 留痕、`reference_type="quick_mcp_call"`），悬挂预留由恢复循环清扫；余额不足返回 409 `INSUFFICIENT_POINTS`。预算过滤（报价 ≤ 预算、无报价排最后）与 top50 截断留在端点层（纯代码排序过滤），模型结果经归一化函数兜底清洗。

Prompt 学习日志与成功案例回放：所有模型调用在 `TencentPlanAdapter.complete_json/stream_text` 统一出口写入 `model_prompt_logs` 表（迁移 0019；完整 messages/response 为 MEDIUMTEXT，purpose/tags 标签化，status=success/invalid/failed + error_code，token 用量与耗时；写日志走独立 SessionFactory 会话，异常只记 warning 绝不阻塞主流程）。调用点经请求的 `log_context`（user_id/session_id/task_id/tags）透传上下文。`model/exemplars.py` 的 `find_success_exemplars` 按 purpose + status=success + tags 交集检索最近成功记录（截断 ~1500 字符、剔除 key/token 特征字段），注入 agent_loop、quick 小循环与 brainstorm 的 user content JSON 的 `"exemplars"` 键，供模型参考工具选择与参数写法。

## 项目结构

```text
backend/            FastAPI 后端
  app/
    api/router.py   /api/v1 路由聚合（auth、users、wallet、sessions、admin、tasks、reporting）
    core/           配置（pydantic-settings）、错误、安全、日志脱敏
    db/             SQLAlchemy Base、引擎与会话
    identity/       用户、模拟认证提供商、JWT；dependencies.py 含 require_admin
    billing/        钱包、账本、积分预留/结算/管理员调整（admin_adjust）
    admin/          管理端账号与积分管理、审计日志（/api/v1/admin）
    workspace/      KOL 会话与消息（空白新建、标星、重命名、恢复；metadata 白名单）
    brainstorm/     需求澄清（关键字表 parameters.py、画像提炼 service、/sessions/{id}/brainstorm）
    quick/          快捷功能（模型小循环 agent.py、同步计费护栏 service.py、/quick/* 端点：达人推荐/达人详情/爆贴/评估上传）
    tasks/          异步流式任务运行时、事件、恢复、幂等
    model/          腾讯 Token Plan 模型适配层；prompt_logs.py 是 prompt 学习日志写入口，exemplars.py 是成功案例回放检索
    mcp_gateway/    DataTap MCP 客户端、工具注册/校验、计费记账
    orchestration/  路由、上下文与迭代循环；loop.py 是 agent 迭代循环契约，bi_requirements.py 是 BI 数据项契约
    reporting/      收藏（只读）与版本化自由分析报告；blocks.py/analysis_reports.py 是自由报告
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
- agent 模式契约：自由报告块类型定义在 `backend/app/reporting/blocks.py`（前端 `ReportBlock` 与之镜像）；新事件 `report.updated`；查询端点 `GET /api/v1/analysis-reports/{id}`；会话 DTO 带 `latest_analysis_report`，任务 DTO 带 `kind`（固定 `"agent"`）。`AgentLoopContext` 用 `required_metrics` 跟踪 BI 数据项覆盖（已无 remaining_calls）；SSE 工具进度事件 `step_total` 为 null（无调用次数分母）；余额不足终态为 `insufficient_balance`（`task.failed` code="insufficient_balance"）。
- 注释与文档：仓库内 Markdown 文档使用中文；代码注释可中英混用，保持与所在文件一致。

## 测试策略

- 后端 pytest：`backend/tests/conftest.py` 默认注入测试环境变量，固定使用独立测试库 `kol_insight_test` 与专用账号 `kol_test`；数据库 fixture 以事务回滚方式隔离每个用例，绝不写开发库。运行 pytest 前测试库需已迁移到 head。
- 前端 Vitest：jsdom 环境，setup 在 `src/test/setup.ts`，SSE 用 `src/test/fakeSse.ts` 模拟。
- Playwright：自动拉起 8000 端口 FastAPI（注入测试环境变量）与 5173 端口 Vite，覆盖 1440×900、1024×768、390×844 三种视口；`reuseExistingServer: false`，端口被占用会直接失败——运行前确认两个端口空闲。

## 配置与安全

- 所有密钥（MySQL 密码、JWT 密钥、`TENCENT_PLAN_API_KEY`、`DATATAP_MCP_TOKEN`）只放在未跟踪的 `.env`；`.env.example` 仅保留占位符，严禁写入真实凭证。
- `app/core/config.py` 在启动时做硬性校验：`MCP_CALL_POINTS` 必须为 10、密钥不得为空。模型供应商可自由配置：`TENCENT_PLAN_BASE_URL` / `TENCENT_PLAN_MODEL` / `TENCENT_PLAN_API_KEY` 支持任意 OpenAI 兼容端点（腾讯 Token Plan、月之暗面 Kimi 等）；`TENCENT_PLAN_REASONING_EFFORT`（low/high/max）为可选思考深度，仅 k3 等推理模型生效，缺省不向端点发送该参数。
- `AUTH_MODE=mock` 仅允许 `development` 与 `test`；`production` 下检测到 mock 认证会拒绝启动。
- 测试账号 `kol_test` 只能访问 `kol_insight_test`，禁止授予开发库或生产库权限。
- 工具启用流程：远程发现的工具默认 quarantined；启用 = 在 `mcp_gateway/registry.py` 的 `DYNAMIC_TOOL_ALLOWLIST` 登记（内部名、审核描述、输出 Schema）并将 `review_status` 置 approved，启动时按实时签名复核，digest 变化会重新隔离。
- 普通用户的会话、消息、钱包查询必须始终带当前认证用户条件（用户数据隔离），新增查询时不得遗漏。

## 运行手册

第二阶段（模型 + MCP + 自由分析报告）的运行、恢复、回滚与供应商授权步骤见 `docs/runbooks/phase-2-runtime.md`。
