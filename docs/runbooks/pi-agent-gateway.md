# Pi Agent Gateway 运维手册

本手册覆盖方案 B 的本地/预生产操作边界。当前交付状态只到
`READY_FOR_REAL_B7_UAT`：真实 B7 UAT、生产切流和方案 C 均需要单独审批。

## 组件、版本与启动检查

后端使用 Python 3.11/3.12、FastAPI、SQLAlchemy Async 和 MySQL 8；启动前必须在隔离测试库执行
`backend/.venv/bin/alembic upgrade head`，并确认只有一个迁移 head。当前 head 为
`0041_runtime_usage_constraints`。后端健康检查使用 `GET /healthz`。

Pi Gateway 是独立 Node worker，控制面只允许调用显式注入的 FastAPI origin；生产锁定依赖为：

```text
@earendil-works/pi-coding-agent 0.79.10
@earendil-works/pi-ai 0.74.2
@earendil-works/pi-tui 0.74.2
pi-mcp-adapter 2.20.1
typebox 1.3.11
typescript 7.0.2 / tsx 4.23.10 / vitest 4.1.10
```

Gateway 的宿主进程实例化 `PiGatewayServer`，为每个 worker 注入唯一 `gatewayId`、控制面 client、
capacity 和 worker factory；不要把数据库驱动、FastAPI secret 或宿主环境全量传入 Node。运行前检查：

```bash
cd backend && .venv/bin/alembic heads
cd ../pi-gateway && npm ls --depth=0 && npm run typecheck
```

### 生产 Gateway 启动

生产入口是 `pi-gateway/src/main.ts`（构建产物 `dist/main.js`）：

```bash
cd pi-gateway
npm ci
npm run build
npm start        # node dist/main.js
```

配置只来自进程环境，缺失或非法即 fail-closed（退出码 1，日志只含变量名、不含取值）：

| 变量 | 必填 | 语义 |
| --- | --- | --- |
| `PI_GATEWAY_ID` | 是 | 固定 gateway id，必须在 FastAPI `PI_GATEWAY_ALLOWED_IDS` 白名单内 |
| `PI_GATEWAY_CONTROL_PLANE_URL` | 是 | FastAPI origin；production 必须 HTTPS，仅 development/test 允许 loopback HTTP |
| `PI_GATEWAY_INTERNAL_SECRET` | 是 | 与控制面共享的 HMAC 密钥（16–512 字符），签名绑定完整挂载路径 |
| `PI_GATEWAY_ENVIRONMENT` | 否 | `production`（默认）/ `development` / `test` |
| `PI_GATEWAY_CAPACITY` | 否 | 共享 Worker 容量，默认 1，上限 128 |
| `PI_GATEWAY_HEALTH_HOST` / `PI_GATEWAY_HEALTH_PORT` | 否 | 运维 HTTP 监听，仅 loopback，默认 `127.0.0.1:9471` |
| `PI_GATEWAY_CLAIM_INTERVAL_MS` / `PI_GATEWAY_CLAIM_MAX_BACKOFF_MS` | 否 | claim 轮询间隔（默认 1000）与有界退避上限（默认 30000） |
| `PI_GATEWAY_HEARTBEAT_INTERVAL_MS` | 否 | Run lease 续租间隔（默认 20000，必须小于 lease 秒数） |
| `PI_GATEWAY_SHUTDOWN_TIMEOUT_MS` | 否 | draining 等待上限（默认 10000） |
| `PI_GATEWAY_MAX_BUFFERED_EVENTS` | 否 | 控制面不可达时的有界内存事件缓冲（默认 256） |
| `PI_GATEWAY_WORKER_SCRIPT` / `PI_GATEWAY_WORKER_EXEC_ARGV` | 否 | Worker 入口覆盖，仅本地运行器/测试使用 |

HMAC 签名串精确为 `METHOD\n<完整挂载路径>\nTIMESTAMP\nNONCE\nSHA256(body)`；Node 与 Python 以
`pi-gateway/tests/hmac-contract.test.ts` 和 `backend/tests/pi_gateway/test_auth.py` 中同一组固定
夹具摘要互锁，任何一侧改动签名口径都会立即红灯。

运维 HTTP 仅监听 loopback：`GET /healthz` 存活性、`GET /readyz` 调度就绪（draining 后 503）、
`GET /metrics` 有界 JSON 计数（claim/错误/活动 Worker，不含租户、用户或任何密钥材料）。
SIGTERM/SIGINT 进入 draining：停止新 claim，按 `PI_GATEWAY_SHUTDOWN_TIMEOUT_MS` 有界等待活动
Worker abort，随后退出码 0。secret envelope 只在领取 Run 后以
`run_id:attempt_id:config_version_id:gateway_id` AAD 在内存解封，经子进程环境注入 Worker，
父进程随即清除引用；解密失败以稳定错误码收口当前 Run，不输出任何明文。

