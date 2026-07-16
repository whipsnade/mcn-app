# KOL Insight AI

面向品牌用户的 KOL 智能筛选与分析平台。当前第一阶段已经形成 React + FastAPI + MySQL 的可运行纵向切片，支持模拟登录、新用户 1000 积分、用户数据隔离、历史会话与消息恢复，并保留原型的三栏界面、Indigo/Slate 配色、Lucide 图标和右侧固定 BI 区域。

## 当前能力

- 模拟手机短信与微信登录，访问令牌保存在内存，刷新会话使用 HttpOnly Cookie。
- 新用户只获得一次 1000 积分，钱包变更写入不可变账本。
- KOL 会话按用户隔离，支持新建、标星、重命名、追加消息和刷新恢复。
- 渠道、品类、目标人群和预算作为 KOL 筛选条件持久化。
- 积分预留、结算和失败释放状态机；每次成功 MCP 工具调用的后续计费单位固定为 10 积分。
- 充值与真实支付暂未开放，当前入口只显示说明，不能修改积分。

第二阶段已接入腾讯 Token Plan 大模型与 DataTap MCP 网关、异步流式任务、KOL 候选清单、收藏/对比和版本化 BI 报告。除登录外，模型与 MCP 只使用真实服务；凭据仅从本地运行环境注入，不写入仓库。

## 技术架构

- 前端：React 19、TypeScript、Vite、Tailwind CSS、Motion、Recharts。
- 后端：Python 3.11/3.12、FastAPI、SQLAlchemy Async、Alembic。
- 数据库：MySQL 8，字符集 `utf8mb4`。
- 测试：Vitest、pytest、Playwright。

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

## 安全约束

- `.env` 中的 MySQL 密码、JWT 密钥、腾讯模型密钥和 DataTap Token 均不得提交到 Git。
- `.env.example` 只能保留占位符，不能出现真实凭证。
- `AUTH_MODE=mock` 只允许用于 `development` 和 `test`。后端在 `production` 环境检测到 mock 认证会拒绝启动。
- 测试账户只能访问独立测试库，禁止赋予生产或开发库权限。
- 普通用户的会话、消息和钱包查询必须始终带当前认证用户条件。
- 运行、恢复、回滚与真实供应商授权步骤见 [第二阶段运行手册](docs/runbooks/phase-2-runtime.md)。

## 项目目录

```text
backend/        FastAPI 模块化单体、迁移与 pytest
src/            React 前端、API Client 与工作区状态
e2e/            Playwright 端到端测试
docs/           架构设计与分阶段实施计划
```
