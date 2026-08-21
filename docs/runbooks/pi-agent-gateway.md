# Pi Agent Gateway 运维手册

本手册覆盖方案 B 的本地/预生产操作边界。

> 状态更新（2026-08-14，Direct Artifact Skill 门禁纠偏）：新 Pi production path 不产生
> 数据库 `Evidence` 业务实体、不使用 `mcp_result_v1` 分类、无 required artifact 门禁；
> 标准 MCP Tool Result 由 adapter 原样交给模型，accounting finalize 只传 metadata；Builder
> 统一为 `build_artifact_draft`（Snapshot allowlist + typed model input）。真实 Direct Model
> + MCP Smoke 已执行：`DIRECT_MODEL_MCP_SMOKE_FUNCTIONALLY_ACCEPTED_WITH_PROTOCOL_DEVIATION`
> （直连对照调用 2 次超出授权上限 1 次，见
> `docs/qa/2026-08-13-direct-model-mcp-smoke-review.md`）。audited Direct MCP baseline
> `c01ec1ba1ea3dc3805184ea3ddb8f4bf0ea14196` 仅保留为历史 Direct MCP Smoke baseline，
> 不再是新 Scenario 2 的 execution gate。
> Direct Artifact Skill 契约修复已完成并独立审查通过（Critical 0 / Important 0 / Minor 0；
> 六个已审核线性提交 `284e4c7`/`45ec465`/`260f5cc`/`d4ab189`/`f15ff5d`/`37be5b6`：模型输入
> DTO + 服务器组装 + 结构化错误反馈 → load_marketing_skill 暴露模型输入契约 →
> capability pack 1.1.0 + 离线 UAT 自纠错 + result_unknown 元数据可观测性 → B7 文档对齐 →
> 关闭审查 Minor → 导出缓存透传 lineage snapshot）。现行 audited Direct Artifact Skill
> baseline（执行门禁）：`37be5b67c52764abb0ab38c458197d3827729144`（branch
> `codex/direct-artifact-skill-contract-repair`；execution HEAD 必须是其线性后代且包含上述
> 六个提交，`37be5b67..execution HEAD` 区间只允许门禁纠偏的 Markdown/changelog 提交，
> 下一条真实 UAT 授权消息必须逐字绑定纠偏提交后的完整 execution HEAD）。
> 当前状态：`READY_FOR_WEB_FUNCTIONAL_SCENARIO_2_RERUN_REVIEW`（待用户授权后重跑；
> 单场景授权模板见授权包 §5.4，验收口径 A/B/C 三档）。
> 历史状态（2026-08-09 写入的 `READY_FOR_REAL_B7_UAT` 已被架构审核否决；2026-08-12 的
> `READY_FOR_REAL_B7_UAT_REVIEW`/`REAUTHORIZATION` 与失败 round
> `REAL_B7_20260812T045636Z_b801c490` 均为历史事实）：真实 B7 UAT、生产切流和方案 C 均
> 需要单独审批。

> 当前发布批次覆盖（2026-08-21）：`main` 发布树为 `c6e5dc7`（随后若有仅文档提交，以其
> 直接后代为准），当前 Alembic head 为 `0049_skill_rollout_history`。本手册下方出现的
> `0036_export_claim_token`、`0043_billing_downgrade_guard` 或 `0044_agent_run_loop_guard`
> 均是对应历史阶段的事实，不是本发布批次的迁移目标；预发布必须先同步本发布树并执行
> `alembic upgrade head`，确认 `alembic current` 为 `0049_skill_rollout_history` 后才能重启。
> 本批次尚未完成一次真实 Web UAT，不能据此宣称生产切流或灰度通过。

## Direct Artifact Skill 契约（2026-08-13 修复后现行）

### capability pack 版本

- 当前包目录 `backend/app/marketing_capability_pack/packs/marketing-v2/`，manifest
  `pack_version="1.1.0"`（6 个 skill 的 `version` 全部 1.1.0）；`build_marketing_run_capability`
  现加载 `marketing-v2`。manifest 内所有 digest（root_policy / skill / contract）是文件内容
  SHA-256（hex）；manifest 自身 `manifest_digest` 由 loader 对 manifest 内容计算。
- 旧 `marketing-v1`（1.0.0）目录原样保留只读：历史 RuntimeSnapshot 经 digest 仍可解析，
  旧 Run 语义不变；禁止原地修改 v1。
- 下一轮 UAT 新建 Runtime Config 时自动使用新 pack（`build_marketing_run_capability` 指向
  v2），无需手工切换；已有 Run 的快照保持不变。

### 模型输入契约

