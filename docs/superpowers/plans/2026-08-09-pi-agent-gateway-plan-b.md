# Pi Agent Gateway 方案 B 实施计划

> **执行要求：** 开发代理必须使用 `superpowers:executing-plans`，逐项完成并在每个任务提交后复核。
> 方案 B 含多个相互独立的审查面；可在同一开发会话内连续执行，但不得跨过任务内的红灯、绿灯、
> 审查和提交边界。

**目标：** 在 FastAPI 可信控制平面之外新增生产级 Node.js Pi Agent Gateway，完成多租户、
License、不可变运行配置、共享 Worker 容量、持久队列、公平调度、会话互斥、透明 MCP 积分结算、
Pi 事件接入现有 SSE、管理端和租户级灰度；最终停在 `READY_FOR_REAL_B7_UAT`，等待真实外部 UAT
授权。

**架构：** FastAPI 继续拥有身份、租户、License、队列、Run/Attempt/Step/ToolCall/Event、Evidence、
Artifact、账务和管理 API。`pi-gateway/` 是无数据库权限的 Node 服务：主进程维护共享容量与 draining，
每个被领取 Run 启动一个隔离子 Worker；Worker 使用 Pi SDK `createAgentSession()` 与
`SessionManager.inMemory()`，结束即销毁。Gateway 只能通过经过签名且绑定 Run lease 的内部 HTTP
协议读上下文、调用受控内部工具、预留/结算 MCP 调用、上报事件与终态。前端仍只连接 FastAPI。

**技术栈：** Python 3.11/3.12、FastAPI、SQLAlchemy Async、MySQL 8、Alembic、Pydantic、
AES-256-GCM、Node.js 20+、TypeScript、Pi SDK、`pi-mcp-adapter`、React 19、pytest、Vitest、
Playwright。

---

## 0. 进入条件与事实基线

### 0.1 授权边界

- 本计划允许在 B0 修复最终通过后进行 **B1–B6 和 B7 本地/合成 UAT 开发**。
- 历史 Pi-only round `20260808T060814Z` 的真实 Gate A 结论仍是 `EVALUATED_FAIL`，不得改写、
  覆盖或表述为 PASS。
- B0 的 synthetic Gate PASS 证明本地 Builder/Publication/Gate 业务契约可用，不等于真实模型、
  DataTap、多租户钱包或生产切流授权。
- 实施期间不得调用真实模型、DataTap、真实钱包或积分，不得重跑历史 Pi RPC/POC 真实六场景 Task 9；
  本计划 Task 9（B5-SSE）属于获准的本地实现范围，不得写历史 round；
  账务测试只使用事务回滚的测试库或纯内存 fake。
- B7 只做到本地多租户 UAT、灰度机制和回滚演练。真实模型/DataTap/钱包 UAT、生产租户切流、
  将 Pi 设为默认，以及方案 C 都要在本计划之外取得新的明确授权。
- 方案 B 真实 UAT 通过并稳定一个完整发布周期后，才提醒用户是否评估方案 C；未经确认不得创建
  方案 C 代码、迁移或任务。

### 0.2 实施基线

- 本计划编写基线：B0 加固提交 `81bc33cf6712238a2087eff061b3d08e30cf2106`，Alembic head
  为 `0036_export_claim_token`。
- 开始实施时必须以开发会话完成 B0 最终修复后的新 HEAD 为准，读取最新 2–3 篇 changelog，确认
  工作树干净、无 `.codegraph/write.lock`，然后将本计划提交 cherry-pick 到该分支。
- 若 B0 最终修复新增了迁移或改动本文列出的稳定接口，先修订本文的迁移编号/文件清单并单独提交；
  不允许在实现中悄悄偏离计划。
- 当前受生产 POC 事实锁定的 Pi 依赖是：
  `@earendil-works/pi-coding-agent@0.79.10`、`@earendil-works/pi-ai@0.74.2`、
  `@earendil-works/pi-tui@0.74.2`、`pi-mcp-adapter@2.20.1`。历史
  `docs/qa/pi-runtime-version-probe.md` 中的 0.84.1 是迁移前探针；由于 adapter 兼容性已回退，
  不得据此升级生产 Gateway。
- B0 已提供并要求继续复用：`CapabilityPackLoader`、营销 Run snapshot、
  `load_marketing_skill`、六类确定性 Builder、Publication Validator、Exporter 和离线 Gate。

### 0.3 开始实施前的强制验收

在最终 B0 HEAD 上运行并保存输出摘要；任一失败就停止，不进入 Task 1：

- `backend/.venv/bin/ruff --version` 必须满足 `>=0.9,<1`；禁止使用全局 Ruff。
- 全仓 Ruff 门禁使用 `backend/pyproject.toml` 中显式的稳定规则
  `[tool.ruff.lint].select = ["E4", "E7", "E9", "F"]` 及四个已确认的 POC 基线逐文件豁免，
  不使用额外全局 `--ignore`、批量 `noqa` 或修改历史债务文件。
- 先前全局 Ruff 0.3.3 的隐式默认规则扫描报告 335 项历史债务；这些不是方案 B 启动回归，
  Task 0 仅稳定工具版本与规则，不清理这批无关历史代码。

```bash
cd backend
.venv/bin/ruff --version
.venv/bin/pytest -q tests/marketing_capability_pack tests/pi_runtime_poc tests/agent_artifacts
.venv/bin/ruff check app tests
cd ../pi-runtime
npm test
npm run typecheck
cd ..
npm run lint
git diff --check
git status --short
```

验收还必须执行离线 B0 Gate，断言六案例 60/60 hard checks、钻取绑定确切 Version 且 0 DataTap、
澄清/拒答均 0 Artifact/0 DataTap。不得生成伪造人工评分。

---

## 1. 全局不变量

1. **可信控制平面唯一：** Gateway/Worker 无 MySQL DSN、无数据库驱动、无 Artifact 文件写权限；
   所有持久化只经 FastAPI。
2. **租户隔离：** 每个 Run 在创建时固定 `tenant_id`；所有内部读写同时验证 tenant/user/session/run/
   attempt/lease，归属失败统一 404 或内部稳定拒绝码，不泄漏对象存在性。
3. **配置不可变：** 新 Run 固定 `runtime_backend` 和 `runtime_config_snapshot_json`；管理端变更只影响
   后续 Run，不修改历史 snapshot。
4. **密钥最小暴露：** 数据库只存 AES-GCM 密文、nonce、key version、fingerprint 和掩码；普通/管理
   API 永不返回明文。FastAPI 只在 claim 时解密本 Run 所需密钥，经内部受控响应送到 Worker 内存；
   Worker 只通过子进程环境注入，退出后清除引用，不写文件、日志、事件或诊断。
5. **工具最小权限：** Pi 必须禁用 builtin read/bash/edit/write/grep/find/ls 和自动资源发现；只允许
   审核后的 `pi-mcp-adapter` 代理及 B0 内部工具白名单。模型不能写 payload、Excel、BI 或数据库。
6. **durable-before-send：** 每次真实 MCP 外发前，FastAPI 必须在单事务内完成工具白名单校验、License/
   用户额度校验、`AgentToolCall` 持久化和固定 10 积分预留；事务提交后才允许 adapter 外发。
7. **不自动重放业务调用：** DataTap 参数错误、空结果、供应商错误不由 Harness/Gateway 改参、换工具
   或自动重试。基础设施崩溃最多新建一次 Attempt；结果不明的 MCP 调用置 `unknown`、保留预留，
   不作为 Evidence，不自动重发。
8. **一次消息一个 Runtime：** 创建 Run 时根据租户配置选择 `current|pi` 并写死；切换配置只影响新
   Run。同一真实消息不能被两个 Runtime 执行。
9. **会话互斥：** 同一 `AgentSession` 最多一个活动 Run；互斥由持久字段与行锁保证，不依赖进程内锁。
10. **事件与终态：** FastAPI 是稳定产品事件的唯一生产者；Gateway source event 先幂等落库，再映射
    到现有 SSE。assistant `message.completed` 必须先于唯一终态事件；终态事件仍由
    `AgentEventStream.settle_terminal()` 在同一事务收口。
11. **账本唯一：** B4 后租户钱包是新旧 Runtime 的统一真实账本；旧用户钱包表只作迁移审计和一个
    发布周期的只读兼容，不得双写成两个事实源。
