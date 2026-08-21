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
- 正式 Artifact 走确定性字段级 lineage 门禁后逐项直接发布为不可变 Version；新 Run 不得启动 Reviewer。
  发布汇总以 `artifact.publish.completed` 表达逐项 `published/validation_failed/failed`，至少一项
  成功且存在失败时 Run 为 `completed_with_warnings`。旧 Review 表只读保留，供历史回滚读取。
- 当前发布批次唯一迁移 head 为 `0049_skill_rollout_history`；`0036_export_claim_token` 仅为旧阶段
  历史记录。部署顺序：备份数据库和上传目录 → drain Run /
  清零历史 `reviewing` → 后端代码与 `alembic upgrade head` → 同批前端 `dist/` → 单 worker 重启 →
  权限、账本、SSE、三类导出冒烟。详情与回滚限制见 cutover §5.10。
- **已知风险（UAT 阻断项，见 cutover 清单）**：2026-08-07 真实 UAT 的品牌场景成功（restricted
  lineage_ok），但活动回答 child Run 因模型供应商持续重连被中断；未完成真实全场景验收前不得生产切档。

## 2026-08-21 Direct MCP 与取消修复的发布前口径

- Pi 新路径将标准 MCP `CallToolResult` 原样交给模型；计费、permit 和 Evidence 统计是独立旁路，
  不把 resource/text/structuredContent 重新包装成业务 envelope，也不以 Evidence 增量作为模型结果
  可见的前置条件。旧兼容读取路径仍保留。
- 取消必须经过持久 `cancel_requested`、外发前 preflight、Gateway heartbeat、worker/provider
  abort、终态 fence、Recovery 和 SSE；前端只显示“正在取消”，直到收到唯一真实
  `run.cancelled`。在飞调用仍按发送事实分类，`result_unknown` 保留预留且禁止自动重放。
- 本轮真实 Web UAT 的品牌固定为“瑞幸咖啡”，且在远程 CI 全绿后只执行一次 Direct MCP + 取消
  组合验证。candidate-r8 的 Backend 红灯已定位为 CI 未安装 `pi-gateway` 依赖；candidate-r9
  (`3c01d13`，Actions `32476331375`) 已全绿并完成预发布部署。唯一 Web UAT 随后在 Run 创建前因
  58 个 enabled+approved MCP 目录超过 Pi adapter 上限 32 而返回 `runtime_adapter_catalog_too_large`；
  不得重试或以配置放宽掩盖，当前仍禁止生产部署。

## 2026-08-21 历史唯一瑞幸 Web UAT 停止记录

- 专用租户 `uat-pi-r9-20260821` 已切换到 Pi backend，后端与 Gateway 均 healthy/ready，迁移 head 为
  `0049_skill_rollout_history`。
- 唯一请求未创建服务端 Session/Message/Run/Attempt/Tool Call；没有模型调用、DataTap 外发、积分扣费
  或取消竞态。只读目录统计为 `insight-cube-mcp=24`、`social-grow-mcp=16`、
  `social-grow-content-mcp=10`、`bilibili-mcp=8`，合计 58。
- 该结果是发布配置预检阻断，不是 MCP 透传或取消链路的通过证据；最终功能验收和生产灰度保持停止。
  若要调整租户目录或适配器限制后重新执行，必须先取得新的明确 Web UAT 授权。

## 2026-08-21 Pi Adapter Catalog 容量修复后的运行口径

- 上述 `runtime_adapter_catalog_too_large` 是历史 control-plane catalog 上限 32 的预检失败，不是
  Pi SDK 或模型工具限制。现行新 Pi 路径上限为 128 个目录条目，canonical JSON 总大小上限为 128 KiB；
  仍禁止截断、分页、用户文本筛选和任意字符串 allowlist。
- `RuntimeConfigService`、后端 Pi Gateway contract 与 `pi-gateway/src/protocol.ts` 使用相同边界和 canonical
  字节计算。`quarantined`、`unknown`、`query_user_info` 排除；字段/schema digest、重复身份、敏感字段、
  DTO/parser 对称校验保留。单一 MCP proxy、`directTools=false`、`scriptMode=false`、output guard、
  归属与 10 积分结算边界不变。
- candidate `0615533c5f65bfd55c57fbbd181fbfa622c13282` 的唯一 Actions run `32480577421` 全绿；受影响
  Backend 47 项、Pi Gateway 56 项定向测试、Ruff、typecheck、build 均通过，独立审查 Critical=0 / Important=0。
- 精确候选已部署到 UAT，服务 health/ready 正常，迁移 head 仍为 `0049_skill_rollout_history`。对专用租户的
  一次只读回滚检查确认新 Run Snapshot 完整包含 58 项（24/16/10/8），没有创建 Run 或改写历史数据。
- 当前浏览器仍是另一个个人租户，未向错误租户发送测试请求；专用账号认证尚未提交。当前状态为
  `PREPROD_DEPLOYED_CATALOG_REPAIR_VERIFIED / WEB_UAT_AUTH_CONFIRMATION_REQUIRED`，不得合入 main、生产
  部署或灰度。

### 唯一 Web UAT 后续结果（当前停止口径）

- 已完成专用账号切换，并只发送一次瑞幸咖啡请求。候选 `0615533` 的首次 claim 409 已定位为
  `pi_gateway_claim_catalog_invalid`：服务端 Pydantic catalog DTO 未先转成 canonical JSON mapping；不是
  58 条目容量或工具审核问题。
- 追加线性测试/修复提交 `5682b6a`、`fc4e70c`，Backend 受影响定向回归 `48 passed`；UAT 后端已同步并重启。
  同一个 Run 由 Attempt 1 领取，但只产生内部工具事件，未产生外部 `AgentToolCall`/DataTap dispatch。
- 唯一 Run 最终以 `pi_model_provider_error` 失败；取消按钮未点击（没有标准 MCP Result 到达模型，取消无法测量），
  不重试、不创建第二个 Run。Run/Attempt 已终态，租约、active_run、ToolCall、账务预留和 worker 均已清理。
- 当前门禁为 `REAL_UAT_CATALOG_CLAIM_FIXED_PROVIDER_FAILED / NOT_READY_FOR_FINAL_FUNCTIONAL_UAT`。
  在新的 provider/发布授权和成功功能 UAT 前，不得合入 main、生产部署或执行灰度。

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