- `build_artifact_draft` 的 payload 按 `load_marketing_skill` 返回的
  `model_input_contract.model_input_schema` 构造：只提交业务字段
  （scope/data/narrative/availability/limitations/methodology_input，insight 另含
  title/parent_artifact_id/parent_artifact_version_id/blocks）。
- `schema_version`/`module`/`data_status`/`canonical_data`/`field_lineage` 是服务器字段，
  由服务器组装（canonical/lineage 从 data 确定性生成、精确覆盖全部叶子）；模型提交这些
  字段会被 `server_owned_field_rejected` 明确拒绝。
- 校验失败回喂结构化字段级错误（RFC6901 path/type/reason/retryable，≤2048 字节、
  truncated 标记、不泄漏提交值）；模型按 path 修正后重试（离线 UAT 自纠错场景已进程级验证）。

## Native Skill/Report 迁移边界（2026-08-21）

### 生产 Native 路径

- Skill 的唯一运行时事实源是创建 Run 时冻结并校验的 Run Snapshot。Gateway 将 Snapshot 中的
  Skill 正文物化到当前 Run 专属目录后显式注入；不读取用户目录、项目目录、cwd 或本机工作树
  的自动发现结果。Root Policy、Tool Contract、Skill digest 和 capability pack 版本必须与
  Snapshot 一致，任一漂移都在模型/MCP 外发前 fail-closed。
- Skill 是模型自主决策的能力说明，不是固定阶段、固定工具顺序、固定调用次数、固定输出规模
  或固定业务权重。模型可按用户目标和当前可用能力选择澄清、历史读取、MCP、确定性计算、Draft、
  Reviewer 或完成；服务端只守住能力边界、归属、计费、证据、结构校验和状态机。
- MCP 标准结果直接交给模型；错误、空结果、partial、timeout 与 `result_unknown` 保持原分类，
  不经 Evidence Bridge，不引入 `mcp_result_v1` 业务实体，也不自动重放可能已发送的调用。
- 正式产物统一经受控 Artifact Draft/Review/Publish 链路。固定模板不能表达长尾需求时使用
  `analysis_report_v1`；Excel 由不可变同版 Version 确定性投影为 `workbook_v1`。所有缺失值必须
  用 `null`、availability 和 limitation 表达，禁止将受限数据当作 0。

### 迁移期 POC 兼容面

- `pi-runtime/src/extensions/internal-tools.ts` 明确标记为 `compatibility`，只供 POC 和兼容测试
  使用。`load_marketing_skill` 在迁移期保留，但生产 Native 正常路径不依赖它；旧 POC 的
  `build_*_draft`/发布工具名不得写入 Native Skill 作为生产必经步骤。
- 修改 Native Skill 文案时必须保持模型自主决策、Run Snapshot、Tool Contract、Evidence 可追溯、
  partial/限制披露和结构化校验反馈语义；不得恢复固定工具顺序、旧桥接协议或本机路径依赖。

### 验证边界

- 本迁移记录只使用离线单元/集成测试验证文案、快照来源、工具契约和安全断言；不等同于真实模型、
  DataTap、钱包、生产数据库、部署或 Web UAT 通过。任何真实外部调用仍需独立授权、隔离租户、
  append-only 证据目录和 reviewer 封口。

### result_unknown 可观测性

- `PiGatewayMcpFailureMetadata` 携带可观测字段（全部可选）：`error_class`（adapter error
  code，如 `call_failed`）、`received_jsonrpc_response`、`dispatch_phase`
  （preflight/dispatched/unknown）、`is_standard_mcp_error`、`upstream_request_id`；
  控制面 `safe_error_message` 以紧凑 JSON 保存非空子集。
- 分类语义不变：`call_failed` → `result_unknown`，预留保持、不自动释放、不自动重放；
  metadata 只用于审计/排障。
- 遗留项 `ACCOUNTING_UNKNOWN_DIAGNOSTIC_REQUIRED`：真实 round 的 14 个 unknown 均为
  adapter `call_failed`（DataTap 抛异常问题），待独立诊断；不阻塞下一轮核心功能

## 正式完成契约与历史 Snapshot 兼容（2026-08-21）

### 新 Pi Runtime 的通用完成不变量

- 新 Run 不再拥有固定 required Artifact 类型。Profile、RuntimeSnapshot、用户文本、模型输出和
  Builder 都不得指定或推导某一种预定报告；`completion_mode` 仅是服务端拥有的“正式分析/交互”语义，
  不是 Artifact contract，也不能被 prompt 覆盖。