12. **可回滚：** `current` Runtime、旧 POC 和历史 Artifact/Run 均保留；本计划不删除
    `pi-runtime/`、旧 Runtime 或历史迁移。

---

## 2. 目标文件结构

```text
backend/app/
  tenancy/
    __init__.py
    models.py                         # Tenant / TenantMembership
    schemas.py
    service.py                        # 当前用户租户解析与隔离
  licensing/
    __init__.py
    models.py                         # TenantLicense
    service.py                        # feature/有效期/并发授权
  runtime_config/
    __init__.py
    models.py                         # RuntimeConfigVersion / EncryptedRuntimeSecret
    crypto.py                         # AES-256-GCM value object
    schemas.py
    service.py                        # active config 与不可变 Run snapshot
  pi_gateway/
    __init__.py
    auth.py                           # 请求 HMAC + nonce 防重放 + lease token
    contracts.py                      # 内部 API 严格 DTO
    models.py                         # gateway/queue/usage 辅助表
    scheduler.py                      # tenant fair claim + session/user/license 限制
    accounting.py                     # MCP preflight/finalize + model usage
    events.py                         # source event 幂等与产品事件投影
    internal_tools.py                 # B0 内部工具生产桥
    service.py                        # claim/heartbeat/terminal/recovery 编排
    router.py                         # /api/v1/internal/pi-gateway
  admin/
    gateway_service.py                # 六模块管理读写与诊断查询
backend/migrations/versions/
  0037_tenant_control_plane.py
  0038_runtime_config_secrets.py
  0039_pi_gateway_control_plane.py
  0040_tenant_billing_usage.py
backend/tests/
  tenancy/**
  licensing/**
  runtime_config/**
  pi_gateway/**
  integration/test_pi_gateway_mysql.py
pi-gateway/
  package.json
  package-lock.json
  tsconfig.json
  vitest.config.ts
  src/
    protocol.ts                       # FastAPI 内部协议镜像
    control-plane-client.ts           # HMAC/lease HTTP client
    gateway.ts                        # claim loop、容量、draining
    worker-pool.ts                    # 每 Run 隔离子 Worker
    worker-entry.ts
    pi-session.ts                     # createAgentSession 工厂与销毁
    resource-loader.ts                # 仅显式审核资源
    internal-tools.ts                 # FastAPI 工具代理
    mcp-accounting-extension.ts       # tool_call 前门禁，tool_result 后结算
    event-projector.ts                # Pi SDK event → Gateway source event
    secret-env.ts                     # 内存环境构建与清理
    server.ts                         # health/metrics + 进程生命周期
  tests/**
src/
  api/adminGateway.ts
  components/admin/
    AdminNavigation.tsx
    TenantAdmin.tsx
    LicenseAdmin.tsx
    UsageAdmin.tsx
    PiRuntimeAdmin.tsx
    RuntimeConfigAdmin.tsx
    RunDiagnostics.tsx
docs/runbooks/pi-agent-gateway.md
docs/qa/pi-agent-gateway-local-uat.md
```

---

## Task 1（B1A）：租户身份、成员关系与 License 基础

**Files**

- Create: `backend/app/tenancy/{__init__.py,models.py,schemas.py,service.py}`
- Create: `backend/app/licensing/{__init__.py,models.py,service.py}`
- Create: `backend/migrations/versions/0037_tenant_control_plane.py`
- Modify: `backend/app/identity/models.py`
- Modify: `backend/app/identity/{schemas.py,service.py,router.py}`
- Modify: `backend/app/agent_runtime/models.py`
- Modify: `backend/app/agent_runtime/{router.py,kol_detail.py,utility.py,reviewer.py}`
- Modify: `backend/app/pi_runtime_poc/comparison.py`
- Modify: `backend/app/admin/service.py`
- Modify: `backend/app/db/models.py`
- Modify: `src/api/contracts.ts`
- Test: `src/api/client.test.ts`
- Test: `backend/tests/tenancy/test_service.py`
- Test: `backend/tests/licensing/test_service.py`
- Test: `backend/tests/identity/test_tenant_provisioning.py`
- Modify/Test: `backend/tests/admin/test_admin_users.py`
- Test: `backend/tests/integration/test_tenant_migration_mysql.py`

**稳定接口**

```python
class TenantContext(BaseModel):
    tenant_id: str
    user_id: str
    membership_role: Literal["owner", "admin", "member"]

class LicenseDecision(BaseModel):
    allowed: bool
    code: Literal[
        "ok", "license_inactive", "license_not_started", "license_expired",
        "feature_disabled", "tenant_concurrency_exceeded", "user_concurrency_exceeded"
    ]
    max_tenant_concurrency: int
    max_user_concurrency: int

TenantService.resolve_user(user_id: str, *, for_update: bool = False) -> TenantContext
LicenseService.authorize_run(tenant_id: str, user_id: str, feature: str) -> LicenseDecision
```

- `Tenant`: `id/slug/name/status/is_internal/runtime_backend/license_status/active_license_id/
  created_at/updated_at`；`runtime_backend=current|pi` 初始统一回填 `current`，`license_status` 是
  `active|suspended`。
- `TenantMembership`: `(tenant_id,user_id)` 唯一，B 阶段再以 `user_id` 唯一约束强制一个用户只属于一个
  租户；`role=owner|admin|member`、`status=active|disabled`。
- `TenantLicense`: append-only 版本，`(tenant_id,version)` 唯一，含 `valid_from/valid_until/features_json/
  max_concurrent_runs/max_user_concurrent_runs/created_by/created_at`；激活只更新 Tenant 的
  `active_license_id`，暂停只更新 `license_status`，不改写历史 License 内容。
- 0037 同时给 `AgentSession` 与 `AgentRun` 增加并回填非空 `tenant_id`；Run tenant 必须与 Session
  tenant 相等，使历史归属不依赖未来 membership 变化，并让 License 并发查询从本任务开始就能按
  租户统计。Run 索引为 `(tenant_id,status,created_at,id)`。
- 迁移为每个存量用户建立一个确定性的 legacy tenant 与 active membership，避免自动把彼此无关的
  旧用户合入共享钱包；为每个 legacy tenant 建立保守 License，`runtime_backend=current`，Pi feature
  默认关闭。迁移可重复读取验证，但 downgrade 在已有非 legacy 多成员租户时 fail-closed。
- B1 后所有新用户在身份创建的同一事务中获得 personal tenant/membership/License；现有 admin 创建用户
  在 B6 支持显式 tenant 前也走这一安全默认。所有生产与 POC `AgentRun` 创建入口必须通过
  `TenantService.resolve_user()` 写 Session/Run tenant_id，内部/重试/达人详情 Run 继承父 Run 或 Session
  固定租户，
  不能在构造器中猜默认租户。

- [x] **Step 1：红灯。** 写服务测试，覆盖跨租户用户、禁用 membership、过期/未开始/暂停 License、
  feature 缺失、租户/用户并发边界和所有失败不泄漏其他租户；运行聚焦 pytest，预期模块不存在。
- [x] **Step 2：迁移红灯。** 用精确 MySQL 测试库从 0036 升到 0037，断言每个旧用户恰有一个不同
  legacy tenant、membership 和 License；运行 downgrade 安全测试。
- [x] **Step 3：最小实现。** 所有时间比较使用 UTC naive 数据库时间；`features_json` 只接受固定
  feature slug；并发统计只计算 queued/running/reviewing，且查询必须含 tenant_id。
- [x] **Step 4：接入身份 DTO 与所有 Run 创建器。** `GET /users/me` 增加 tenant id/name/membership
  role 并镜像前端类型；逐一覆盖 message、retry、utility、kol detail、历史 reviewer 与 POC Run，确保
  不存在 NULL 或从其他用户继承 tenant。
- [x] **Step 5：绿灯与回归。** 运行 tenancy/licensing/identity/admin 聚焦测试、迁移 upgrade/downgrade、
  范围 Ruff、前端 API 聚焦 Vitest、`npm run lint` 和 `git diff --check`。
- [x] **Step 6：审查。** 检索所有新增 `select(Tenant*|TenantMembership*)`，确认非平台管理员路径都有
  tenant 过滤；Critical/Important 必须为 0。
- [x] **Step 7：Commit。** `feat: add tenant and license control plane`。

---

## Task 2（B1B）：版本化 Runtime 配置、加密密钥与 Run snapshot

**Files**