## Secret 与 Runtime Config

### Master key 生成与轮换

`RUNTIME_SECRET_MASTER_KEYS` 是逗号分隔的 `version:base64(raw-32-bytes)`，例如：

```bash
python -c 'import base64,os; print("v2:" + base64.b64encode(os.urandom(32)).decode())'
```

将新 key 以受限权限写入部署环境的 secret store，设置
`RUNTIME_SECRET_ACTIVE_KEY_VERSION=v2`，再滚动重启。旧 key 必须保留到所有旧 Runtime Config
读取/迁移完成；轮换只影响新写入，不能删除历史解密所需 key。禁止把 key、model token、DataTap token
写入 `.env.example`、Git、日志、Run snapshot、SSE 或管理响应。

### 写入与激活

管理员在 Runtime Config 页面创建 draft，后端通过 `RuntimeConfigService` 加密 secret 并只返回
masked/fingerprint 引用；浏览器不回读明文。确认 `runtime_contract_version=marketing_runtime_v1`、
backend、tenant scope、模型/桥接配置和价格快照后，再单独激活。激活是 append-only 版本操作，旧
active 版本转 retired，不修改已创建 Run 的 snapshot。

## 灰度、容量与回滚

1. 先创建/激活兼容的 tenant Pi Runtime Config，确认 License 有效且包含 `kol_selection`，再确认至少
   一个 `PiGatewayInstance` 为 `status=active`、`mode=active`、`desired_capacity>0`。
2. 管理员在租户页面把 `runtime_backend` 从 `current` 切到 `pi`。前置条件失败返回稳定 409，不能
   通过直接写数据库绕过。
3. Gateway 的 capacity 与 draining 由管理 API 调整；draining 只阻止新 claim，活动 worker 按 lease/
   heartbeat 完成或交由恢复。停止 Gateway 不把在途 Pi Run 转交 current，也不重放同一消息。
4. 紧急回滚优先打开 `PI_GATEWAY_KILL_SWITCH=true` 并滚动重启控制面。它只让**新 Run** 选择 current，
   不改历史 Pi snapshot、不杀在途 Run。随后可把单租户 backend 切回 current；切换仍只影响新 Run。
5. 若租户/Gateway 状态异常，先停止新 claim、保存审计与诊断，再按恢复服务处理 queued/running Run。
   不手工修改 `runtime_backend`、Attempt、lease 或账务状态。

## 诊断与恢复

- **queued/lease**：检查 tenant license、gateway status/mode/capacity、Run snapshot 和 lease owner；
  expired lease 由 recovery service 处理，不能用 current executor 抢 Pi Run。
- **Attempt/unknown**：首次基础设施丢失只结束 Attempt 并回队列；未确定的 ToolCall 保留 reserved 并
  标为 unknown，禁止自动重放。第二次基础设施失败才进入稳定 failed。
- **账务不一致**：读取 admin Run diagnostics 的 usage/reconciliation 投影；reconciliation 只读并标记
  mismatch，不自动修账。管理员核对后通过既有 reconcile 流程幂等结算/释放。
- **secret 解密失败**：确认 key version 仍在 key ring、AAD 所需 tenant/config/run 未变；不要复制明文
  到日志。恢复失败时停新 Run，保留原 Config 和审计记录，修复 key store 后再重试。
- **Gateway 离线/网络失败**：控制面错误归类为 `control_plane_unreachable`，worker 使用有界事件缓存；
  超限 abort 并交给恢复，不伪造业务 failed。查看 Gateway diagnostics 时只看稳定 error code。
- **SSE**：用户事件从 AgentEvent/SSE 续传；usage 仅进入 RuntimeUsageRecord，不进入用户 SSE 或 prompt。

## 禁止事项与真实 UAT 停止门

真实 B7 UAT 必须单独授权，使用 append-only 证据目录、独立测试钱包和隔离租户；每个 UAT round 记录
commit、迁移 head、依赖版本、Run/Attempt/lease、账务和停止条件。任一 secret 泄露、跨租户数据、重复
MCP/积分、unknown 重放、终态不一致或外部服务超时未分类，都立即停止并保留证据。

本地 Task 12 只使用 fake-friendly 组件/测试库，不等于真实 UAT 通过。当前 Runtime 至少保留一个稳定
发布周期；周期结束前不得删除旧表、旧 Review 数据或历史快照，也不得执行历史 Pi RPC/POC 真实六场景
Task 9、真实模型/DataTap、真实钱包/积分调用或生产切流。