- 正式分析 Run 进入 `completed` 或 `completed_with_warnings` 前，必须有当前 Run 的至少一个顶层主
  Report Version。Pi 可在 Snapshot artifact allowlist 内选择现有标准 Artifact 或 `analysis_report_v1`。
  `clarification_requested` 不要求报告；failed/cancelled/paused 不伪造报告；interaction Run 也不
  作为正式分析报告门禁。
- 主 Report 必须同时满足严格 Schema、allowlist、tenant/user/session/run 归属、已发布 Publication、
  不可变 Version、Draft Revision、lineage/可信字段校验。child insight、历史 Run、其他租户或未发布
  Draft 均不能满足当前 Run。Excel 请求产生的 `workbook_v1` 必须引用同一 Report Version；不一致时
  以稳定错误拒绝。

### 公共终态出口与兼容读取

- engine、terminal ACK-loss/迁移、Recovery 和 `force-complete` 必须调用同一个
  `CompletionValidator`；不得在任一出口恢复固定 Artifact 门禁或另加测试特判。
- 历史 `required_artifact` 字段、DTO、数据库列和旧 RuntimeSnapshot 保留用于兼容读取；历史 Snapshot
  按其原版本语义读取，绝不回写。新 Snapshot 构造时只保留当前能力 allowlist，不把旧固定 contract
  复制成新 Run 门禁。既有标准 Artifact Schema 与 Exporter 不因本修复改变。
- 排障时区分 `pi_gateway_main_artifact_missing`（当前正式分析缺少任意合法主报告）与历史兼容错误
  `required_artifact_missing`/`required_artifact_invalid_lineage`；后者只能来自旧 Snapshot 语义，
  不能由新 Run 的固定类型推导产生。

### 验证边界

2026-08-21 的受影响定向测试为 57 项通过；原 12 项红灯只复验一次（11 项通过，剩余 1 项隔离后通过），
离线 UAT 全套只运行一次并为 28 项通过。上述均不等同于真实模型/DataTap、钱包、生产库、部署或 Web
UAT 通过；生产发布仍须遵循本手册的隔离、灰度、监控和回滚步骤。
  Scenario 2 重跑，但阻塞完整 B7 PASS 与生产切流。

## 组件、版本与启动检查

后端使用 Python 3.11/3.12、FastAPI、SQLAlchemy Async 和 MySQL 8；启动前必须在隔离测试库执行
`backend/.venv/bin/alembic upgrade head`，并确认只有一个迁移 head。当前 head 为
`0044_agent_run_loop_guard`（`0043_billing_downgrade_guard` 为第一轮 B7 时点的历史事实）。
后端健康检查使用 `GET /healthz`。

迁移回滚护栏：0043 的 downgrade guard 挂在 head→0042 一步；任何降穿 0040 的命令（包括
staged downgrade）由 `migrations/env.py` 的预检统一拦截——租户账本在 0040 之后产生新流水/
用量/余额漂移时 fail-closed，绝不静默丢账。

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

B7 专用的模型决策预算：`limits.max_decisions`（整数 1..100）是 server-owned 的模型请求预算，
经 claim snapshot 送达 worker；worker 的 provider 流式入口由 `ModelRequestBudget` 在任何 HTTP
外发前同步计数并拦截（超限抛稳定码 `pi_decision_limit`，terminal=failed、不创建恢复 Attempt、
不重放）。缺失或非法（布尔/浮点/越界）时 worker 在启动前 fail-closed
（`pi_gateway_runtime_snapshot_invalid`），Gateway 不得用默认值替代服务端快照。SDK 两层自动
重试（agent auto-retry 与 provider maxRetries）在生产 session 中一律关闭；provider 流式终局
失败以 `pi_model_provider_error` 稳定 failed 收口。历史 B7 流程：L1 用 `max_decisions=2` 的配置版本；
L1 通过后、L2 前激活 `max_decisions=50` 的新 append-only 版本（只影响新 Run，旧 Run snapshot
不变）。现行（2026-08-13）：`max_decisions` 是防失控紧急上限，不是工具规划策略；Web Functional
Scenario 2 建议 `max_decisions=60`，不得因达到常见调用数量而提前干预模型（授权包 §5.4）。

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
- **Heartbeat 失败语义**：单次 heartbeat 网络抖动/超时不丢租约；连续 3 次失败才按 lease 丢失
  处理并把 Run 交给恢复。Worker abort 是 SIGTERM 加 5 秒 SIGKILL 升级——优雅停机卡住的子进程
  也必须真正退出，否则旧 Attempt 会经 IPC 桥继续执行工具调用（双重执行）。
- **SSE**：用户事件从 AgentEvent/SSE 续传；usage 仅进入 RuntimeUsageRecord，不进入用户 SSE 或 prompt。