- Create: `backend/app/runtime_config/{__init__.py,models.py,crypto.py,schemas.py,service.py}`
- Create: `backend/migrations/versions/0038_runtime_config_secrets.py`
- Modify: `backend/app/core/config.py`
- Modify: `.env.example`
- Modify: `backend/app/agent_runtime/models.py`
- Modify: `backend/app/agent_runtime/router.py`
- Modify: `backend/app/agent_runtime/{kol_detail.py,utility.py,reviewer.py}`
- Modify: `backend/app/pi_runtime_poc/comparison.py`
- Modify: `backend/app/db/models.py`
- Test: `backend/tests/runtime_config/{test_crypto.py,test_service.py,test_snapshots.py}`
- Test: `backend/tests/agent_runtime/test_runtime_backend_snapshot.py`

**稳定接口**

```python
class RuntimeConfigSnapshot(BaseModel):
    config_version_id: str
    runtime_contract_version: Literal["marketing_runtime_v1"]
    runtime_backend: Literal["current", "pi"]
    model: dict[str, str | int | float | None]       # endpoint 仅 masked_origin
    datatap: dict[str, object]                       # service slug + schema digest，无 token
    capability_pack: dict[str, object]              # B0 snapshot/digests
    limits: dict[str, int | float]
    billing: dict[str, int | str]

class RuntimeSecretBundle(BaseModel):
    model_base_url: SecretStr
    model_api_key: SecretStr
    datatap_token: SecretStr
    datatap_urls: dict[str, SecretStr]

SecretCipher.encrypt(plaintext: SecretStr, *, aad: bytes) -> EncryptedSecretValue
SecretCipher.decrypt(value: EncryptedSecretValue, *, aad: bytes) -> SecretStr
RuntimeConfigService.snapshot_for_new_run(tenant_id: str) -> RuntimeConfigSnapshot
RuntimeConfigService.resolve_secret_bundle(config_version_id: str, run_id: str) -> RuntimeSecretBundle
```

- 使用 `cryptography` AES-256-GCM；环境变量 `RUNTIME_SECRET_MASTER_KEYS` 是
  `key_version:base64-32-bytes` 映射，`RUNTIME_SECRET_ACTIVE_KEY_VERSION` 指定写入版本。AAD 精确绑定
  `tenant_id:secret_id:kind:key_version`，错 tenant/kind/version 一律解密失败。
- `RuntimeConfigVersion` 为 append-only：`scope=system|tenant`、`tenant_id`、`version`、
  `status=draft|active|retired`、`config_json`、`secret_refs_json`、`created_by/created_at/activated_at`。
  0038 给 Tenant 增加 `active_runtime_config_id`；激活操作锁 Tenant，同一租户最多一个 active tenant
  override，系统默认最多一个 active。
- `EncryptedRuntimeSecret` 只存 `ciphertext/nonce/key_version/fingerprint/masked_value/status`，无明文列。
- 0038 建立一个只兼容 `current` 的 `legacy_env_v1` 系统配置，令存量 current Run 继续从进程 Settings
  取 secret；snapshot 只记录 masked endpoint/model/config id，不写环境值。该配置不能被 Pi claim，
  `resolve_secret_bundle()` 对它 fail-closed。租户切 Pi 前必须由管理 API 创建并激活完整加密配置。
- `AgentRun` 复用 0037 的非空 `tenant_id`，新增 `runtime_backend`、`runtime_config_version_id`、
  `runtime_config_snapshot_json`、`queued_at`；迁移用 legacy tenant、`current` 和当前安全默认生成历史
  snapshot，不把 `.env` 密钥写进数据库。
- 创建消息的同一事务中锁 Session、解析 Tenant/License/config，固定 backend/snapshot 后才写 Run；
  幂等重放返回原 Run，不重新选择 backend/config。
- 内部 Run、retry、kol detail 和 POC Run 不得绕过 snapshot：生产子 Run 继承父 Run 的 backend/config
  version 并重新生成标明 parent 的不可变 snapshot；无父 Run 的内部任务按当前租户 active config 创建。
  POC 继续固定 `runtime_backend=pi` 与 POC 隔离 snapshot，不能读取生产 secret bundle。

- [x] **Step 1：红灯。** 覆盖密文随机 nonce、AAD 跨租户失败、key rotation 旧读新写、任何 API DTO
  无明文/Authorization/token/key、active config 唯一和历史版本不可改。
- [x] **Step 2：Run 红灯。** 覆盖租户从 current 切 pi 后旧幂等消息仍返回 current Run、新消息才选 pi；
  message/retry/utility/kol detail/legacy reviewer/POC 六类创建入口都具有正确 snapshot；缺 active config/
  pack digest/runtime contract 不兼容时建 Run 整体回滚。
- [x] **Step 3：最小实现与迁移。** 先扩表 nullable、回填、再改 non-null；索引
  `(tenant_id,runtime_backend,status,queued_at,id)`；不得修改旧迁移。
- [x] **Step 4：配置日志门禁。** `repr`/Pydantic dump/异常只包含 masked value 和 fingerprint；新增
  secret 扫描测试直接扫描 response、Run snapshot、admin audit detail 和日志 caplog。
- [x] **Step 5：绿灯与回归。** runtime_config + agent router + migration 测试、Ruff、diff check。
- [x] **Step 6：审查。** 确认只有 `resolve_secret_bundle()` 能调用 decrypt，且普通/管理 API 无调用者。
- [x] **Step 7：Commit。** `feat: snapshot encrypted tenant runtime config`。

---

## Task 3（B2A）：建立独立 `pi-gateway` 与锁定 SDK Session 工厂

**Files**

- Create: `pi-gateway/{package.json,package-lock.json,tsconfig.json,vitest.config.ts}`
- Create: `pi-gateway/src/{protocol.ts,pi-session.ts,resource-loader.ts,secret-env.ts,worker-entry.ts}`
- Create: `pi-gateway/tests/{sdk-contract.test.ts,pi-session.test.ts,resource-loader.test.ts,secret-env.test.ts}`
- Modify: `.gitignore`
- Modify: `docs/qa/pi-runtime-version-probe.md`

**稳定接口**

```ts
export interface PiSessionFactory {
  create(work: ClaimedRun, secrets: SecretBundle): Promise<PiRunSession>;
}

export interface PiRunSession {
  prompt(content: string): Promise<void>;
  subscribe(listener: (event: PiSdkEvent) => void): () => void;
  abort(): Promise<void>;
  dispose(): Promise<void>;
}

export function createProductionPiSession(
  work: ClaimedRun,
  secrets: SecretBundle,
): Promise<PiRunSession>;
```

- `package.json` 精确锁定当前四个 Pi/adapter 版本，不使用 `^`、`~` 或 `latest`。
- 直接使用 SDK `createAgentSession()`；Session 必须是 `SessionManager.inMemory()`；配置
  `noTools: "builtin"`，再以显式 allowlist 限制 adapter `mcp` 与 B0 内部工具。
- 凭证与模型注册必须使用 `AuthStorage.inMemory()`、`setRuntimeApiKey()` 和
  `ModelRegistry.inMemory()`/`registerProvider()`；运行设置使用 `SettingsManager.inMemory()`，不得创建
  `auth.json`、`models.json` 或 settings 文件。
- ResourceLoader 只返回仓库固定的 production adapter 与审计/内部工具 Extension，Skills/context 列表
  必须为空；B0 专项 Skill 仍只能经 `load_marketing_skill` 动态读取，不恢复第二个 Pi Skill 入口。
  `cwd` 和 `agentDir` 指向每 Run `mkdtemp` 目录，禁止读取 HOME、项目自动 Extension/Skill/context。
  临时目录权限 0700，结束递归删除。
- Worker 在临时 cwd 生成固定 `.mcp.json`，内容只能是四个审核服务、adapter 安全设置和环境变量引用，
  不含 endpoint/token；模型/auth 配置仍全部内存化。测试对文件 digest 与 B0 snapshot 中 adapter catalog
  digest 做一致性检查。
- `secret-env.ts` 只构建当前 Run 子 Worker 的环境映射；不得修改主进程 `process.env`，不得把值序列化。
- 每个 `worker-entry` 只执行一个 Run。父进程把非密 claim 经 Node IPC 传入，把 secret 仅放在该子进程
  的 spawn env；子进程 ready 后父进程立即删除 secret bundle/env 引用。结束无论成功/异常/取消都执行
  `abort → unsubscribe → dispose → temp cleanup`。

