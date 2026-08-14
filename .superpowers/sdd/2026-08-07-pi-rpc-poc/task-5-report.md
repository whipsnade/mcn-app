# Task 5 报告：受控历史、Builder 与确定性发布内部工具

## Task 与交付结论

完成受控内部工具桥：Pi 只可见 11 个白名单工具（get_session_context /
search_evidence / read_tool_result / read_artifact / 六个 build_*_draft /
publish_artifacts），历史读取与六类 Builder 复用既有可信工具，publish 复用
ArtifactPublicationService（确定性发布、幂等）。禁止 bash/shell/文件编辑/任意
HTTP/Draft 直写（create_draft/update_draft/abandon_draft）/计算/记忆；DataTap
仍由 Task 4 Extension 直连，内部 Registry 不注册 AgentMcpTool。身份由服务端从
Run token 对应 Run 强制注入，伪造身份键被剥离或 Schema 拒绝。未接入积分、
正式 Runtime 或方案 B/C。

## 修改文件

- `backend/app/pi_runtime_poc/internal_tools.py`（新增）
- `backend/app/pi_runtime_poc/service.py`（worker_id + execute_internal_tool）
- `backend/app/pi_runtime_poc/router.py`（POST /runs/{run_id}/internal-tools）
- `backend/tests/pi_runtime_poc/test_internal_tools.py`（新增，9 例）
- `pi-runtime/src/extensions/internal-tools.ts`（新增）
- `pi-runtime/src/http/client.ts`（executeInternalTool）
- `pi-runtime/tests/internal-tools.test.ts`（新增，5 例）
- `changelog/2026-08-07.md`、`.superpowers/sdd/2026-08-07-pi-rpc-poc/task-5-report.md`

## 红灯证据

- 前端：实现前 `npm test -- internal-tools.test.ts` 因模块缺失 collection fail。
- 后端：实现前 `pytest --confcutdir ... test_internal_tools.py` 报
  `ModuleNotFoundError: No module named 'app.pi_runtime_poc.internal_tools'`。

## 绿灯命令与准确结果

- `cd pi-runtime && npm test` → **27 passed**（internal-tools 5 / datatap-mcp 6 /
  http-client 4 / redaction 4 / rpc-probe 8）。
- `cd pi-runtime && npm run typecheck` → 通过。
- `cd backend && .venv/bin/pytest --confcutdir=tests/pi_runtime_poc tests/pi_runtime_poc/test_internal_tools.py -q`
  → **9 passed**。
- `cd backend && TENCENT_PLAN_API_KEY=<占位> DATATAP_MCP_TOKEN=<占位> .venv/bin/pytest tests/pi_runtime_poc -q`
  → **37 passed**。
- `cd backend && .venv/bin/ruff check app/pi_runtime_poc tests/pi_runtime_poc app/api/router.py`
  → **All checks passed**（ruff --fix 清理 20 个冗余 noqa 后零剩余）。
- app openapi 确认 `/api/v1/internal/pi-poc/runs/{run_id}/internal-tools` 注册。

## 安全/数据隔离核对

- 后端测试用 SQLite 内存库（MEDIUMTEXT 编译 TEXT），不连接 kol_insight /
  kol_insight_test / kol_insight_pi_poc 任何 MySQL 库。import 链到达 mcp.py 时用
  非敏感占位环境变量通过 Settings（engine lazy 不连接）。
- 伪造身份键双重防护：Registry 剥离 SERVER_RESERVED_KEYS（忽略）+ HTTP Schema
  extra=forbid（422 拒绝）；`test_http_schema_rejects_forged_identity_keys` 覆盖。
- 工具目录白名单测试断言不含 bash/read/write/edit/create_draft/update_draft/
  abandon_draft/remember_scope/calculate_*/rank_kols。
- Builder 反馈只返回受限摘要（schema_version/draft_id/limitations），测试断言
  safe_summary 不含完整原始 Evidence。
- 未提交/读取/修改任何 `.env`；无方案 B/C、积分、License、管理端、并发改动。

## 发现的计划偏差

- **tests/agent_artifacts 套件不运行**：计划验证命令含 `tests/agent_artifacts`，
  但其依赖根 conftest 的 db_session（绑定 kol_insight_test），简报硬边界禁止
  连接 kol_insight_test，且 `--confcutdir=tests/pi_runtime_poc` 下其 db_session
  fixture 不可用。复用既有 Builder/publish 的集成验证改由 test_internal_tools
  （brand Evidence → build draft → publish → Version → export_artifact Excel 渲染
  同 Version 全链路）覆盖。
- **worker_id**：POC service 使用固定 worker_id（"pi-poc"），publish 前主动确保
  Run 活跃租约（claim_lease）；完整租约生命周期由 Task 7 runner 接管。

## Commit

- 独立 commit（仅含 Task 5 文件 + changelog + 报告；工作树既有未提交文件未混入）。

## 是否允许进入下一 Task

- 否。本报告等待独立审查通过后再放行 Task 6。
