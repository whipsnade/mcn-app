# KOL Insight AI

面向品牌用户的 KOL 智能筛选与分析平台。当前架构为**模型主导的统一 Agent 运行时（Agent Runtime v3）**：模型在可信执行内核之上自主完成澄清、工具选择、失败处理、钻取与产物生成，代码负责能力边界、安全、计费、状态、证据与展示。一次性切换已于 2026-08-02 完成（设计 `docs/superpowers/specs/2026-08-02-model-led-agent-runtime-design.md`）。

前端 React 19 + TypeScript + Vite + Tailwind + Motion + Recharts；后端 Python + FastAPI + SQLAlchemy Async + Alembic；数据库 MySQL 8。原型的三栏界面、Indigo/Slate 配色、Lucide 图标和右侧固定 BI 区域保留。

## 当前能力

- 模拟手机短信与微信登录，访问令牌保存在内存，刷新会话使用 HttpOnly Cookie。
- 新用户只获得一次 1000 积分，钱包变更写入不可变账本。
- **智能会话**：每条用户消息创建一个独立、可恢复、可审计的 Agent Run；会话按用户隔离，支持新建、重命名、软删除、刷新恢复。会话 Agent 自主完成澄清、答疑、工具选择、失败处理与产物生成。
- **三个固定 BI 模块**：品牌分析 / 活动分析 / 达人（达人内含「KOL 分析 / 圈选达人」两个子 Tab），产物为不可变强类型 Artifact（brand_report_v3 / campaign_report_v2 / kol_selection_v3 / kol_analysis_v2 / kol_detail_v2），支持历史版本与未读圆点；品牌与圈选支持零积分 Excel 导出。
- **达人详情**：点击圈选达人创建轻量 Run，产物经 Reviewer 发布 `kol_detail_v2`，带 24 小时会话级缓存、主页链接与最近 5 条热帖。
- **澄清与暂停恢复**：信息不足时 Agent 主动提问（选项 chips）；Attempt 达到 30 分钟或 50 次模型决策上限后暂停，用户可显式继续。
- **积分与计费**：积分预留、结算和失败释放状态机；每次 DataTap MCP 工具调用固定 10 积分，模型/历史/计算/Artifact 工具零积分；余额不足由 Agent 说明限制或用已有证据交付。
- **审计与可追溯**：每个正式数值字段都能递归追溯到当前会话内的 Evidence；Reviewer 独立复核正式产物（最多打回两次）；`unknown` MCP 调用禁止自动重放，经恢复核对后结算/释放。
- 充值与真实支付暂未开放，当前入口只显示说明，不能修改积分。

模型与 MCP 使用腾讯 Token Plan 大模型与 DataTap MCP 网关的真实服务（除登录外不做 mock）；凭据仅从本地运行环境注入，不写入仓库。

> 2026-08-02 起旧的 Brainstorm、GoalPlanner、任务 Goal 与快捷功能（达人推荐 / 活动评估 / 小红书爆贴 / 抖音爆贴四个独立入口）已随一次性切换移除；这些分析仍可在普通会话中自然语言发起，由会话 Agent 自主调用同类能力并放入固定 BI 模块。旧执行表保留为只读 legacy ORM，首次切换不 drop 旧表。

## 技术架构

- 前端：React 19、TypeScript、Vite、Tailwind CSS、Motion、Recharts。
- 后端：Python 3.11/3.12、FastAPI 模块化单体、SQLAlchemy Async、Alembic。
- 数据库：MySQL 8，字符集 `utf8mb4`。
- 测试：Vitest、pytest、Playwright。
- 核心运行时：`backend/app/agent_runtime/`（会话 / Run / Attempt / Step / 工具运行时 / 事件 / 恢复 / Reviewer）与 `backend/app/agent_artifacts/`（强类型产物 / lineage / Draft / 不可变 Version / Excel 导出）。四个 Agent Profile：`session_analyst_v1`、`artifact_reviewer_v1`、`kol_detail_v1`、`utility_v1`；统一动作协议 `ask_user / call_tool / submit_review / complete`；API 前缀 `/api/v1/agent`，SSE 事件流带 Last-Event-ID 断线续传。