- [x] **Step 1：SDK 契约红灯。** 从已安装 0.79.10 类型与运行时断言
  `createAgentSession/SessionManager.inMemory/tool_call block/AgentSession abort+dispose` 的精确形状；测试不
  调模型。缺任一能力直接停止，不改用 CLI 或升级依赖。
- [x] **Step 2：Session 工厂红灯。** fake provider 下断言一个 Worker 一个内存 Session，两个租户的
  system prompt、消息、工具状态、临时目录和 env 互不可见；异常与 SIGTERM 都清理。
- [x] **Step 3：最小实现。** 显式构建 model/auth/resource/session manager；禁止 Pi 内建工具和自动
  discover；B0 root policy 作为真正 system 参数注入，普通 user prompt 不重复正文。
- [x] **Step 4：依赖审计。** `npm ls` 断言精确版本；静态扫描 production src 不出现
  `child_process.exec`、shell 拼接、HOME 继承、MySQL driver、任意 fetch host。
- [x] **Step 5：绿灯。** `npm test`、`npm run typecheck`、`npm run build`。
- [x] **Step 6：文档校正。** 在版本探针追加“生产锁定组合”章节，保留历史 0.84.1 事实，不覆盖旧文。
- [x] **Step 7：Commit。** `feat: add isolated pi sdk gateway worker`。

---

## Task 4（B2B）：FastAPI↔Gateway 签名协议与生产内部工具桥

**Files**

- Create: `backend/app/pi_gateway/{__init__.py,auth.py,contracts.py,models.py,internal_tools.py,service.py,router.py}`
- Create: `backend/app/agent_runtime/tools/pi_internal_tools.py` (生产与 POC 共用的 B0 内部工具实现)
- Create: `backend/migrations/versions/0039_pi_gateway_control_plane.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/app/core/config.py`
- Modify: `.env.example`
- Modify: `backend/app/db/models.py`
- Refactor: `backend/app/pi_runtime_poc/internal_tools.py`
- Create: `pi-gateway/src/{control-plane-client.ts,internal-tools.ts}`
- Modify: `pi-gateway/src/secret-env.ts`
- Test: `backend/tests/pi_gateway/{test_auth.py,test_contracts.py,test_claim_service.py,test_internal_tools.py}`
- Test: `pi-gateway/tests/{control-plane-client.test.ts,internal-tools.test.ts,secret-envelope.test.ts}`

**内部协议**

```text
POST /api/v1/internal/pi-gateway/v1/claims
POST /api/v1/internal/pi-gateway/v1/runs/{run_id}/heartbeat
POST /api/v1/internal/pi-gateway/v1/runs/{run_id}/events
POST /api/v1/internal/pi-gateway/v1/runs/{run_id}/internal-tools
POST /api/v1/internal/pi-gateway/v1/runs/{run_id}/terminal
```

- 生产 `PI_GATEWAY_CONTROL_PLANE_URL` 必须是 HTTPS；仅 development/test 且 host 为 loopback 时允许 HTTP。
  HMAC 只提供身份与完整性，不得被误当作传输加密。
- 所有请求必须带 `X-Pi-Gateway-Id/X-Pi-Timestamp/X-Pi-Nonce/X-Pi-Signature`。签名串精确为
  `METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(body)`，HMAC-SHA256 使用独立
  `PI_GATEWAY_INTERNAL_SECRET`；时间偏差上限 30 秒。
- `PiGatewayRequestNonce(gateway_id,nonce,expires_at)` 唯一，验证与插入同事务；重复 nonce 拒绝，恢复
  循环定期清理过期行。响应与日志不得回显签名。
- `PI_GATEWAY_ALLOWED_IDS` 是部署环境中的固定 gateway id 白名单；白名单 id 首次签名 claim 可创建
  `PiGatewayInstance`，非白名单 id 即使 HMAC 正确也拒绝。实例后续由 claims/heartbeat 更新健康状态。
- claim 成功返回短期一次 Run lease token；数据库只存 SHA-256 hash。除 claims 外的 Run 端点还必须
  带 `X-Pi-Run-Lease`，验证 gateway/run/attempt/hash/expiry 后才操作。
- secret bundle 不以明文 JSON 字段返回。FastAPI 用 HKDF-SHA256 从 lease token 派生本 Run 数据密钥，
  以 AES-256-GCM 和 `run_id:attempt_id:config_version_id:gateway_id` AAD 生成
  `RuntimeSecretEnvelope(alg,nonce,ciphertext)`；Node 验证 AAD 后只在内存解封。public/admin DTO 与日志
  仍只有 masked/fingerprint。
- Pydantic DTO 全部 `extra="forbid"`，有界字符串/数组/批量条数。Claim 响应包含 snapshot、受限会话
  transcript、B0 pack/root policy/skill catalog、受控工具定义、审核 MCP catalog 和临时 secret bundle；
  MCP catalog 每项含服务端生成的 `catalog_entry_id`、adapter-visible name、service、remote name、input
  schema digest。响应不含任意文件路径、数据库信息或其他租户配置。
- 把 POC 中 B0 内部工具注册表的公共部分提到生产桥；POC 继续零积分旁路，production context 必须从
  lease 解析 tenant/user/session/run，不接受 body 中身份字段。`publish_artifacts`、
  `request_clarification` 保留租约门禁。

- [x] **Step 1：认证红灯。** 覆盖错误 method/path/body hash、旧时间、重复 nonce、未知 gateway、错 lease、
  跨 tenant/run/attempt、非 HTTPS production URL、secret envelope 错 AAD/篡改和日志泄漏；统一返回
  安全 401/404/409。
- [x] **Step 2：协议镜像红灯。** 同一 JSON fixture 同时通过 Pydantic 与 TypeScript parser；额外字段、
  超长 delta、伪造 tenant_id、未知事件全部拒绝。
- [x] **Step 3：迁移和最小服务。** 0039 一次性新增 `PiGatewayInstance`、nonce、
  `PiTenantQueueState`、`AgentSession.active_run_id`、AgentRun gateway lease/infra retry 字段和
  `AgentEvent.source_event_id`；`(run_id,source_event_id)` 唯一且 NULL 兼容旧事件。后续 Task 5/6 只
  启用这些字段，不回改已提交迁移。
- [x] **Step 4：内部工具桥。** 复用 B0 loader/Builder/Publication；生产 Gateway 不能调用 POC 的
  `PiPocSettingsGuard`、零积分 audit 或 POC 数据库门禁。
- [x] **Step 5：Node client。** 只允许构造时注入的 FastAPI origin；禁止 3xx；请求超时、abort、重试规则
  明确：GET-like heartbeat 可按同 nonce 新请求重试一次，任何工具/终态写请求依赖幂等 id，不盲重发。
- [x] **Step 6：绿灯与回归。** Python/Node 聚焦测试、POC 全量回归、Ruff、typecheck、build、diff check。
- [x] **Step 7：Commit。** `feat: add authenticated pi gateway control protocol`。

---

## Task 5（B3A）：持久队列、租户公平调度、会话互斥与 draining

**Files**

- Create: `backend/app/pi_gateway/scheduler.py`
- Modify: `backend/app/pi_gateway/{models.py,service.py,router.py}`
- Modify: `backend/app/agent_runtime/{models.py,router.py,events.py}`
- Create: `backend/migrations/versions/0039a_pi_session_mutex_backfill.py` (依赖 0039 的
  `active_run_id`，对存量活动 Run 回填并对多活动 Session fail-closed)
- Modify: `backend/app/agent_runtime/repository.py`
- Create: `pi-gateway/src/{gateway.ts,worker-pool.ts,server.ts}`
- Test: `backend/tests/pi_gateway/{test_scheduler.py,test_scheduler_concurrency.py,test_draining.py}`
- Test: `backend/tests/{agent_runtime/test_repository.py,agent_runtime/test_utility_wiring.py,
  pi_gateway/test_internal_tools.py,test_phase2_migrations.py}`
- Modify/Test: `backend/tests/integration/test_tenant_migration_mysql.py`
- Test: `backend/tests/integration/test_pi_gateway_mysql.py`
- Test: `pi-gateway/tests/{gateway.test.ts,worker-pool.test.ts,shutdown.test.ts}`

**稳定接口**