## 真实 B7 UAT 专用隔离环境（2026-08-12 绑定）

真实 B7 UAT 只允许使用以下已创建并核验的专用环境（授权计划 §2.0；启动门禁逐项核验，
任一不符即 B7_BLOCKED）：

| 项 | 值 |
| --- | --- |
| 数据库 | `kol_insight_b7_uat`（host `127.0.0.1:3306`，user identity `kol_b7_uat@localhost`，`APP_ENV=test` / `AUTH_MODE=mock`） |
| migration head | `0043_billing_downgrade_guard`（迁移已完成，只核验不重建） |
| charset / collation | `utf8mb4` / `utf8mb4_unicode_ci`（73 张表） |
| 隔离证明 | 专用账号访问 `kol_insight.users` 被 MySQL 1142 拒绝 |
| MySQL 密码 | macOS Keychain `com.kol-insight.real-b7-uat.mysql` / `kol_b7_uat@127.0.0.1`（只记引用，值仅进程内） |
| DataTap Token | macOS Keychain `com.kol-insight.real-b7-uat.datatap` / `DATATAP_MCP_TOKEN`（只记引用） |
| Runtime master keys | macOS Keychain `com.kol-insight.real-b7-uat.runtime-secret-master-keys` / `v1`（32 bytes，active version `v1`，只记引用） |
| 模型配置 | 主仓库未跟踪文件 `backend/.env`（只读 `TENCENT_PLAN_BASE_URL`/`TENCENT_PLAN_MODEL`/`TENCENT_PLAN_API_KEY`，仅进程内） |

运维边界：

- 严禁连接 `kol_insight`、`kol_insight_test` 或任何开发/预生产/生产/正式客户数据库；禁止
  DROP/CREATE/重建 `kol_insight_b7_uat`；禁止对其运行普通 pytest、离线 UAT harness 或迁移
  downgrade。
- 环境初始化（两个 synthetic tenant、用户、各 2000 积分钱包、周期额度 2000、全能力 License、
  租户级 Pi Runtime Config）只经生产 domain/admin service 幂等完成并写审计/账本；禁止直接
  INSERT/UPDATE `encrypted_runtime_secrets`。
- L0 全程零真实模型/DataTap 请求：L0 阶段控制面以 loopback 占位 `DATATAP_MCP_ORIGIN` 启动；
  真实 DataTap discovery（仅 negotiation/list-tools，0 ToolCall、0 积分）只允许发生在
  L1-00 外部调用预检（真实 origin 重启控制面后核验 29 个已审核工具 digest 与固定
  quarantine 基线 `insight-cube-mcp`/`query_user_info`）。
- 数据库数据 `retained_by_policy`：tenant/用户/账本/Runtime Config/License/usage/lineage
  保留至独立 reviewer 封口；清除需用户另行授权；证据目录永久保留。
- 证据封口角色分离：operator 只追加 `execution_completed`/`execution_stopped` 帧与
  `operator-summary.md`；`round_sealed` 帧、`verdict.md`、`hashes.sha256` 只由
  independent reviewer 写入；历史失败 round 的证据目录只读封存，禁止补写或覆盖。
- 本地身份闸拦截（`mcp_tool_identity_invalid`/`mcp_tool_identity_ambiguous`）按设计不产生
  preflight/控制面记录（0 外发、0 扣费）；operator 必须从 Run 的 `agent_events`
  （tool.started/tool.failed）与 assistant 消息中收录被拦截尝试，以便 reviewer 区分
  「模型未尝试」与「尝试被拦截」。

## 禁止事项与真实 UAT 停止门

真实 B7 UAT 必须单独授权，使用 append-only 证据目录、独立测试钱包和隔离租户；每个 UAT round 记录
commit、迁移 head、依赖版本、Run/Attempt/lease、账务和停止条件。任一 secret 泄露、跨租户数据、重复
MCP/积分、unknown 重放、终态不一致或外部服务超时未分类，都立即停止并保留证据。

本地 Task 12 使用 `backend/tests/integration/test_pi_gateway_offline_uat.py` 的离线进程级
fake topology（测试 MySQL + FastAPI 子进程 + 生产 Gateway 可执行文件 + fake 模型 + fake
DataTap MCP，0 外部网络），不等于真实 UAT 通过。当前 Runtime 至少保留一个稳定
发布周期；周期结束前不得删除旧表、旧 Review 数据或历史快照，也不得执行历史 Pi RPC/POC 真实六场景
Task 9、真实模型/DataTap、真实钱包/积分调用或生产切流。