## 本地启动

准备 Node.js、npm、Python 3.11 或 3.12，以及正在运行的 MySQL 8。

1. 创建开发库和测试库：

```bash
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS kol_insight CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE DATABASE IF NOT EXISTS kol_insight_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

为自动化测试创建仅能访问测试库的本地账号（密码是仓库内公开的测试专用值，不得复用于其他环境）：

```bash
mysql -u root -p -e "CREATE USER IF NOT EXISTS 'kol_test'@'%' IDENTIFIED BY 'test-only-password'; GRANT ALL PRIVILEGES ON kol_insight_test.* TO 'kol_test'@'%';"
```

2. 创建本地配置，并填写本机数据库密码、随机 JWT 密钥、腾讯模型密钥和 DataTap MCP 令牌：

```bash
cp .env.example .env
```

3. 创建 Python 虚拟环境并安装后端依赖：

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd ..
```

4. 执行数据库迁移：

```bash
cd backend
.venv/bin/alembic upgrade head
APP_ENV=test AUTH_MODE=mock MYSQL_DATABASE=kol_insight_test MYSQL_USER=kol_test MYSQL_PASSWORD=test-only-password JWT_SECRET=test-only-jwt-secret-at-least-32-characters .venv/bin/alembic upgrade head
cd ..
```

5. 启动后端 API：

```bash
cd backend
.venv/bin/uvicorn app.main:app --reload --port 8000
```

6. 新开终端，安装并启动前端：

```bash
npm install
npm run dev
```

访问 `http://127.0.0.1:5173`。开发环境短信验证码固定为 `000000`，点击“获取验证码”后界面会自动填充。

## 验证命令

后端静态检查与测试：

```bash
cd backend
.venv/bin/ruff check app tests
.venv/bin/pytest -q
```

前端单测、类型检查与生产构建：

```bash
npm run test
npm run lint
npm run build
```

首次运行 E2E 前安装 Chromium，之后执行完整浏览器流程：

```bash
npx playwright install chromium
npm run test:e2e
```

Playwright 会自动启动 8000 端口的 FastAPI 和 5173 端口的 Vite，并固定使用隔离的 `kol_insight_test` 与测试专用账号，不写入开发库。测试会依次覆盖 1440×900、1024×768 和 390×844 三种视口；如果端口已被占用会直接失败，避免误测其他版本的服务。

真实模型 + 真实 DataTap 的 UAT（`backend/tests/integration/test_agent_runtime_real.py`）默认被 pytest 跳过，单独入口：

```bash
cd backend
./scripts/run_real_agent_uat.sh
```

## 安全约束

- `.env` 中的 MySQL 密码、JWT 密钥、腾讯模型密钥和 DataTap Token 均不得提交到 Git。
- `.env.example` 只能保留占位符，不能出现真实凭证。
- `AUTH_MODE=mock` 只允许用于 `development` 和 `test`。后端在 `production` 环境检测到 mock 认证会拒绝启动。
- 测试账户只能访问独立测试库，禁止赋予生产或开发库权限。
- 普通用户的会话、消息、Run、Evidence、Artifact 和达人缓存查询必须始终带当前认证用户条件。
- 运行、恢复、回滚与真实供应商授权步骤见 [第二阶段运行手册](docs/runbooks/phase-2-runtime.md)；一次性切换清单与发布阻断条件见 [Agent Runtime v3 切换清单](docs/runbooks/agent-runtime-v3-cutover.md)。

## 项目目录

```text
backend/        FastAPI 模块化单体、agent_runtime / agent_artifacts、迁移与 pytest
src/            React 前端、Agent API Client 与 Run SSE 状态
e2e/            Playwright 端到端测试
docs/           架构设计与分阶段实施计划、运行手册
```