```python
PiRunScheduler.claim_next(gateway_id: str, capacity: int) -> ClaimedRun | None
PiRunScheduler.heartbeat(run_id: str, attempt_id: str, lease: str) -> HeartbeatDecision
PiRunScheduler.set_gateway_mode(gateway_id: str, mode: Literal["active", "draining"]) -> None
```

- `AgentSession.active_run_id` 是持久互斥槽。消息创建在锁定 Session 后校验为空并写新 Run id；唯一终态
  事务只在槽仍指向本 Run 时清空。迁移对既有活动 Run 按 `created_at,id` 选择一个，发现同 Session
  多活动 Run 直接 fail-closed，不擅自终止。
- `PiTenantQueueState(tenant_id,last_claimed_at,active_runs,version)` 提供轮转游标。claim 事务：
  1) 锁 eligible tenant state（License/tenant/user/gateway 容量均可用）；2) 按
  `last_claimed_at NULL FIRST, oldest queued_at, tenant_id` 选 tenant；3) 锁该租户最老 queued Run；
  4) 建 Attempt、写 gateway lease、增加 active count、更新游标；5) commit 后返回 secrets。
- MySQL 8 使用 `FOR UPDATE SKIP LOCKED`；SQLite 单测覆盖语义，真实 MySQL 测试证明两个 Gateway 并发
  claim 不会领取同一 Run、不会突破并发上限。
- Gateway `desired_capacity` 下降或 mode=draining 时停止新 claim，现有 Worker 自然完成；关闭超时只
  abort 当前精确 Worker，Run 留给恢复，不直接改终态。容量上升不重启服务。

- [x] **Step 1：红灯。** 以租户 A 20 个、B 2 个、C 2 个 queued Run 验证前六次 claim 不饿死 B/C；
  同租户 FIFO、优先级相同时用 `queued_at,id`；session/user/license/global limit 全覆盖。
- [x] **Step 2：互斥红灯。** 两个并发 `append_message` 同 Session 只能一个成功；终态与新消息竞态不
  清掉新 Run 的槽；current/pi 共用同一互斥。
- [x] **Step 3：最小实现。** 调度不按 UUID 排序；所有计数与 lease 变更在同一事务；claim 响应生成
  失败时回滚 Attempt/active count，不把 Run 留在 running。
- [x] **Step 4：Node 容量池。** fake control plane 验证 capacity、backoff、有界 jitter、draining、
  SIGTERM 和子 Worker 精确清理；无 unhandled rejection。
- [x] **Step 5：真实 MySQL 并发绿灯。** 至少 20 轮双 claim + heartbeat + terminal 竞态，断言无重复
  Attempt/Event sequence、无负 active count、无脏 session slot。
- [x] **Step 6：范围回归和审查。** agent router/executor/recovery 既有测试必须保持通过；审查锁顺序统一
  `Tenant → License/Wallet → Session → Run → child rows`，避免反向死锁。
- [ ] **Step 7：Commit。** `feat: schedule pi runs fairly across tenants`。

---

## Task 6（B3B）：基础设施恢复、一次 Attempt、unknown 调用与取消

**Files**

- Modify: `backend/app/pi_gateway/{service.py,scheduler.py,events.py}`
- Modify: `backend/app/agent_runtime/{repository.py,recovery.py,transcript.py,state.py,events.py}`
- Modify: `pi-gateway/src/{control-plane-client.ts,gateway.ts,worker-pool.ts,worker-entry.ts,pi-session.ts}`
- Test: `backend/tests/pi_gateway/{test_recovery.py,test_cancel.py,test_terminal.py}`
- Test: `backend/tests/agent_runtime/test_pi_attempt_recovery.py`
- Test: `pi-gateway/tests/{control-plane-client.test.ts,gateway.test.ts,worker-crash.test.ts,cancel.test.ts}`

**恢复分类**

```python
InfrastructureFailure = Literal[
    "gateway_lost", "worker_exited", "sdk_protocol_error", "control_plane_unreachable"
]
BusinessFailure = Literal[
    "model_error", "tool_failed_confirmed", "invalid_model_output", "publication_rejected"
]
```

- `AgentRun.infrastructure_retry_count` 初始 0。只有上述基础设施失败且无取消请求时可原子增加到 1、结束
  当前 Attempt、清 gateway lease、重置 queued；第二次基础设施失败直接 `run.failed`。
- 新 Attempt 从数据库的 user message、已完成 Step、settled Evidence、已发布 Version 与 B0 snapshot 重建；
  不恢复 Pi 内存 Session，不重放 incomplete Step。
- 崩溃时当前 Attempt 的 `reserved|running` ToolCall 全部置 `unknown` 并保留预留；新 Attempt context
  只显示“结果待核对”，不能当 success Evidence，也不能重新使用同 logical call id 外发。
- 明确 `definitely_not_sent` 只能由 preflight 未提交或 adapter 证明未发出；只有现有业务规则允许的单次
  retry 才可派发，Gateway 崩溃不能自行推断 definitely-not-sent。
- heartbeat 返回 `cancel_requested=true` 时 Worker 调 `session.abort()`，停止新工具，发送 cancel ack；
  FastAPI 释放未外发预留/活动 Draft并以现有终态事务收口。取消时 Worker 崩溃由恢复循环直接 cancel，
  不创建恢复 Attempt。

- [x] **Step 1：红灯矩阵。** 分别模拟 claim 后/模型中/tool preflight 前/preflight 后外发前/外发后
  result 前/result 后/Artifact 发布前/终态提交前崩溃，断言每个窗口唯一且安全的状态。
- [x] **Step 2：一次恢复红灯。** 第一次 gateway_lost 产生 Attempt 2，第二次直接 failed；业务错误不
  消耗 infra retry；用户 resume 的 Attempt 与 infra retry 计数相互独立。
- [x] **Step 3：最小实现。** 恢复循环只处理 runtime_backend=pi 的过期 gateway lease；current 路径
  继续使用既有 executor lease。终态/接管均以 `populate_existing` 当前读复核。
- [x] **Step 4：Node crash/abort。** 子 Worker exit code/signal 映射稳定错误码；父进程只上报，不伪造
  Tool result；FastAPI 不可达时本地只保存有界内存事件，超过上限 abort 并按 infra fail。
- [x] **Step 5：绿灯。** Python/Node 恢复与取消测试、现有 Agent recovery/unknown reconciliation 全量、
  真实 MySQL 窗口测试、Ruff/typecheck/build。
- [x] **Step 6：审查。** 断言 `dispatch_count` 不因基础设施恢复增加，unknown 没有 Evidence，所有
  terminal path 恰好一个终态事件。
- [ ] **Step 7：Commit。** `feat: recover pi infrastructure failures once`。

---

## Task 7（B4A）：统一租户钱包、用户周期额度与 MCP 前置结算

**Files**

- Modify: `backend/app/billing/{models.py,schemas.py,service.py,router.py}`
- Modify: `backend/app/mcp_gateway/accounting.py`
- Modify: `backend/app/admin/service.py`
- Create: `backend/app/pi_gateway/accounting.py`
- Create: `backend/migrations/versions/0040_tenant_billing_usage.py`
- Modify: `backend/app/agent_runtime/tools/mcp.py`
- Modify: `backend/app/pi_gateway/{contracts.py,router.py,service.py}`
- Modify: `pi-gateway/src/{mcp-accounting-extension.ts,control-plane-client.ts,pi-session.ts}`
- Test: `backend/tests/billing/{test_tenant_wallet.py,test_user_quota.py,test_wallet_compat.py}`
- Test: `backend/tests/mcp_gateway/test_tenant_accounting.py`
- Modify/Test: `backend/tests/admin/{test_admin_points.py,test_admin_points_history.py,test_admin_users.py}`
- Test: `backend/tests/pi_gateway/{test_mcp_preflight.py,test_mcp_finalize.py}`
- Test: `pi-gateway/tests/mcp-accounting-extension.test.ts`

**账务模型与接口**

```python
TenantAccountingService.reserve_mcp_call(context: McpPreflightContext) -> McpPermit
TenantAccountingService.settle_mcp_call(permit_id: str, payload: object) -> EvidenceReceipt
TenantAccountingService.fail_mcp_call(
    permit_id: str,
    classification: Literal["definitely_not_sent", "failed_confirmed", "result_unknown"],
) -> None
```

- `TenantWallet(tenant_id,balance,reserved,version,updated_at)`；
  `TenantWalletTransaction` 为不可变账本，幂等键全局唯一，含 tenant/user/run/tool_call/reference。
