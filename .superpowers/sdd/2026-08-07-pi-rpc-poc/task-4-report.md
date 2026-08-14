# Task 4 报告：透明 DataTap MCP Extension 与 Evidence 旁路

## Task 与交付结论

完成方案 A 的透明 DataTap MCP Extension 与 Evidence 旁路：Pi 直接、透明调用 DataTap，
每次工具调用以零积分旁路沉淀为 AgentStep + AgentToolCall + EvidenceItem。工具名、
参数、原始成功结果、错误、空结果、超时全部原样给 Pi，仅新增顶层 `_runtime_metadata`；
不自动重试、拆分、改参、换工具、熔断、意图路由。token 只存在于 HTTP Authorization 头。
未接入 DataTap 真实调用、Producer、正式 Runtime、积分或方案 B/C。

## 修改文件

- `pi-runtime/src/redaction.ts`（新增）
- `pi-runtime/src/extensions/datatap-mcp.ts`（新增）
- `pi-runtime/src/extensions/poc-runtime.ts`（新增）
- `pi-runtime/src/http/client.ts`（新增）
- `pi-runtime/tests/redaction.test.ts`、`tests/datatap-mcp.test.ts`、`tests/http-client.test.ts`（新增）
- `pi-runtime/package.json`、`package-lock.json`（新增 typebox 依赖）
- `backend/app/pi_runtime_poc/schemas.py`（PiToolStarted 加 call_id、响应 DTO、fail status）
- `backend/app/pi_runtime_poc/service.py`（新增）
- `backend/app/pi_runtime_poc/router.py`（新增）
- `backend/app/api/router.py`（挂载 pi-poc router）
- `backend/tests/pi_runtime_poc/test_evidence_ingest.py`（新增）
- `changelog/2026-08-07.md`、`.superpowers/sdd/2026-08-07-pi-rpc-poc/task-4-report.md`

## 红灯证据

- 前端：实现前 `npm test -- datatap-mcp.test.ts redaction.test.ts` 因模块缺失 collection fail。
- 后端：实现前 `pytest --confcutdir ... test_evidence_ingest.py` 报
  `ModuleNotFoundError: No module named 'app.pi_runtime_poc.service'`。

## 绿灯命令与准确结果

- `cd pi-runtime && npm test` → **22 passed**（redaction 4 / datatap-mcp 6 / http-client 4 / rpc-probe 8）。
- `cd pi-runtime && npm run typecheck` → 通过。
- `cd backend && .venv/bin/pytest --confcutdir=tests/pi_runtime_poc tests/pi_runtime_poc/test_evidence_ingest.py -q`
  → **8 passed**。
- `cd backend && TENCENT_PLAN_API_KEY=<占位> DATATAP_MCP_TOKEN=<占位> .venv/bin/pytest tests/pi_runtime_poc -q`
  → **28 passed**（test_auth/test_config/test_rpc/test_evidence_ingest 全绿）。
- `cd backend && .venv/bin/ruff check app/pi_runtime_poc tests/pi_runtime_poc app/api/router.py`
  → **All checks passed**。
- app 加载 openapi 确认 `/api/v1/internal/pi-poc/runs/{run_id}/tool-calls/{start|settle|fail}`
  三个端点注册。

## 安全/数据隔离核对

- 后端测试明确用 `sqlite+aiosqlite:///:memory:`（`MEDIUMTEXT` 编译为 TEXT），
  不连接 kol_insight / kol_insight_test / kol_insight_pi_poc 任何 MySQL 库。
- token 只在 `Authorization: Bearer` 头；`datatap-mcp.test.ts` 断言审计 body 不含 token；
  `http-client.test.ts` 断言请求体不含 token。
- DataTap token / 模型 key / 内部 token 未写入 Prompt、Skill、stdout、事件、Artifact、
  fixture、快照、QA 或 git diff；秘密扫描干净。
- 未读取/修改/提交任何 `.env`；无方案 B/C、积分、License、管理端、并发改动。
- 未修改 Task 3 的最小环境 allowlist 与 abort 清理语义。

## 发现的计划偏差

- **测试数据库**：简报「测试不得连接 kol_insight 或 kol_insight_test」，且本地无
  kol_insight_pi_poc 库（root 凭据不可得）。`test_evidence_ingest.py` 改用 SQLite
  内存库验证 ORM 持久化/幂等/事件/hash，完全隔离、可独立运行，不连接任何 MySQL。
- **测试范围**：`test_evidence_ingest.py` 直测 service 层（HTTPException 401/404、
  零积分落库、幂等、事件），未拉起完整 ASGI app（避免 create_app 加载整个运行时
  对完整 Settings/DataTap 的依赖）。

## Commit

- 独立 commit（仅含 Task 4 文件 + changelog + 报告；工作树既有未提交文件未混入）。

## 是否允许进入下一 Task

- 否。本报告等待独立审查通过后再放行 Task 5。
