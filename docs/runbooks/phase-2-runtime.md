# 第二阶段运行手册

当前架构为**模型主导的统一 Agent 运行时（Agent Runtime v3）**：`/api/v1/agent` 提供会话、Run、SSE 事件与 Artifact API，`backend/app/agent_runtime/` 负责执行与恢复，`backend/app/agent_artifacts/` 负责产物与导出。旧 `/api/v1/sessions/{id}/tasks`、`/quick/*`、`brainstorm`、手动 `/kol-analysis` 等执行入口已取消注册（返回 404，见 `backend/tests/agent_runtime/test_legacy_routes_removed.py`）。一次性切换、发布阻断条件与回滚清单见 [agent-runtime-v3-cutover.md](agent-runtime-v3-cutover.md)。

部署只使用一个 Uvicorn worker；Run、租约、MCP 调用、Evidence 与积分账本都由 MySQL 持久化，后续再按负载拆分 Worker。

## 启动与迁移

在项目根目录准备未提交的 `.env`，必须设置 `MYSQL_*`、随机 `JWT_SECRET`、`APP_ENV`、`AUTH_MODE`、`TENCENT_PLAN_API_KEY` 与 `DATATAP_MCP_TOKEN`。模型为任意 OpenAI 兼容端点（`TENCENT_PLAN_BASE_URL` / `TENCENT_PLAN_MODEL`，当前生产使用 Kimi `k3`）；`TENCENT_PLAN_REASONING_EFFORT`（low/high/max）为可选思考深度，仅 k3 等推理模型生效。MCP 固定使用 DataTap；不存在 Provider 切换或模拟回退。

```bash
cd backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-graceful-shutdown 5
```

`--timeout-graceful-shutdown 5` 限制优雅停机等待时长：前端 SSE 长连接（Run 事件流/thinking 流）不释放时，重启与热重载不会被无限期卡住。

开发环境只保留 `AUTH_MODE=mock` 的短信与微信登录模拟。模型与 MCP 在所有环境均使用真实供应商；测试前确认密钥有效，且测试日志不得输出模型响应、达人数据、令牌或接口地址。

可用以下命令验证真实配置与供应商连通性：

```bash
cd backend
.venv/bin/python -c 'from app.core.config import get_settings; s=get_settings(); print({"model":"deepseek-v4-pro","datatap_configured":bool(s.datatap_mcp_token.get_secret_value().strip())})'
.venv/bin/pytest tests/integration/test_real_providers.py -q
```

（`test_real_providers.py` 中断言 `deepseek-v4-pro`，与本机 `backend/.env` 的 `glm-5.2` 不符是历史遗留的环境性失败，不影响运行时。）

## 运行与恢复

- 发布或维护前将管理开关设为“关闭新任务”；已运行 Run 允许完成，必要时由 `POST /agent/runs/{run_id}/cancel` 终止。
- 进程重启后恢复作业（`agent_runtime/recovery.py`）扫描过期租约，重新领取可恢复 Run，从最后一个完整 Step 继续，**禁止凭内存状态重建调用**；相同 `logical_call_id` 不重复执行或扣费。
- `unknown` MCP 调用（请求已发出但结果未知，如网关 504）保持预留、禁止自动重放，只能经恢复核对（`agent_tool_call_reconciliations`）确认成功/失败/保持 unknown，必要时管理员走 `POST /api/v1/admin/agent-tool-calls/{call_id}/reconcile` 人工核对。禁止凭猜测重复请求。
- 账务排查以 `wallet_ledger` 与 `agent_tool_calls` 状态为准，核对每次成功 DataTap 调用 10 积分（`points_settled == 10`）；发现差异先冻结新任务，再导出账本与调用证据处理。
- 每条用户消息创建一个独立 Run（`session_analyst_v1`），SSE 事件流按 Run sequence 幂等续传（Last-Event-ID）。每个 Run Attempt 上限 30 分钟或 50 次模型决策，触发后 Run 以 `paused` 结束，用户显式 `POST /agent/runs/{run_id}/resume` 创建新 Attempt 继续。余额不足（`InsufficientPointsError`）作为结构化工具错误回喂模型，不直接失败。
- 正式 Artifact 必须经 Reviewer（`artifact_reviewer_v1`）复核后原子发布为不可变 Version；Reviewer 最多打回两次，第三次只能 approve/reject。发布前做字段级 lineage 完整性校验，缺失或失效引用拒绝进入 review。
- **已知风险（UAT 阻断项，见 cutover 清单）**：真实 DataTap 某些长查询在传输层无法可靠超时/取消，可挂死 Run；真实模型在 Attempt 预算内可能无法可靠产出 lineage 有效的正式 Artifact。修复完成前不应执行生产切档。

## UAT 部署

- 连接：`ssh root@111.10.192.19`（密钥用 `~/.ssh` 下默认 id_ed25519/id_rsa，免密已配好；服务器主机名显示为 localhost）。
- 布局：项目根 `/home/kol_insight/`（`backend/` 为 FastAPI 后端、`dist/` 为前端构建产物）；后端 `.env` 在 `/home/kol_insight/backend/.env`；虚拟环境 `/home/kol_insight/.venv`。
- 服务：systemd `kol-insight.service`（WorkingDirectory=`/home/kol_insight/backend`，uvicorn 监听 `127.0.0.1:8100`）；重启 `systemctl restart kol-insight.service`。
- 公网入口：nginx `http://111.10.192.19:40099`（`/api/` 反代到 8100，`/` 静态托管 dist；站点配置在 `/etc/nginx/sites-available/kol-insight`）。
- 同步方式：从本地工作区 `rsync`/`scp` 改动文件到 `/home/kol_insight/backend/`（不覆盖远端 `.env`），有迁移时先 `alembic upgrade head`，再重启服务。
- 验证：本机 `curl http://127.0.0.1:8100/healthz` 应返回 `{"status":"ok"}`；公网用 `curl http://111.10.192.19:40099/api/v1/agent/sessions` 期望 401（证明 nginx→后端链路通，`/healthz` 不在 `/api/` 下、不公网暴露）。
- 注意：远端无 --reload，改代码必须重启服务；云安全组与 ufw 是两层，曾误开 ufw 导致 SSH 断连，端口变更需同时确认两侧放行。

## 凭据与日志

日志调用 `app.core.redaction.redact_for_log()` 后再序列化。该函数递归遮蔽授权头、Cookie、手机号、模型/MCP token、JWT 密钥和 MySQL 密码；严禁打印原始请求头、环境变量或完整 Prompt。模型永远不接触 DataTap token、数据库 DSN 或 JWT 密钥。