- `TenantUserQuotaPolicy(tenant_id,user_id,period=monthly,points_limit,status)` 与
  `TenantUserQuotaUsage(period_start,period_end,spent,reserved,version)`；reserve 同时锁 wallet 与 usage，
  任何一侧不足都整体回滚。
- 0040 同时创建 Task 8 将使用的 `RuntimeUsageRecord` 表及索引，避免后续回改迁移；Task 7 只注册
  模型，Task 8 才接入 usage 写入和汇总服务。
- 0037 的 legacy tenant 与旧 `Wallet` 一一对应；0040 将旧余额/预留精确复制到 tenant wallet，并为
  旧流水记录迁移校验摘要。迁移要求无无法归属的悬挂预留；不删除旧表。B4 后 `WalletService(user_id)`
  成为解析 membership 后调用 tenant ledger 的兼容 facade，current 与 pi 共用同一事实源。
- `/wallet` 继续返回 `balance/reserved/available`，但语义是当前 tenant pool 对当前用户可用额度的交集；
  管理端用户积分旧操作改为租户池调整并在响应明确 tenant_id，禁止给同租户成员重复展示为独立余额。
- Pi `tool_call` hook 在 adapter 真正执行前调用 preflight。只有含明确 `tool/server/args` 的 call 才收费；
  MCP connect/search/list 不创建 ToolCall、不扣分。preflight 校验动态 allowlist、渠道、License feature、
  wallet/quota、args schema，按 claim 中 adapter-visible name 查出 `catalog_entry_id`；FastAPI 再以 Run
  snapshot digest 复核 catalog entry，提交 running ToolCall 与 reserve 后返回 permit。模型输入不能
  自报 remote name 或绕过 catalog 映射。
- hook 无 permit 必须 `{block:true, reason:<stable-code>}`；不得因 control-plane audit 失败放行。`tool_result`
  根据 adapter `details.mode=call/mcpResult/error` 做 success/confirmed failure/unknown 收口，复用现有 output
  schema、Normalization、EvidenceWriter 和 reconciliation。

- [x] **Step 1：迁移/账务红灯。** 覆盖旧余额与预留精确复制、同租户多用户共享余额、用户月额度、并发
  reserve、幂等 settle/release、负余额/负 reserved 不变量、旧表不再写。
- [x] **Step 2：hook 红灯。** fake adapter 断言调用顺序必为 preflight commit → MCP call → finalize；
  preflight 401/409/余额不足/License 失效全部 0 MCP；search/list 0 计费。
- [x] **Step 3：最小实现。** 提取现有 `DurableToolCallCoordinator` 的账务端口，使 current/pi 共用状态机；
  不复制一套状态分类。积分固定从 Settings 校验为 10，不接受 Gateway body 报价。
- [x] **Step 4：unknown 与核对。** unknown 保留 tenant wallet + user quota reserved；自动/人工核对成功 settle，
  确认失败 release，keep_unknown 不动账；审计 append-only。
- [x] **Step 5：绿灯。** billing、current MCP、Pi preflight、recovery、admin points、真实 MySQL并发测试，
  再运行后端全量与 Node 测试。
- [x] **Step 6：审查。** 对所有 Wallet/WalletTransaction 写入口静态盘点，确认 B4 后除迁移与只读兼容外
  为 0；检查所有外发 MCP 都有已提交 permit。
- [ ] **Step 7：Commit。** `feat: bill all agent mcp calls from tenant wallet`。

---

## Task 8（B4B）：模型 Token/成本、用量汇总与账务对账

**Files**

- Modify: `backend/app/billing/models.py`
- Modify: `backend/app/pi_gateway/{accounting.py,contracts.py,events.py,service.py}`
- Modify: `pi-gateway/src/{event-projector.ts,control-plane-client.ts}`
- Test: `backend/tests/pi_gateway/{test_model_usage.py,test_usage_reconciliation.py}`
- Test: `backend/tests/billing/test_usage_aggregation.py`
- Test: `pi-gateway/tests/event-projector.test.ts`

- `RuntimeUsageRecord` append-only，唯一 `(run_id,attempt_id,source_event_id,kind)`；含 tenant/user/backend/
  provider/model/input/output/cache tokens、`cost_micros`、currency、upstream_request_id、observed_at。
- 模型 token 只记录成本，不扣业务积分；上游未给 usage 时记录 `usage_status=unavailable`，不得估算成 0。
- cost 来自 Run snapshot 中管理员发布的 price table；历史 snapshot 缺价格时 `cost_status=unpriced`。
- 每次 terminal 后运行纯查询 reconciliation：MCP settled points 等于 tenant ledger settle、reserved/unknown
  与账本 reserved 对齐、usage event 不重复；发现差异只报警和标记 `reconciliation_status=mismatch`，不
  自动改账。

- [x] **Step 1：红灯。** 覆盖 usage 重复事件、缺 usage、跨 Attempt、价格版本切换、MCP ledger mismatch、
  unknown reserved 和跨租户汇总污染。
- [x] **Step 2：最小实现。** Node 只投影 Pi SDK 实际 usage；FastAPI 根据 snapshot 定价并持久化，
  Gateway 不计算货币。
- [x] **Step 3：汇总。** 提供 tenant/user/run/day 查询服务，所有管理查询用整数 micros 累加，展示层再
  格式化；不使用 float 累计。
- [x] **Step 4：绿灯与回归。** 聚焦、billing 全量、pi-gateway Node 全量、Ruff/typecheck/build。
- [x] **Step 5：审查。** 同一 provider request 不可生成多条计费记录；usage 不进入用户 SSE/Prompt。
- [x] **Step 6：Commit。** `feat: record and reconcile pi runtime usage`。

---

## Task 9（B5）：Pi SDK 事件接入现有 SSE、终态、取消与续传

**Files**

- Create: `backend/app/pi_gateway/events.py`
- Modify: `backend/app/pi_gateway/{contracts.py,router.py,service.py}`
- Modify: `backend/app/agent_runtime/{events.py,sse.py,router.py,thinking.py}`
- Create: `pi-gateway/src/event-projector.ts`
- Test: `backend/tests/pi_gateway/{test_event_projection.py,test_event_idempotency.py,test_event_ordering.py}`
- Test: `backend/tests/agent_runtime/test_pi_sse_resume.py`
- Test: `pi-gateway/tests/event-projector.test.ts`
- Modify: `src/components/agent/{AgentRunCard.tsx,AgentThinking.tsx,AgentRunSteps.tsx}`
- Modify: `src/api/{agent.ts,taskStream.ts}`
- Test: corresponding `*.test.tsx` / `*.test.ts`

**事件契约**

- Worker 的 `source_event_id` 固定 `{attempt_id}:{worker_sequence}`，sequence 从 1 连续增长；FastAPI 以
  `(run_id,source_event_id)` 幂等。重复 batch 返回原 receipt，不重复 AgentStep/AgentEvent/Message。
- Node 只上报有界投影：agent/turn/message/tool start/end、thinking/text delta、usage、终态；不上传完整
  message snapshot、原始 prompt、Authorization、secret env 或未脱敏供应商异常。
- thinking delta 在 Node 端按 100ms 或 4KiB 先到者批量；FastAPI 继续产生
  `thinking.started/delta/completed/failed`，默认折叠。文本 delta 有界且最终由 FastAPI 合并成唯一 assistant
  message，再发 `message.completed`。
- 工具 UI 事件只含 `call_id/internal_tool_name/status/safe_summary`；MCP 参数、raw payload 和密钥不进入
  SSE。Evidence/Artifact 事件仍由 FastAPI 内部工具和 PublicationService 产生。
- 前端继续用现有 `/api/v1/agent/runs/{id}/events` 与 `Last-Event-ID`；不出现 Gateway URL。断线从 DB
  sequence 恢复，终态后流结束。

- [x] **Step 1：红灯。** 真实 Pi 0.79 fake-provider event fixture 覆盖 token 级 delta 聚合、工具成功/
  失败/unknown、重复 batch、乱序/缺口、跨 Attempt、终态前 message、Last-Event-ID。
- [x] **Step 2：最小 Node 投影。** 未知 SDK 事件忽略并计安全 diagnostic counter；原始错误只映射稳定
  code。批量内保持顺序，发送失败保留有界队列并用相同 source ids 重试。
- [x] **Step 3：FastAPI 落库。** 锁 Run 当前读分配 Step/Event sequence；拒绝未来 attempt、旧 lease、
  终态后的用户事件；terminal 继续调用现有 `settle_terminal()`。
- [x] **Step 4：前端。** 确认 thinking 默认折叠、工具安全摘要、cancel pending、completed_with_warnings、
  重连不重复文本；不增加直连 Gateway 代码。
- [x] **Step 5：绿灯。** 后端事件/SSE、Node、前端聚焦测试；前端完整 test/lint/build；后端相关回归。
- [x] **Step 6：审查。** 终态是最后一条用户可见事件；每 Run 仅一条 assistant completion；55k token
  事件 fixture 经聚合后数量有界且文本不丢失。
- [x] **Step 7：Commit。** `feat: stream pi gateway events through agent sse`。

---

## Task 10（B6A）：六模块管理后端与只读 Run 诊断

**Files**

- Create: `backend/app/admin/gateway_service.py`
- Modify: `backend/app/admin/{schemas.py,router.py,service.py,models.py}`
- Modify: `backend/app/pi_gateway/{service.py,scheduler.py}`
- Test: `backend/tests/admin/{test_tenants.py,test_licenses.py,test_runtime_config.py,test_pi_runtime.py,test_usage.py,test_run_diagnostics.py}`
- Test: `backend/tests/admin/test_gateway_admin_audit.py`

**管理模块与端点**

```text
/api/v1/admin/tenants
/api/v1/admin/tenants/{tenant_id}/users
/api/v1/admin/tenants/{tenant_id}/license
/api/v1/admin/tenants/{tenant_id}/usage
/api/v1/admin/pi-runtime/gateways
/api/v1/admin/runtime-configs
/api/v1/admin/agent-runs/{run_id}/diagnostics
```

- 租户：创建、重命名、启停；有 active Run/unknown reservation 时禁止禁用。
- 用户：把新用户直接创建到指定 tenant；B 阶段不支持把已有活跃账本用户跨租户搬迁，API 明确 409。
- License：append version/activate/suspend；并发与 feature 生效只影响新 claim，已执行 Run 不强杀。
- 用量与积分：tenant wallet 调整、用户周期额度、按日/用户/Run 的积分与模型成本聚合。
- Pi Runtime：Gateway health/capacity/active/draining/last heartbeat/version；写操作只有 desired capacity 与
  draining，不能从管理端执行 shell/重启命令。
- 模型与 MCP 配置：创建 draft、写/替换 secret、activate/retire；响应仅 masked/fingerprint；不在线编辑
  B0 Skill/Policy 正文，只选择已验证 pack 版本。
- Run 诊断严格只读：Run/Attempt/Step/ToolCall/Event/Artifact/usage/reconciliation 的安全投影；不返回
  prompt、raw Evidence、MCP args/result、密文、明文 endpoint/token 或用户手机号。
- 所有写操作使用 `Idempotency-Key`，调用现有 `_audit()` 写 `AdminAuditLog`；detail 记录 before/after
  的安全字段、tenant、actor、reason，不记录 secret 值。

- [x] **Step 1：红灯。** 每模块覆盖普通用户 403、未知/跨租户 404、幂等写、并发版本冲突、secret
  response/audit/log 泄漏、Run diagnostics 只读。
- [x] **Step 2：最小实现。** router 只做 DTO/异常映射；事务和审计在 service 同一提交边界。平台 admin
  可跨租户，但每次请求必须显式 target tenant，不提供无界全表 dump。
- [x] **Step 3：分页与索引验证。** 所有 list limit 上限 200，稳定排序和 cursor/offset；对 usage/run
  diagnostics 运行 EXPLAIN 测试或索引断言，禁止 N+1 逐行查询。
- [x] **Step 4：绿灯。** admin、tenancy/licensing/runtime config/billing/pi gateway 相关测试与后端全量，
  Ruff、diff check。
- [x] **Step 5：审查。** Critical/Important=0；重点检查 secret、跨 tenant、审计原子性和管理写操作权限。
- [x] **Step 6：Commit。** `feat: expose audited pi gateway administration api`。

---

## Task 11（B6B）：管理端导航、六模块页面与 Run 诊断界面

**Files**

- Create: `src/api/adminGateway.ts`
- Modify: `src/api/contracts.ts`
- Create: `src/components/admin/{AdminNavigation.tsx,TenantAdmin.tsx,LicenseAdmin.tsx,UsageAdmin.tsx,PiRuntimeAdmin.tsx,RuntimeConfigAdmin.tsx,RunDiagnostics.tsx}`
- Test: matching `src/components/admin/*.test.tsx`
- Modify: `src/components/AdminPanel.tsx`
- Modify: `src/components/AdminPanel.test.tsx`

- 现有账号管理成为“用户”模块，不复制其表单逻辑；新增左侧模块导航与响应式布局。
- 所有类型精确镜像后端 Literal；API client 统一走现有认证/错误处理，不在浏览器保存 Gateway secret、
  Runtime secret 或 lease token。
- Runtime 配置的 secret 输入是 write-only：提交后立即清空，页面只显示 `••••last4`、fingerprint、
  更新时间；编辑页面不能读取旧明文。
- 切换 runtime backend、激活配置、暂停 License、调整余额/额度、draining 都需要二次确认并显示“只影响
  新 Run”或具体账务影响；使用随机 Idempotency-Key。
- Run Diagnostics 仅呈现安全字段和事件时间线，unknown/reserved 有醒目标记；不得渲染 raw JSON 的
  未审计字段，不提供重放/改状态按钮。

- [x] **Step 1：API/类型红灯。** 用 compile-time fixture 与 MSW/fetch mock 覆盖所有 response/input、
  Idempotency-Key、分页和错误码。
- [x] **Step 2：页面红灯。** 覆盖六模块导航、loading/empty/error、权限、确认、write-only secret、
  draining、unknown 诊断与窄屏；先运行定向 Vitest 确认失败。
- [x] **Step 3：最小实现。** 拆分 AdminPanel，保留现有用户管理行为和测试；图表仅用于 usage 时间序列，
  其余使用表格/状态标签。
- [x] **Step 4：可访问性。** 对话框 focus trap、label、键盘关闭、状态色配文本、表格标题和移动端滚动
  都有测试。
- [x] **Step 5：绿灯。** 定向 Vitest、`npm run test`、`npm run lint`、`npm run build`。
- [x] **Step 6：审查。** 浏览器 storage/network fixture 不出现 runtime secret、lease、HMAC 或其他租户
  的数据；Critical/Important=0。
- [x] **Step 7：Commit。** `feat: add pi gateway administration console`。

---

## Task 12（B7 本地部分）：租户级灰度、全局 kill switch 与回滚演练

**Files**

- Modify: `backend/app/tenancy/{models.py,schemas.py,service.py}`
- Modify: `backend/app/runtime_config/service.py`
- Modify: `backend/app/agent_runtime/{router.py,executor.py}`
- Modify: `backend/app/agent_runtime/{utility.py,kol_detail.py}`
- Modify: `backend/app/main.py`
- Modify: `backend/app/pi_gateway/{scheduler.py,service.py}`
- Modify: `backend/app/admin/{schemas.py,router.py,gateway_service.py}`
- Modify: `backend/app/core/config.py`
- Modify: `.env.example`
- Modify: `src/components/admin/TenantAdmin.tsx`
- Test: `backend/tests/pi_gateway/test_runtime_rollout.py`
- Test: `backend/tests/integration/test_pi_gateway_local_uat.py`
- Test: `src/components/admin/TenantAdmin.test.tsx`

**选择规则**

```python
effective_backend = (
    "current"
    if settings.pi_gateway_kill_switch
    else tenant.runtime_backend
)
```

- `PI_GATEWAY_KILL_SWITCH=true` 是最高优先级，只让新 Run 选择 current；不改历史 Run、不杀在途 Pi。
- Tenant `runtime_backend=current|pi` 变更必须有 active compatible config、Pi feature License、Gateway
  healthy capacity 和 B0 pack compatibility；否则 409。内部租户可先切，普通租户必须由管理端逐个切。
- 不实现“双跑/影子执行”：同一 `AgentMessage.id` 最多一个 user Run，数据库唯一/幂等测试证明。
- 所有生产 `AgentRun` 都按自身 snapshot 路由：user、retry、kol detail 和 utility 子 Run 继承父 Run 的
  backend；Pi internal Run 进入 Pi scheduler，current internal Run 继续走 current Utility/Engine。
  不允许数据库写 `runtime_backend=pi` 却由 FastAPI current 模型适配器实际执行。
- 本地 UAT 使用两个 tenant、每个至少两个 user、fake OpenAI-compatible model、fake DataTap MCP、事务
  测试钱包；真实走 Gateway SDK、HTTP 签名、scheduler、preflight、Builder、Publication、Excel、SSE，
  但不访问外网。
- UAT 场景：跨租户上下文/secret/Artifact 隔离；公平调度；同 Session 互斥；额度不足；License 过期；
  Worker 崩溃一次恢复；二次失败；unknown 不重放；取消；SSE 续传；draining；current→pi→current；
  kill switch；旧 Run snapshot 不变。

- [ ] **Step 1：灰度红灯。** 覆盖所有前置条件、旧/new Run 选择、幂等消息、kill switch 与在途 Run。
- [ ] **Step 2：本地 UAT 红灯。** 先运行完整 fake topology，预期 production Gateway/rollout 尚未接通。
- [ ] **Step 3：最小接线。** `create_agent_runtime()` 继续启动 current executor；Pi scheduler 领取
  `runtime_backend=pi` 的 user/internal Run，current executor/Utility 只执行 current；二者共享 session
  slot、账本和终态事件。为 kol detail/utility 分别覆盖父 backend 继承与执行器归属测试。
- [ ] **Step 4：回滚演练。** Pi Gateway 停止、租户切 current、kill switch 三种方式都只影响新 Run；
  在途 Pi 自然完成或按基础设施恢复规则收口，不能转交 current 重跑同一消息。
- [ ] **Step 5：绿灯。** 本地多租户 UAT、后端/Node/前端聚焦与全量回归；验证 0 外部网络、0 真实
  secret、0 历史 round 修改。
- [ ] **Step 6：输出状态。** 只可记录 `READY_FOR_REAL_B7_UAT`；不得写 Gate A PASS、B7 PASS、
  production ready 或默认已切 Pi。
- [ ] **Step 7：Commit。** `feat: add tenant pi rollout and rollback controls`。

---

## Task 13：运维手册、完整回归、安全审计与实施收口

**Files**

- Create: `docs/runbooks/pi-agent-gateway.md`
- Create: `docs/qa/pi-agent-gateway-local-uat.md`
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `changelog/2026-08-09.md`（若实施跨日，写实际日期文件）
- Modify: this plan（仅勾选完成项与记录实际偏差，不重写历史设计）

**Runbook 必须覆盖**

- FastAPI 与 Gateway 启动/健康检查/依赖版本；master key 生成与轮换；Runtime secret 写入和激活。
- License、tenant backend、capacity、draining、kill switch 的操作顺序与影响范围。
- queue/lease/Attempt/unknown/账务不一致/secret 解密失败/Gateway 离线的诊断与恢复。
- current 回滚流程；明确禁止把同一消息从 Pi 改由 current 重跑。
- 真实 B7 UAT 的单独授权、append-only 证据目录、钱包隔离、停止条件与生产切流审批。
- 当前 Runtime 至少保留一个稳定发布周期；周期结束前不得删除。

- [ ] **Step 1：全量验证。** 在干净环境运行：

```bash
cd backend
.venv/bin/alembic upgrade head
.venv/bin/ruff check app tests
.venv/bin/pytest -q
cd ../pi-runtime
npm test
npm run typecheck
cd ../pi-gateway
npm ci
npm test
npm run typecheck
npm run build
cd ..
npm run test
npm run lint
npm run build
git diff --check
git status --short
```

- [ ] **Step 2：真实 MySQL 局部验证。** 运行 0036→0040 迁移、并发 claim、钱包 reserve/settle/release、
  Event/Attempt sequence、session mutex 和 downgrade guard；不得连接开发库或生产库。
- [ ] **Step 3：本地端到端。** 启动 fake model + fake MCP + FastAPI + production `pi-gateway`，执行
  Task 12 的两租户场景；对生成的正式 Version/Excel/BI 跑 B0 Gate 和 Version 绑定检查。
- [ ] **Step 4：安全扫描。** 扫描 git diff、日志、测试输出、Run snapshot、AgentEvent、AdminAuditLog、
  Gateway diagnostics 和浏览器 fixture，确认无 key/token/Bearer/DSN/明文 endpoint/密文误回显；确认
  Gateway 依赖树无数据库驱动，Worker 无 builtin 工具。
- [ ] **Step 5：独立代码审查。** 按 Critical/Important/Minor 分类；必须修复全部问题并重新运行受影响
  测试，最终 Critical 0 / Important 0 / Minor 0。
- [ ] **Step 6：计划一致性审查。** 对照 B1–B7、12 条全局不变量、所有 API/迁移/前后端契约；扫描
  常见临时实现标记、空函数体和未实现异常，生产新代码命中为 0。
- [ ] **Step 7：文档与 changelog。** 记录实际提交、测试数、迁移 head、依赖版本、local UAT 结果、
  已知限制和 `READY_FOR_REAL_B7_UAT`；明确没有真实外部调用、没有进入方案 C。
- [ ] **Step 8：Commit。** `docs: finalize pi gateway local readiness`。

---

## 3. 实施提交顺序与停止规则

建议严格保持以下提交序列，便于逐项审查和回滚：

1. `feat: add tenant and license control plane`
2. `feat: snapshot encrypted tenant runtime config`
3. `feat: add isolated pi sdk gateway worker`
4. `feat: add authenticated pi gateway control protocol`
5. `feat: schedule pi runs fairly across tenants`
6. `feat: recover pi infrastructure failures once`
7. `feat: bill all agent mcp calls from tenant wallet`
8. `feat: record and reconcile pi runtime usage`
9. `feat: stream pi gateway events through agent sse`
10. `feat: expose audited pi gateway administration api`
11. `feat: add pi gateway administration console`
12. `feat: add tenant pi rollout and rollback controls`
13. `docs: finalize pi gateway local readiness`

每个任务均遵守：确认基线干净 → 写并运行红灯 → 最小实现 → 定向绿灯 → 范围回归 → 安全/隔离审查 →
只暂存本任务文件 → 独立提交。不得把多个任务压成一个提交，也不得为了“全绿”修改弱化测试或 Gate。

以下任一情况必须立即停止并报告，不继续后续任务：

- B0 Gate、现有 Artifact/POC/Agent Runtime 回归失败；
- Pi 0.79.10 锁定 SDK 缺少计划依赖的阻断/abort/in-memory Session 能力；
- 需要升级 Pi/adapter、改变 B0 Artifact 契约或开放额外 builtin/HTTP/shell 权限；
- 无法在外发前完成 durable reservation；
- migration 无法无损归属旧钱包预留；
- 跨租户、密钥泄漏、重复外发、重复终态或双 Runtime 执行；
- 需要真实模型、DataTap、真实钱包、历史 Pi RPC/POC 真实六场景 Task 9、生产切流或方案 C 才能继续。

---

## 4. 计划自审结论

- B1：覆盖 Tenant、membership、License、不可变 config、AES-GCM secrets 和 Run snapshot。
- B2：覆盖独立 Node SDK Gateway、每 Run 隔离 Worker、签名内部协议、受控资源与内部工具。
- B3：覆盖持久队列、公平调度、全局/租户/用户容量、Session mutex、draining、一次 infra Attempt、
  unknown 和取消。
- B4：覆盖统一租户钱包、用户周期额度、MCP 外发前预留、成功结算/失败释放/unknown 保留、模型 token/
  cost 和 reconciliation。
- B5：覆盖 Pi SDK 事件的有界投影、FastAPI 稳定事件、现有 SSE、Last-Event-ID、thinking、终态顺序。
- B6：覆盖租户、用户、License、用量与积分、Pi Runtime、模型/MCP 配置、只读 Run 诊断和管理前端；
  所有写操作审计。
- B7：覆盖 tenant backend、kill switch、current 保留、local multi-tenant UAT 和回滚，但真实 UAT/生产切流
  明确锁定在新授权之后。
- B8：没有实施方案 C；只保留“真实 B7 稳定一个发布周期后提醒用户”的停止点。

计划没有把 B0 synthetic PASS 误写为真实 Gate A PASS，没有修改历史 round，也没有为完成本地开发而
放宽外部调用、钱包、密钥、租户隔离或 Artifact lineage 边界。
