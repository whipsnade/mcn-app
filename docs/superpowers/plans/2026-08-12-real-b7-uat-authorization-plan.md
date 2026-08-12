# REAL B7 UAT 授权计划（方案 B）

> 本计划只定义真实 B7 UAT 的授权边界、执行前提与场景矩阵；不执行任何真实外部调用。
> 证据目录设计、硬停止条件与授权文本模板见配套授权包
> `docs/qa/2026-08-12-pi-b7-uat-authorization-pack.md`。

```text
Status: READY_FOR_REAL_B7_UAT_REAUTHORIZATION（授权模式二选一：模式 A 两阶段 / 模式 B 一次性完整授权，见 §7；
模式 B round REAL_B7_20260812T045636Z_b801c490 已执行并封存：L0 通过、L1 FAIL（mcp_tool_identity_invalid，
0 外发 0 扣费）按规则终止；授权包 §6.6 修复轮已收口。旧授权已消费——真实 B7 必须由用户重新授权，
execution commit 为修复提交后的 clean HEAD）
Real external calls authorized: NO（旧授权已随失败 round 封存；新授权前任何真实外部调用禁止）
Production cutover authorized: NO
Historical Task 9 rerun authorized: NO
Plan C authorized: NO
```

**目标：** 在用户明确授权后，于隔离测试环境对方案 B（FastAPI 控制面 + 生产 Pi Agent Gateway）
执行真实模型 + 真实 DataTap + 真实测试钱包的 B7 UAT，产出 append-only 证据并由独立 reviewer
判定 B7_PASS / B7_FAIL / B7_BLOCKED。

**范围边界：** 本计划不授权以下任何事项——生产切流、把 Pi 设为默认 Runtime、历史 Pi RPC/POC
真实六场景 Task 9（round `20260808T060814Z` 永为 `EVALUATED_FAIL`，不得重跑）、方案 C、
修改代码/迁移/凭证、连接开发/预生产/生产数据库、查询或修改真实钱包与正式客户租户。

**字段状态词汇表（全文只允许这四种取值，不得留空，不得使用 TBD/TODO）：**

- `VERIFIED`：本仓库当前可核对的只读事实（注明核对来源）。
- `NEEDS_USER_INPUT`：需要用户提供的事实性信息（人名、时间窗口等）。
- `NEEDS_USER_APPROVAL`：需要用户明确批准的授权项（环境、预算、凭证、状态变更等）。
- `NOT_APPLICABLE`：本场景该项不适用。

## 0. 已验证仓库事实基线（VERIFIED）

以下事实于 2026-08-12 在工作树 `.worktrees/codex-marketing-capability-pack-b0` 上只读核对：

| 事实 | 值 | 核对来源 |
| --- | --- | --- |
| 分支 | `codex/marketing-capability-pack-b0` | `git status --short --branch` |
| 修复前 production baseline | `61576f7a45c3a93063bdaae5328aefd67933df68`（`codex/marketing-capability-pack-b0` 上最后一个触及代码的提交，失败 round `REAL_B7_20260812T045636Z_b801c490` 的代码即此线；仅为历史事实，不再是 execution gate 参照） | `git log --oneline` |
| 已审核 L1 repair baseline | `68deca58f2d2cd8aafb96d9ea47a0a60462142fa`（`codex/real-b7-l1-repair`；含 `f8a4ffa`/`494d20e`/`c22a3a1`/`8a5f264`/`68deca5` 五个已审核修复提交，独立审查 Critical 0 / Important 0；**新 round 启动门禁的代码基线**） | `git log --oneline`（修复分支） |
| 授权包第一版文档基线 HEAD | `f7ab159aaea379b37e3885381abe79cc3454bb41`（2026-08-12 第一版文档轮撰写时点的 `git rev-parse HEAD`，docs-only；仅为历史事实陈述，不是执行身份，也不是 execution commit 候选） | `git rev-parse HEAD`（撰写时点） |
| execution commit（规则，不预写 SHA） | 等于本门禁纠偏提交之后、执行启动门禁通过时点的 `git rev-parse HEAD`（工作树必须干净）；必须是已审核 L1 repair baseline `68deca58…` 的线性后代且包含全部五个修复提交；`68deca58…` 至 HEAD 区间只允许本次门禁纠偏的 Markdown/changelog 变更；模式 A 由用户在阶段 A 授权文本中逐字确认，模式 B 由授权消息以身份规则绑定、执行时点现场核验；本文档不内嵌任何自指 SHA | 规则见 §1.1/§1.2 |
| 工作树状态 | 干净（无未提交变更） | `git status --short` |
| `git diff --check` | 通过（无输出） | `git diff --check` |
| 迁移 head | `0043_billing_downgrade_guard` | `backend/migrations/versions/` 目录序 |
| Pi SDK 锁定版本 | `@earendil-works/pi-coding-agent 0.79.10`、`@earendil-works/pi-ai 0.74.2`、`@earendil-works/pi-tui 0.74.2` | `pi-gateway/package.json`（精确版本，无 `^`/`~`） |
| MCP adapter 版本 | `pi-mcp-adapter 2.20.1` | `pi-gateway/package.json` |
| 固定 MCP 积分 | 10（`mcp_call_points`，启动硬校验 `MCP_CALL_POINTS must be 10`） | `backend/app/core/config.py:69,144-145` |
| DataTap 默认 origin | `https://datatap.deepminer.com.cn`（仅测试/离线拓扑可覆盖为 loopback） | `backend/app/core/config.py:60-61` |
| DataTap 服务枚举 | `insight-cube-mcp` / `social-grow-mcp` / `social-grow-content-mcp` / `aktools-mcp` / `bilibili-mcp` | `backend/app/mcp_gateway/contracts.py` |
| 已审核动态工具 | 29 个（4 个服务，明细见 §4） | `backend/app/mcp_gateway/registry.py` `DYNAMIC_TOOL_ALLOWLIST` |
| Pi 内部工具 | 13 个（明细见 §4） | `backend/app/pi_gateway/service.py` claim 响应 |
| adapter 服务别名 | `insight-cube` / `social-grow` / `social-grow-content` / `aktools`（`bilibili-mcp` 经 `aktools` 别名接入） | `pi-gateway/src/protocol.ts` `PI_ADAPTER_SERVICE_ALIASES` |
| 代码架构复核结论 | Critical 0 / Important 0 / Minor 1（2026-08-12，针对方案 B 代码） | 用户任务书事实陈述 |
| 授权包复核结论 | Critical 0 / Important 3 / Minor 1（2026-08-12，针对授权包第一版 `f7ab159`）；修复轮已全部关闭，逐项证据见授权包 §6 | 用户任务书事实陈述 + 授权包 §6 |
| 离线进程级 UAT | 17 场景 × 串行 3 轮全绿（fake model + fake DataTap MCP，0 外部网络） | `changelog/2026-08-11.md`、`docs/qa/pi-agent-gateway-local-uat.md` |
| 当前交付状态 | 代码：`READY_FOR_REAL_B7_UAT_REVIEW`；授权包：`AWAITING_USER_AUTHORIZATION`（两阶段均未授权；修复前曾为 `NEEDS_AUTHORIZATION_PACK_REPAIR`，见授权包 §6） | `docs/qa/pi-agent-gateway-local-uat.md`、`docs/runbooks/pi-agent-gateway.md`、授权包 §6 |

**事实判读：** 本地离线 fake topology 通过 ≠ 真实 B7 通过；`READY_FOR_REAL_B7_UAT_REVIEW`
≠ `READY` 被确认，更不等于 B7 PASS 或 production ready。真实 B7 UAT 必须取得用户明确授权
（§7 定义两种合法模式：模式 A 两阶段授权——阶段 A 只放行 L0 零外发预检、阶段 B 才放行真实
模型/DataTap/钱包调用；模式 B 一次性完整授权 L0→L1→L2）。2026-08-12 的模式 B round
`REAL_B7_20260812T045636Z_b801c490` 已执行：L0 通过、L1 FAIL（`mcp_tool_identity_invalid`，
0 外发 0 扣费），按规则终止并封存；该次授权已消费完毕。修复轮（授权包 §6.6）后的任何真实
B7 执行都必须取得用户**新的**授权，且 L0 失败仍必须停止、L1 失败不得进入 L2。

## 1. Round 身份

### 1.1 格式

```text
REAL_B7_<YYYYMMDDTHHMMSSZ>_<SHORT_COMMIT>
```

- `<YYYYMMDDTHHMMSSZ>`：operator 提出 round_id 时刻的 UTC 秒级时间戳。提出行为仅为本地
  字符串构造：不得创建任何目录、不得连接任何环境（数据库/模型/DataTap/钱包）。
- `<SHORT_COMMIT>`：execution commit SHA 的前 8 位小写十六进制（与 `git rev-parse
  --short=8` 一致）；模式 A 取用户最终批准值，模式 B 取启动门禁通过时点的 clean HEAD。
- execution commit = 本门禁纠偏提交之后、执行启动门禁通过时点 `git rev-parse HEAD` 的值
  （工作树必须干净），且必须是已审核 L1 repair baseline `68deca58…` 的线性后代、包含
  `f8a4ffa`/`494d20e`/`c22a3a1`/`8a5f264`/`68deca5` 五个修复提交；`68deca58…` 至 HEAD
  区间只允许已审核的文档纠偏。本文档与授权模板不预写该值——任何 commit 不得在自身内部
  硬编码自己的 SHA；`61576f7…`（修复前 production baseline）与 `f7ab159…`（第一版文档
  基线）均为历史事实，不构成执行身份；不再要求 `61576f7…` 至 HEAD 为 docs-only。
- round_id 必须由 operator 在授权确认**之前**以完整确定值或完整身份规则提出（模式 A：写入
  阶段 A 授权模板由用户逐字确认；模式 B：以「执行时点 clean HEAD 前 8 位」规则写入一次性
  授权模板由用户确认，执行时展开为确定值）；不存在任何"授权后由 operator 补填"的通道。
- L0-01 现场复核 `git rev-parse HEAD` 与已确认 execution commit（模式 B 为身份规则展开值）
  不一致，或工作树不干净，即整个授权作废：operator 必须以新时间戳与新 short commit 重新
  提出 round_id 并按所选模式重新取得授权。
- round_id 一经写入证据目录 `authorization-phase-a.md`（模式 A）或 `authorization.md`
  （模式 B）即不可变；任何身份字段变化必须终止当前 round 并重新取得授权、新建 round。
- **历史 round 记录**：2026-08-12 模式 B round `REAL_B7_20260812T045636Z_b801c490` 已执行
  并封存（L0 通过、L1 FAIL 按规则终止，授权包 §6.6）；其证据目录只读封存，禁止补写、修改
  或覆盖。任何新的真实 B7 执行必须取得用户新授权并生成新 round_id；在新授权前，任何形如
  `REAL_B7_*` 的目录均不得创建。

### 1.2 未来执行时必须固定的字段

| 字段 | 当前值/来源 | 状态 |
| --- | --- | --- |
| 授权模式 | 模式 A（两阶段）/ 模式 B（一次性完整授权）二选一；2026-08-12 的模式 B 授权已随失败 round `REAL_B7_20260812T045636Z_b801c490` 消费完毕，新执行须重新授权 | NEEDS_USER_APPROVAL |
| round_id | operator 在授权前提出的完整确定值或身份规则（§1.1）；模式 A：阶段 A 模板逐字确认、阶段 B 绑定同一值；模式 B：一次性模板确认身份规则，启动门禁通过时点展开为确定值 | NEEDS_USER_APPROVAL |
| commit_sha | 本门禁纠偏提交之后、启动门禁通过时点 `git rev-parse HEAD`（工作树干净；`68deca58…` 已审核 L1 repair baseline 的线性后代且含全部五个修复提交；区间仅已审核文档纠偏）；`61576f7…` 仅为修复前 production baseline，`f7ab159…` 仅为第一版文档基线，均非执行身份 | NEEDS_USER_APPROVAL |
| branch | `codex/marketing-capability-pack-b0`（L1 修复在 `codex/real-b7-l1-repair`；新 round 的 execution commit 须落在包含该修复的分支/合并线上） | VERIFIED |
| migration_head | `0043_billing_downgrade_guard` | VERIFIED |
| Pi SDK 版本 | `pi-coding-agent 0.79.10` / `pi-ai 0.74.2` / `pi-tui 0.74.2` | VERIFIED |
| adapter 版本 | `pi-mcp-adapter 2.20.1` | VERIFIED |
| Gateway build hash | L0-04 从 execution commit 全新 `npm ci && npm run build` 后记录 dist 摘要；模式 A 由阶段 B 模板固定，模式 B 逐字录入证据后视为固定值（变化命中授权包 §3-18） | NEEDS_USER_APPROVAL |
| Runtime Config ID/version | 专用库内 append-only 独立版本（L0 初始化创建，§2.0/§6.1）；模式 A 由阶段 B 模板固定，模式 B 录入证据后视为固定值 | NEEDS_USER_APPROVAL |
| License ID/version | 专用库内独立 License（L0 初始化创建，§2.0/§6.1）；固定方式同上 | NEEDS_USER_APPROVAL |
| tenant IDs | 两个独立 synthetic B7 租户（slug 含 round_id 与 tenant-a/tenant-b，L0 初始化创建）；固定方式同上 | NEEDS_USER_APPROVAL |
| test user IDs | 每租户至少两个 synthetic 测试用户（L0 初始化创建）；固定方式同上 | NEEDS_USER_APPROVAL |
| gateway IDs | 测试 Gateway 固定 id 白名单值，L0 核对后固定（模式同上） | NEEDS_USER_APPROVAL |
| evidence directory | `docs/qa/evidence/pi-b7/<round_id>/`（结构见授权包 §2） | NEEDS_USER_APPROVAL |
| operator | 执行人，授权模板填写（模式 A 阶段 A / 模式 B 一次性模板） | NEEDS_USER_INPUT |
| independent reviewer | 独立复核人（不得与 operator 同一人），授权模板填写（模式同上） | NEEDS_USER_INPUT |
| start/end time | 授权时间窗口，授权模板填写（模式 A：阶段 A 填写、阶段 B 可复述或延长且不早于阶段 A 窗口；模式 B：一次性模板填写） | NEEDS_USER_APPROVAL |
| authorization message reference | 模式 A：两条独立用户新消息，记录于 `authorization-phase-{a,b}.md`；模式 B：单条一次性授权消息，记录于 `authorization.md`。用户在模板填 `THIS_MESSAGE`，operator 只记录平台消息 ID/任务 ID 与授权文本 SHA-256，不得修改用户原文 | NEEDS_USER_APPROVAL |

## 2. 隔离环境要求（必须由用户逐项确认）

§2.3 列出的 12 项均为 `NEEDS_USER_APPROVAL`；任一项不能确认即不得进入 Level 1/2。其中第
1、2 项（独立环境与独立数据库）已由 §2.0 的专用环境满足并经 2026-08-12 核验；第 3–6 项由
L0 环境初始化（§6.1）在专用库内创建 synthetic 数据满足；其余为执行期要求。

### 2.0 本次授权绑定的专用隔离环境（2026-08-12 已创建并核验，VERIFIED）

数据库身份（启动门禁逐项核验，任一不符即 B7_BLOCKED）：

| 项 | 值 | 核验方式 |
| --- | --- | --- |
| host / port | `127.0.0.1` / `3306` | 连接参数 |
| database | `kol_insight_b7_uat`（唯一允许数据库） | `SELECT DATABASE()` |
| user identity | `kol_b7_uat@localhost` | `CURRENT_USER()` |
| 运行模式 | `APP_ENV=test` / `AUTH_MODE=mock` | 进程环境 |
| migration head | `0043_billing_downgrade_guard`（唯一 head；迁移已完成，只核验不重建） | `alembic heads` |
| charset / collation | `utf8mb4` / `utf8mb4_unicode_ci` | 库级变量 |
| 表数量 | 73 | information_schema 计数 |
| 隔离证明 | 专用账号访问 `kol_insight.users` 被 MySQL 1142 拒绝 | 实测拒绝 |
| 第一轮执行前的历史初始状态 | `tenants`=0、`encrypted_runtime_secrets`=0、`runtime_config_versions` 含 2 条系统迁移种子配置（保留）。**仅历史事实（2026-08-12 第一轮 L0 前核验）；不得作为新 round 门禁** | SELECT 计数（当时） |
| 当前状态（失败 round 后） | 专用库包含 round `REAL_B7_20260812T045636Z_b801c490` 的 retained_by_policy 数据：tenant/用户/钱包/账本/License/Runtime Config/encrypted_runtime_secrets/usage/lineage 等全部保留 | 历史 round 证据 |

凭证引用（只记录引用，任何值不得写入文档/证据/Git）：

| 凭证 | secret store reference（VERIFIED） |
| --- | --- |
| MySQL 密码 | macOS Keychain `service=com.kol-insight.real-b7-uat.mysql` / `account=kol_b7_uat@127.0.0.1`（直接捕获到进程内 `MYSQL_PASSWORD`，禁止 echo/日志/写文件） |
| DataTap Token | macOS Keychain `service=com.kol-insight.real-b7-uat.datatap` / `account=DATATAP_MCP_TOKEN` |
| Runtime master keys | macOS Keychain `service=com.kol-insight.real-b7-uat.runtime-secret-master-keys` / `account=v1`；已核验格式为 `v1` + 32 bytes；`RUNTIME_SECRET_ACTIVE_KEY_VERSION=v1` |
| 模型配置 | 主仓库未跟踪文件 `backend/.env`（只读 `TENCENT_PLAN_BASE_URL`/`TENCENT_PLAN_MODEL`/`TENCENT_PLAN_API_KEY`，仅进程内；禁止写入 worktree 或证据） |

已核验事实（2026-08-12，VERIFIED；不记录任何明文或完整指纹）：

- DataTap Keychain 值与主仓库 `backend/.env` 当前 `DATATAP_MCP_TOKEN` 指纹一致。
- L0 环境初始化只经生产 domain/admin service：两个 synthetic B7 tenant（slug 含 round_id 与
  `tenant-a`/`tenant-b`，`status=active`，不用真实客户名称）、每 tenant ≥2 synthetic 用户、
  TenantWallet（各 2000 积分、初始 reserved=0）、用户周期额度 2000、License（含
  `kol_selection`/`brand_analysis`/`campaign_analysis`/`kol_detail`/`utility`，支持后续
  suspend/restore）、租户级 Pi Runtime Config（`runtime_backend=pi`、
  `runtime_contract_version=marketing_runtime_v1`，draft→单独激活，append-only）。
  全部创建幂等并写审计/账本；**禁止直接 INSERT/UPDATE `encrypted_runtime_secrets`**。
- 数据库证据 `retained_by_policy`：tenant/用户/账本/Runtime Config/License/usage/lineage
  保留至独立 reviewer 封口；清除需用户另行授权；证据目录永久保留。

使用约束（违反即 B7_BLOCKED）：

- 严禁连接 `kol_insight`、`kol_insight_test` 或任何开发/预生产/生产/正式客户数据库。
- 禁止 DROP/CREATE/重建 `kol_insight_b7_uat`；禁止对其运行普通 pytest、离线 UAT harness
  或迁移 downgrade。
- 禁止使用 shell xtrace；禁止把任何密码/Token/API Key/DSN/Cookie/Authorization header/
  HMAC 值/解密结果写入命令输出、日志、Git、Markdown、SSE 或 Artifact。

新 round 的数据库状态门禁（重授权 round 适用；任一不符即 B7_BLOCKED）：

1. 专用库允许包含 retained_by_policy 历史数据（失败 round 的全套证据行）；「库为空」
   不再是合法前提。
2. 启动时只读记录 before snapshot 与各表现有行数（不写任何行）。
3. 新 round_id 对应的 tenant/user/gateway/Run 在启动时必须为 0（按 slug/idempotency key/
   gateway_id 中的新 round_id 前缀核验）。
4. 禁止复用或修改旧 round 的 tenant、用户、钱包、账本、License、Runtime Config、
   encrypted secret、usage、lineage；历史行只读。
5. L0 只通过生产 domain/admin service 创建 slug 与 Idempotency-Key 含新 round_id 的全新
   tenant-a/tenant-b 及配套数据（用户/钱包/额度/License/Runtime Config/gateway 身份）。
6. 创建后核对新增行 delta：所有新增行只能属于新 round 身份集合。
7. 新 round 的预算、账务、外发与隔离统计只计算新 round 身份集合；历史 retained rows
   不得纳入（钱包恒等式、usage 对账、MCP 计数均以新 round 行过滤）。
8. 即使清理授权为 YES，也只允许处理本 round 进程级临时状态与明确授权的本 round 数据；
   旧失败 round 数据一律不得清理。

### 2.1 固定 quarantine 基线（known_quarantined，不可调用）

2026-08-12 首次真实 discovery（失败 round `REAL_B7_20260812T045636Z_b801c490`）核验并固定：

| service | remote_name | discovery_digest | review_status |
| --- | --- | --- | --- |
| `insight-cube-mcp` | `query_user_info` | `aa4933db9542bc5802a6a1a31b6dd274ea08e562ca01cf1ef1f1fd2a56afeb49` | `quarantined` |

- 该工具为 DataTap 网关存在但未审核的历史工具：**不登记** `DYNAMIC_TOOL_ALLOWLIST`、
  **不进入** claim `adapter_catalog`、preflight 不可达。
- 新的授权消息必须显式确认本基线（精确 service/remote_name/digest）。
- round 期间该基线的 digest、行数（恰 1 条）或 review_status 发生任何变化，立即硬停止
  （授权包 §3-3）；不得在 round 中审批或豁免。
- 已审核 29 个工具的 digest 以 L1-00 写入的 `catalog-digests.json` 为准。

### 2.2 真实 DataTap discovery 的时机（L0 零外发边界）

- L0 全程零真实模型/DataTap 请求：L0 阶段启动控制面时，`DATATAP_MCP_ORIGIN` 必须指向
  loopback 占位（discovery 本地失败、注册中心继续启动、catalog 不变更、0 外发、0 ToolCall、
  0 积分）。
- 真实 DataTap discovery 只允许发生在 **L1-00 外部调用预检**（§6.2）：以真实 origin 重启
  控制面，让 lifespan 完成真实 discovery（negotiation/list-tools），然后核验 29 个已审核
  工具 digest 全一致 + §2.1 quarantine 基线不变，写入 `catalog-digests.json`。discovery 是
  协商动作：0 ToolCall、0 积分。

### 2.3 隔离环境要求（必须由用户逐项确认）
1. 只能使用独立 B7 测试环境，与开发、预生产、生产环境物理或账户级隔离。
2. 独立测试数据库（独立实例或独立 schema），不连接开发/预生产/生产数据库。
3. 独立测试租户，至少两个，用于跨租户隔离验证；不复用任何正式客户租户。
4. 独立测试用户（每租户至少两个），不复用真实用户账号。
5. 独立测试钱包（TenantWallet）与用户周期额度，仅存在于测试环境。
6. 不共享生产钱包、不共享正式客户租户、不共享生产 License。
7. 测试 Gateway 与生产 Gateway 隔离：独立 `PI_GATEWAY_ID`、独立 HMAC secret、独立进程与端口。
8. 测试 Runtime Config 为 append-only 独立版本（scope=tenant，仅测试租户），不回写任何现有配置。
9. 测试 License 可独立暂停/恢复，暂停动作只影响测试租户。
10. kill switch（`PI_GATEWAY_KILL_SWITCH`）已在测试环境验证可用，且验证过程本身计入场景。
11. current 回滚路径已准备：测试租户可随时切回 `runtime_backend=current`，且只影响新 Run。
12. 测试结束后的保留/清理策略由用户批准：证据目录 append-only 永久保留；测试租户/用户/
    钱包/账本数据的保留期与清理范围须明示。

授权包与证据中不得包含明文 DSN 或任何凭证值；数据库连接只以 secret store 引用出现。

## 3. 凭证引用清单（只记录引用，不记录值）

任何情况下不得把凭证值写入 Markdown、Git、日志或命令行输出。「masked fingerprint」只允许
`••••` + 末 4 位或等效掩码形式。9 项凭证的 secret store reference 列已按 §2.0 专用环境
核验（VERIFIED，只记引用不记值）；其余 6 列元数据（masked fingerprint / key version /
owner / rotation/expiry / execution host / status）在 L0 只读核对后逐项固定——模式 A 填入
阶段 B 授权模板，模式 B 逐字录入证据后视为固定值（未授权变化命中授权包 §3-18）。

| 凭证变量 | secret store reference | masked fingerprint | key version | owner | rotation/expiry | execution host | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `TENCENT_PLAN_BASE_URL` | VERIFIED：主仓库未跟踪文件 `backend/.env`（仅进程内读取） | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `TENCENT_PLAN_MODEL` | VERIFIED：主仓库未跟踪文件 `backend/.env`（仅进程内读取） | NOT_APPLICABLE（非密钥，模型名） | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `TENCENT_PLAN_API_KEY` | VERIFIED：主仓库未跟踪文件 `backend/.env`（仅进程内读取） | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `DATATAP_MCP_ORIGIN` | VERIFIED：`https://datatap.deepminer.com.cn`（代码默认的已审核真实 origin；以授权 Runtime Config 的 `datatap_urls` 为准） | NOT_APPLICABLE（非密钥，origin） | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `DATATAP_MCP_TOKEN` | VERIFIED：Keychain `com.kol-insight.real-b7-uat.datatap` / account `DATATAP_MCP_TOKEN` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `PI_GATEWAY_INTERNAL_SECRET` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `RUNTIME_SECRET_MASTER_KEYS` | VERIFIED：Keychain `com.kol-insight.real-b7-uat.runtime-secret-master-keys` / account `v1` | NEEDS_USER_APPROVAL | VERIFIED：`v1`（32 bytes，active version `v1`） | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `JWT_SECRET` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| MySQL credential reference | VERIFIED：Keychain `com.kol-insight.real-b7-uat.mysql` / account `kol_b7_uat@127.0.0.1` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |

约束：

- 生产路径上模型/DataTap 凭证只经 Runtime Config 加密 secret（AES-256-GCM，AAD 绑定
  tenant/secret/kind/key_version）下发到 Worker 内存；进程环境变量是测试环境唯一允许的替代
  注入方式，且其值不得出现在任何证据文件。
- 凭证轮换/吊销本身不改变已授权 round；round 进行中凭证到期属于硬停止条件
  （外部错误无法稳定分类）。

## 4. 工具与网络 allowlist

### 4.1 允许的 DataTap 服务与工具（VERIFIED，来自 `DYNAMIC_TOOL_ALLOWLIST`）

- 每次 MCP 外发固定积分：**10**（VERIFIED，启动硬校验）。
- 是否产生外部请求：下表 29 个工具全部产生对 DataTap 网关的外部请求。
- 预期 Evidence 类型：MCP `tool_call` Evidence（DataTap payload 经归一化落库，lineage 层
  `source_type=evidence`）；connect/search/list 等协商动作不创建 ToolCall、不扣分。
- schema digest：实时发现时计算并落 `mcp_tool_catalog.discovery_digest`；本环境的真实 digest
  只能在 Level 0 从测试环境目录读取记录，当前全部 `NEEDS_USER_APPROVAL`。digest 漂移即隔离
  （quarantine）并触发硬停止，不得自动批准新工具。
- remote tool name：实时 DataTap 网关以审核内部名暴露工具，运行时 remote name 取内部名；
  allowlist 中旧式 `datatap.*.v1` 名为历史遗留映射，不参与运行时寻址（VERIFIED，见
  `registry.py` `ToolRegistryService.__init__` 注释）。

| 服务（service slug） | internal tool name（= 运行时 remote name） | 历史映射名 | 输出 Schema | 外发 | 积分 | schema digest |
| --- | --- | --- | --- | --- | --- | --- |
| insight-cube-mcp | match_best_tag | datatap.insight.match.best.tag.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | query_analysis_data | datatap.insight.query.analysis.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | social_statistic_trend | datatap.insight.social.statistic.trend.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | social_statistic_user_profile | datatap.insight.social.statistic.user.profile.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | social_statistic_hot_user | datatap.insight.social.statistic.hot.user.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | social_statistic_overview | datatap.insight.social.statistic.overview.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | social_statistic_hot_topic | datatap.insight.social.statistic.hot.topic.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | social_statistic_category_rank | datatap.insight.social.statistic.category.rank.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | query_raw_posts | datatap.insight.query.raw.posts.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | social_statistic_brand_activity | datatap.insight.social.statistic.brand.activity.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | query_rank_list | datatap.insight.query.rank.list.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| insight-cube-mcp | analysis_target_search | datatap.insight.analysis.target.search.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-mcp | kol_match_mentions_tag | datatap.social.grow.kol.match.mentions.tag.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-mcp | kol_detail | datatap.social.grow.kol.detail.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-mcp | kol_get_class_tag_dictionary | datatap.social.grow.kol.class.tag.dictionary.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-mcp | kol_xiaohongshu_search | datatap.xiaohongshu.kol.search.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-mcp | kol_douyin_search | datatap.douyin.kol.search.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-mcp | kol_bilibili_search | datatap.social.grow.kol.bilibili.search.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-mcp | kol_weibo_search | datatap.social.grow.kol.weibo.search.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-mcp | kol_wechat_search | datatap.social.grow.kol.wechat.search.v1 | datatap_result_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| bilibili-mcp | general_search | datatap.bilibili.general.search.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| bilibili-mcp | search_user | datatap.bilibili.search.user.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| bilibili-mcp | get_video_danmaku | datatap.bilibili.video.danmaku.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| bilibili-mcp | get_precise_results | datatap.bilibili.precise.results.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-content-mcp | hotwords_xiaohongshu_dictionary | datatap.content.hotwords.xiaohongshu.dictionary.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-content-mcp | hotwords_xiaohongshu_list | datatap.content.hotwords.xiaohongshu.list.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-content-mcp | hotwords_xiaohongshu_posts | datatap.content.hotwords.xiaohongshu.posts.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-content-mcp | topic_xiaohongshu_list | datatap.content.topic.xiaohongshu.list.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |
| social-grow-content-mcp | topic_xiaohongshu_posts | datatap.content.topic.xiaohongshu.posts.v1 | permissive_v1 | 是 | 10 | NEEDS_USER_APPROVAL |

注：`aktools-mcp` 枚举存在但当前 allowlist 无任何已审核工具；adapter 侧 `aktools` 别名当前承载
`bilibili-mcp` 的 4 个工具（VERIFIED，见 `protocol.ts` 别名表与 `resource-loader.ts`
`ALLOWED_SERVICES`）。目录行数上限 32（claim 查询 `limit(32)`）。

### 4.2 Pi 内部工具（VERIFIED，claim 响应固定 13 个）

以下工具经父子 IPC 桥由 FastAPI 执行，**不产生任何外部网络请求**（只访问控制面数据库与
B0 能力包），不计 MCP 积分：

`get_session_context`、`load_marketing_skill`、`search_evidence`、`read_tool_result`、
`read_artifact`、`build_brand_report_draft`、`build_campaign_report_draft`、
`build_kol_selection_draft`、`build_kol_analysis_draft`、`build_kol_detail_draft`、
`build_insight_draft`、`publish_artifacts`、`request_clarification`。

### 4.3 网络 allowlist

| 方向 | 主机 | 状态 |
| --- | --- | --- |
| 允许 | 模型端点 origin（`TENCENT_PLAN_BASE_URL` 的授权取值，阶段 B 模板固定） | NEEDS_USER_APPROVAL |
| 允许 | DataTap MCP origin（默认 `https://datatap.deepminer.com.cn`，VERIFIED 为代码默认值；测试环境实际取值以授权 Runtime Config 的 `datatap_urls` 为准，阶段 B 模板固定） | NEEDS_USER_APPROVAL |
| 允许 | FastAPI 控制面 origin（`PI_GATEWAY_CONTROL_PLANE_URL`；production 必须 HTTPS，仅 development/test 允许 loopback HTTP；阶段 B 模板固定） | NEEDS_USER_APPROVAL |
| 禁止 | 上述之外的一切主机：未登记供应商、对象存储、遥测/分析端点、模型自行决定的任意 URL、Pi 内建工具可达的本地资源与 shell | VERIFIED（禁止规则本身为代码强制：Worker `noTools:"builtin"`、adapter `hostConfigDiscovery:"off"`、ResourceLoader 全量关闭自动发现） |

digest 漂移处置：catalog/schema digest 与授权记录不一致时，registry 自动隔离该工具；
执行现场必须立即停止整个 round，不得在 round 中审批或豁免新工具。

## 5. 预算授权表

以下金额/上限全部由用户在授权模板中填写并批准（模式 A：阶段 B 模板；模式 B：一次性模板）；
本计划不预填任何数值。固定项已标注 VERIFIED。13 个待批数值：最大 round 数、最大 Run 数、
每场景最大 Run 数、最大 MCP 外发次数、单 Run 最大 MCP 次数、测试钱包初始余额、最大允许积分
净支出、用户周期额度、最大模型请求数、最大输入 token、最大输出 token、最大模型费用、最大执行时长。

| 字段 | 值 | 状态 |
| --- | --- | --- |
| 最大 round 数 | 由用户批准 | NEEDS_USER_APPROVAL |
| 最大 Run 数（全 round 合计） | 由用户批准 | NEEDS_USER_APPROVAL |
| 每场景最大 Run 数 | 由用户批准 | NEEDS_USER_APPROVAL |
| 最大 MCP 外发次数（全 round 合计） | 由用户批准 | NEEDS_USER_APPROVAL |
| 单 Run 最大 MCP 次数 | 由用户批准 | NEEDS_USER_APPROVAL |
| 固定 MCP 积分 | 10 | VERIFIED |
| 测试钱包初始余额 | 由用户批准 | NEEDS_USER_APPROVAL |
| 最大允许积分净支出 | 由用户批准 | NEEDS_USER_APPROVAL |
| 用户周期额度（points_limit/period） | 由用户批准 | NEEDS_USER_APPROVAL |
| 最大模型请求数 | 由用户批准 | NEEDS_USER_APPROVAL |
| 最大输入 token | 由用户批准 | NEEDS_USER_APPROVAL |
| 最大输出 token | 由用户批准 | NEEDS_USER_APPROVAL |
| 最大模型费用 | 由用户批准 | NEEDS_USER_APPROVAL |
| 最大执行时长 | 由用户批准 | NEEDS_USER_APPROVAL |
| 达到预算后的行为 | 立即停止整个 round（不自动重试、不进入后续场景） | VERIFIED（本计划强制） |

本次执行（模式 B）用户已批准的 13 项数值以其 2026-08-12 一次性授权消息为唯一权威来源，
round 开启时逐字录入 `authorization.md` 与 manifest 帧；本表不复制具体数值。

## 6. 场景矩阵

授权层级：`L0`（零外部调用预检）→ `L1`（最小真实冒烟）→ `L2`（完整真实 B7）。授权模式见
§7：模式 A 下 L0 由阶段 A `L0_PRECHECK_AUTHORIZATION` 授权、L1/L2 由阶段 B
`REAL_B7_CALL_AUTHORIZATION` 授权（阶段 A 之后的另一条独立新消息）；模式 B 下 L0→L1→L2
由单条一次性授权消息（`ONESHOT_FULL_AUTHORIZATION`）整体授权。无论哪种模式，在授权消息被
完整确认之前，任何真实模型/DataTap/钱包操作均禁止。标记 `+D(...)` 的场景包含 destructive
test-state change，必须由用户在授权文本中逐项显式开启，不能由通用授权隐含允许。

所有场景的预算上限均 `NEEDS_USER_APPROVAL`，且不得超出 §5 全局预算；停止条件均为
「命中授权包 §3 任一硬停止条件即终止整个 round」，下文不再逐场景重复全局条件，只列
场景特有停止条件。

### 6.1 Level 0：零外部调用预检（初始化 + 12 项预检）

L0 不调用真实模型/DataTap，不发生任何 Run 级计费（reserve/settle/release），也不发生真实
DataTap discovery：L0 阶段启动控制面时 `DATATAP_MCP_ORIGIN` 必须指向 loopback 占位
（discovery 本地失败、注册中心容错继续启动、catalog 不变更、0 外发、0 ToolCall、0 积分）；
真实 discovery 只允许发生在 L1-00（§6.2，§2.2）。专用库含 retained_by_policy 历史数据
（§2.0 新 round 数据库状态门禁），L0 授权包含一次性的
环境初始化例外：只经生产 domain/admin service 幂等创建 slug 与 Idempotency-Key 含新
round_id 的全新
synthetic 测试数据（两个 tenant、每 tenant ≥2 用户、TenantWallet 各 2000 积分且 reserved=0、
用户周期额度 2000、含 `kol_selection`/`brand_analysis`/`campaign_analysis`/`kol_detail`/
`utility` 的 License、租户级 Pi Runtime Config draft→单独激活），全部写审计/账本，禁止直接
INSERT/UPDATE `encrypted_runtime_secrets`，禁止复用或修改历史 retained 行；创建后按新增行
delta 对账（只允许属于新 round 身份集合）。除该初始化外，L0 只允许本地构建、对专用库只读
访问（Runtime Config、License、TenantWallet 与 quota 元数据，SELECT only）、创建证据目录并
写入首帧。任一失败即停止，不进入 L1；模式 A 下也不得提出阶段 B。

| ID | 预检项 | 通过判据 | 状态 |
| --- | --- | --- | --- |
| L0-00 | 专用库环境初始化（幂等，仅 domain/admin service） | 两 tenant 各有独立 active Runtime Config；`encrypted_runtime_secrets` 存在 `datatap_token`/`model_api_key`/`model_base_url`/`datatap_url:*` 密文行（只记 masked/fingerprint/key_version）；RuntimeConfigService 内存内解密一致性核验（只写 true/false）；DataTap token 指纹与 Keychain 一致；新 Run 绑定 active config snapshot | NEEDS_USER_APPROVAL |
| L0-01 | Git commit/branch 核对 | `git rev-parse HEAD` 与阶段 A 授权文本逐字一致、工作树干净；不一致即授权作废 | NEEDS_USER_APPROVAL |
| L0-02 | migration head 核对 | `alembic heads` 唯一且为 `0043_billing_downgrade_guard` | NEEDS_USER_APPROVAL |
| L0-03 | 依赖版本核对 | `npm ls --depth=0` 与锁定版本一致；后端依赖与授权记录一致 | NEEDS_USER_APPROVAL |
| L0-04 | Gateway build 核对 | 从 execution commit 全新构建，dist 摘要落 evidence（`dependency-versions.json`） | NEEDS_USER_APPROVAL |
| L0-05 | Runtime Config/License 形状核对 | append-only、scope/status/版本号符合 §2；只读查询 | NEEDS_USER_APPROVAL |
| L0-06 | catalog 静态核对 | 代码 `DYNAMIC_TOOL_ALLOWLIST` 29 个工具登记完整；真实 digest 核对在 L1-00 进行（L0 不做真实 discovery，§2.2） | NEEDS_USER_APPROVAL |
| L0-07 | 租户/用户隔离配置核对 | ≥2 测试租户、每租户 ≥2 测试用户、membership 形状正确；只读 | NEEDS_USER_APPROVAL |
| L0-08 | 钱包/额度配置形状核对 | 测试钱包初始余额、预留=0、用户周期额度形状；只读 | NEEDS_USER_APPROVAL |
| L0-09 | kill switch/current 回滚准备核对 | 配置可置位、回滚路径演练脚本就绪（不执行真实切换） | NEEDS_USER_APPROVAL |
| L0-10 | append-only evidence 目录可写核对 | 创建 `<round_id>/` 并写入 `authorization-phase-a.md` 与 `manifest.jsonl` 首帧（round_opened），flush/fsync 成功 | NEEDS_USER_APPROVAL |
| L0-11 | 日志脱敏检查 | 抽查启动日志无 token/key/DSN/手机号明文 | NEEDS_USER_APPROVAL |
| L0-12 | 网络出口核对 | 进程外发目标仅 §4.3 允许主机（静态配置核对，不发真实请求） | NEEDS_USER_APPROVAL |

### 6.2 Level 1：最小真实冒烟（L1-00 外部调用预检 + 1 个场景，需独立批准）

**L1-00 外部调用预检（0 ToolCall、0 积分）**：以真实 `DATATAP_MCP_ORIGIN` 重启控制面，完成
真实 DataTap discovery（仅 negotiation/list-tools 协商动作）；核验 29 个已审核工具 digest
与 discovery 全部一致、§2.1 quarantine 基线不变（恰 1 条、digest 相同、仍 quarantined），
写入 `catalog-digests.json` 快照。任一不符即停止，不进入 L1-SMOKE。模型请求 0、MCP 工具
调用 0、扣费 0。

| 字段 | 内容 |
| --- | --- |
| 场景 ID | L1-SMOKE |
| 形状 | 单租户、单用户、单 Run、单个已审核只读 MCP 工具、一次外发、固定最多 10 积分 |
| 候选工具 | `match_best_tag`（insight-cube-mcp）；最终工具选择由阶段 B 模板固定，NEEDS_USER_APPROVAL |
| 真实模型 / 真实 DataTap | 是 / 是（DataTap 一次；模型最多 2 次逻辑请求，见下） |
| 模型请求上限 | **最多 2 次逻辑请求**：第一次产出工具调用，第二次消费结果并完成；第 3 次请求发生前硬停止（SDK/业务自动重试一律禁止）。执行门禁：worker 的 provider 流式入口由 `ModelRequestBudget` 在外发前同步计数并拦截（`pi_decision_limit`），不是文字约束 |
| L1 Runtime Config | tenant-a 使用 `limits.max_decisions=2` 的 append-only 配置版本（L0 初始化创建并激活）；L1 Run 的 snapshot 绑定该版本 |
| 验证点 | durable preflight 提交 → adapter 外发 → finalize 全链路；Evidence、租户账本（恰好 −10）、usage 记录、审计行齐备；provider guard 计数与去重后的 RuntimeUsageRecord 对账一致 |
| 禁止事项 | 禁止自动重试；失败立即停止，不进入 L2 |
| 预期 terminal | `completed`（或稳定分类的失败——仍判 L1 不通过并停止） |
| 回滚动作 | 无需状态回滚；证据保留 |
| 授权级别 | L1（模式 A：由阶段 B 授权文本；模式 B：由一次性授权文本的「允许的 Level」与「最终 L1 MCP 工具」字段单独开启） |

### 6.3 Level 2：完整真实 B7（20 个场景）

预算列统一为 `NEEDS_USER_APPROVAL`（最大 Run/MCP/token/积分，且 ≤ §5 全局预算）。

**L1→L2 Runtime Config 版本规则（append-only，授权的状态变更）**：

- L1 成功后、进入 L2 前，经管理 API 创建并激活 tenant-a 的新 append-only 配置版本，
  `limits.max_decisions=50`（L2 上限）；L1 若失败，不得激活 L2 配置。
- 激活只影响新 Run：既有 L1 Run 的 `runtime_config_snapshot_json` 逐字节不变、不重绑定；
  新 L2 Run 必须绑定新版本（snapshot `config_version_id` == 新版本 id）。
- Evidence 记录两个 Config ID/version、激活时间与每个相关 Run 的 snapshot 绑定关系。
- 模型请求预算以 worker provider guard（`ModelRequestBudget`）计数为准，并与去重后的
  RuntimeUsageRecord 对账；两者不一致即停止。

**L2-01 品牌报告完整链路**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 否
预期 Artifact：`brand` 模块正式 Version（契约 `brand_report_v3`）+ Excel 导出；预期 terminal `completed`/`completed_with_warnings`
必需 Evidence：全部 settled MCP 的 tool_call Evidence、lineage、publish 事件、导出 SHA-256
场景特有停止条件：Artifact 无法通过发布门禁即停
回滚动作：无（只读类业务产物保留作证据）｜授权级别 L2

**L2-02 活动报告完整链路**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 否
预期 Artifact：`campaign` 模块正式 Version + Excel 导出；预期 terminal `completed`/`completed_with_warnings`
必需 Evidence：同 L2-01
场景特有停止条件：同 L2-01
回滚动作：无｜授权级别 L2

**L2-03 KOL 圈选、KOL 分析与 Excel 导出**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 否
预期 Artifact：`kol-selection` 与 `kol-analysis` 模块正式 Version + 达人 Excel 导出；预期 terminal `completed`/`completed_with_warnings`
必需 Evidence：圈选证据、评分快照、分析 narrative 来源 Evidence、导出 SHA-256
场景特有停止条件：圈选名单与导出内容不一致即停
回滚动作：无｜授权级别 L2

**L2-04 钻取绑定确切 Version，0 新 DataTap**
真实模型 是｜真实 DataTap **否（0 次新外发为硬断言）**｜测试钱包变更 否｜状态变更 否
预期 Artifact：`insight` 模块 Version，parent 绑定确切父 Version；预期 terminal `completed`
必需 Evidence：父 Version 读取记录、lineage parent 关联、0 外发证明（无外发日志/无新 ToolCall）
场景特有停止条件：发生任何新 DataTap 外发即停
回滚动作：无｜授权级别 L2

**L2-05 澄清输入：0 Artifact、0 MCP、0 扣费**
真实模型 是｜真实 DataTap 否｜测试钱包变更 **否（0 扣费为硬断言）**｜状态变更 否
预期 Artifact：无；预期 terminal `clarification_requested`
必需 Evidence：0 ToolCall、0 账本流水、0 Artifact 的数据库证明
场景特有停止条件：发生扣费/外发/Artifact 即停
回滚动作：无｜授权级别 L2

**L2-06 非营销拒答：0 Artifact、0 MCP、0 扣费**
真实模型 是｜真实 DataTap 否｜测试钱包变更 否（硬断言）｜状态变更 否
预期 Artifact：无；预期 terminal `completed`（拒答回答）
必需 Evidence：同 L2-05
场景特有停止条件：同 L2-05
回滚动作：无｜授权级别 L2

**L2-07 余额不足：0 外部调用**
真实模型 是（至多到达首次 preflight 前）｜真实 DataTap **否（0 外发为硬断言）**｜测试钱包变更 是（需先把测试钱包余额调到 <10）｜状态变更 **是（钱包调整）**
预期 Artifact：无；预期 terminal `insufficient_balance`（或既有稳定终态码）
必需 Evidence：0 外发证明、钱包 0 变动、无 reserve/settle/release 流水
场景特有停止条件：发生任何真实外发即停
回滚动作：恢复测试钱包初始余额（授权文本明示）｜授权级别 L2+D(钱包调整)

**L2-08 License 暂停：后续 MCP 0 外发**
真实模型 是｜真实 DataTap 是（暂停前已确认次数）｜测试钱包变更 是（暂停前已结算部分）｜状态变更 **是（License suspend）**
预期 Artifact：按模型行为而定，允许无正式 Artifact；预期 terminal `failed`/`completed_with_warnings`（稳定分类）
必需 Evidence：暂停前 settled 记录、暂停后 preflight 拒绝记录、0 新增外发/流水证明
场景特有停止条件：暂停后发生任何新外发即停
回滚动作：恢复测试 License 至授权前状态｜授权级别 L2+D(License suspend)

**L2-09 两租户隔离**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是（两租户各自账本）｜状态变更 否
预期 Artifact：两租户各自业务 Artifact；预期 terminal 各自 `completed`/`completed_with_warnings`
必需 Evidence：跨租户读 session/run/events/Artifact/账务全部 404 的证明、DB 层互不可见抽查
场景特有停止条件：出现任何跨租户数据/事件/Artifact/账务记录即停（全局硬停止）
回滚动作：无｜授权级别 L2

**L2-10 SSE 顺序与 Last-Event-ID 续传**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 否
预期 Artifact：随主场景产物；预期 terminal 与主场景一致
必需 Evidence：事件序号单调递增、`message.completed` 先于唯一终态、断线 `Last-Event-ID` 重连无洞无重复
场景特有停止条件：终态顺序错误/多终态即停（全局硬停止）
回滚动作：无｜授权级别 L2

**L2-11 cancel 收口**
真实模型 是｜真实 DataTap 是（取消前已确认部分）｜测试钱包变更 是（取消前已结算部分）｜状态变更 否
预期 Artifact：允许无；预期 terminal `cancelled`（唯一、不翻转）
必需 Evidence：cancel ack、未外发预留释放记录、终态唯一性证明
场景特有停止条件：取消后仍发生新外发即停
回滚动作：无｜授权级别 L2

**L2-12 一次基础设施恢复，第二次稳定 failed**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 **是（Gateway/Worker 强制终止，含 SIGKILL）**
预期 Artifact：无正式 Artifact 要求；预期 terminal `failed`（恰好 2 个 Attempt，无第三次重试）
必需 Evidence：Attempt 1 丢失 → 恢复恰好一次新 Attempt → 再次丢失 → 稳定 failed；崩溃窗口内 ToolCall 置 unknown 且 0 重放
场景特有停止条件：出现第二次以上恢复或 unknown 被重放即停
回滚动作：测试 Gateway 恢复正常运行｜授权级别 L2+D(Gateway restart/worker kill)

**L2-13 draining 停止新 claim、完成在途 Run**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 **是（Gateway draining 置位/复位）**
预期 Artifact：在途 Run 正常产物；预期 terminal 在途 `completed`，新 Run 保持 `queued`
必需 Evidence：draining 期间新 Run 0 claim、在途完成、恢复 active 后 queued 被领取
场景特有停止条件：draining 期间发生新 claim 即停
回滚动作：Gateway 恢复 active｜授权级别 L2+D(Gateway draining)

**L2-14 current → pi → current**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 **是（租户 runtime_backend 切换 ×2）**
预期 Artifact：两个 backend 各自产物；预期 terminal 各自正常终态
必需 Evidence：切换只影响新 Run、同一消息不被两个 Runtime 执行、旧 Run snapshot 不变
场景特有停止条件：同一消息双执行即停（全局硬停止）
回滚动作：租户切回授权前 backend｜授权级别 L2+D(current/pi 切换)

**L2-15 kill switch 只影响新 Run**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 **是（kill switch 置位/复位）**
预期 Artifact：无新要求；预期 terminal 在途 Pi Run 正常完成、新 Run 走 current
必需 Evidence：置位后新 Run `runtime_backend=current`、在途 Pi 不被杀、历史 snapshot 不变
场景特有停止条件：在途 Run 被强杀或历史 Run 被改写即停
回滚动作：kill switch 复位｜授权级别 L2+D(kill switch)

**L2-16 Run snapshot 不可变**
真实模型 是｜真实 DataTap 是｜测试钱包变更 是｜状态变更 **是（激活新 Runtime Config 版本）**
预期 Artifact：新旧配置版本下各自产物；预期 terminal 各自正常
必需 Evidence：激活新版本后旧 Run `runtime_config_snapshot_json` 逐字节不变、版本指针不变
场景特有停止条件：任何历史 snapshot 变化即停
回滚动作：无（append-only，旧版本自然 retired）｜授权级别 L2+D(Runtime Config 激活)

**L2-17 Artifact/Version/Evidence/Excel/BI/B0 Gate 同版本**
真实模型 否（复用 L2-01/02/03 产物做核验）｜真实 DataTap 否｜测试钱包变更 否｜状态变更 否
预期 Artifact：无新产物（核验性场景）；预期 terminal NOT_APPLICABLE
必需 Evidence：Excel 导出与 BI 详情绑定同一 Version、B0 发布门禁对正式产物复核零 issue、lineage 全链一致
场景特有停止条件：版本绑定不一致即停（全局硬停止）
回滚动作：无｜授权级别 L2

**L2-18 钱包净支出 == 已确认 MCP 外发次数 × 10**
真实模型 否｜真实 DataTap 否｜测试钱包变更 否（只读核对）｜状态变更 否
预期 Artifact：无；预期 terminal NOT_APPLICABLE
必需 Evidence：账本逐笔清单、ToolCall 状态分布、净支出恒等式计算过程
场景特有停止条件：恒等式不成立即停（全局硬停止）
回滚动作：无｜授权级别 L2

**L2-19 model usage 与账务 reconciliation**
真实模型 否｜真实 DataTap 否｜测试钱包变更 否（只读核对）｜状态变更 否
预期 Artifact：无；预期 terminal NOT_APPLICABLE
必需 Evidence：`RuntimeUsageRecord` 与模型请求逐条对应、无重复计费、usage 未进用户 SSE/prompt
场景特有停止条件：对账 mismatch 即停
回滚动作：无｜授权级别 L2

**L2-20 结束后进程、端口、租约、nonce 与测试数据检查**
真实模型 否｜真实 DataTap 否｜测试钱包变更 否｜状态变更 **是（测试数据按 §2.12 批准策略清理）**
预期 Artifact：无；预期 terminal NOT_APPLICABLE
必需 Evidence：无残留进程/端口/租约/nonce 证明、测试数据保留/清理清单
场景特有停止条件：存在无法有界退出的进程/租约即停（全局硬停止）
回滚动作：按批准策略完成清理｜授权级别 L2+D(测试数据清理)

## 7. 授权流程（两种合法模式）

授权有两种合法模式，由用户选择（2026-08-12 的模式 B round 已执行并封存，新执行须重新授权）：

- 模式 A（推荐）：两阶段授权。阶段 A（`L0_PRECHECK_AUTHORIZATION`）与阶段 B
  （`REAL_B7_CALL_AUTHORIZATION`）必须由用户在**两条独立的新消息**中分别完整确认；阶段 B
  消息必须晚于阶段 A 且晚于 L0 全部通过。
- 模式 B：一次性完整授权（`ONESHOT_FULL_AUTHORIZATION`）。用户在**一条新消息**中明确填写
  全部字段，一次性授权 L0→L1→L2 连续执行，无需第二条授权消息。

授权文本模板见授权包 §5（§5.1/§5.2 为模式 A，§5.3 为模式 B）；所有模板均不存在任何可留空
后补的字段，任何留空/占位项视为未授权。模式 B 不降低任何预算、隔离、destructive 开关或
停止条件要求：预算表（§5）、隔离要求（§2）、8 个 destructive 逐项开关、19 条硬停止条件与
模式 A 完全相同；L0 任一失败仍必须停止，L1 失败不得进入 L2。

### 7.1 模式 A 阶段 A：L0_PRECHECK_AUTHORIZATION

1. operator 在提出授权之前：确认工作树干净、位于授权包最终修复提交之后的分支尖端，
   以 `git rev-parse HEAD` 取得 execution commit，并按 §1.1 构造完整确定的 round_id。
   本步骤只读：不得创建任何目录、不得连接任何环境（数据库/模型/DataTap/钱包）。
2. operator 按授权包 §5.1 模板预填全部字段（round_id、execution commit SHA、branch、
   migration head、隔离测试环境标识、隔离测试数据库引用、operator、reviewer、时间窗口、
   evidence directory 等），提交给用户。
3. 用户在**新消息**中完整确认阶段 A 授权文本（`authorization message reference` 填
   `THIS_MESSAGE`）。operator 收到后只做记录：把平台消息 ID/任务 ID、收到时刻（UTC）与
   授权文本全文的 SHA-256 写入 `authorization-phase-a.md`（L0-10），不得修改用户原文。
4. 阶段 A 仅授权四件事：本地构建（不连接任何外部环境）、专用库环境初始化（仅经生产
   domain/admin service 幂等写入并写审计/账本，见 §6.1 L0-00；禁止直接写
   `encrypted_runtime_secrets`）、专用库只读预检（Runtime Config/License/TenantWallet/
   quota 元数据，SELECT only）、证据目录创建与首帧写入。
5. 阶段 A 明确禁止：真实模型调用、DataTap 外发、Run 级计费（reserve/settle/release）
   与初始化之外的任何钱包/额度变更、一切 destructive state change。
6. 按 §6.1 执行 L0-00…L0-12；任一失败即停止，阶段 B 不得提出。

### 7.2 模式 A 阶段 B：REAL_B7_CALL_AUTHORIZATION

1. 仅在 L0 全部通过后准备。operator 按授权包 §5.2 模板预填：与阶段 A 完全一致的 round_id
   与 execution commit SHA；L0 固定值（Gateway build hash、dependency snapshot、
   catalog/schema digests、Runtime Config ID/version、License ID/version、
   tenant/user/gateway IDs）；三个网络 origin（模型/DataTap/FastAPI 控制面）；9 项凭证的
   reference/fingerprint/key version/owner/expiry/host/status（只记录引用与掩码）；
   最终 L1 MCP 工具、13 个预算数值、允许的 Level、8 个 destructive 开关、清理/保留策略。
2. 用户必须在**另一条新消息**中完整确认阶段 B 授权文本，并再次确认授权包 §3 全部 19 条
   硬停止条件生效。operator 按 §7.1.3 同样规则记录消息引用并写入
   `authorization-phase-b.md`（首个真实调用之前）。
3. 阶段 B 确认之前，任何真实模型/DataTap/钱包操作均禁止；确认之后也只允许按授权的
   Level、预算、工具与 destructive 开关执行，不得超出。

### 7.3 模式 B：ONESHOT_FULL_AUTHORIZATION（一次性完整授权）

1. operator 在提出授权之前：确认工作树干净、位于授权包最终修复提交之后的分支尖端，
   以 `git rev-parse HEAD` 取得 execution commit 候选，并按 §1.1 给出 round_id 身份规则。
   本步骤只读：不得创建任何目录、不得连接任何环境（数据库/模型/DataTap/钱包）。
2. operator 按授权包 §5.3 模板预填全部字段，提交给用户；用户在**一条新消息**中完整确认
   （`authorization message reference` 填 `THIS_MESSAGE`）。operator 收到后只做记录：
   平台消息 ID/任务 ID、收到时刻（UTC）与授权文本全文 SHA-256，round 开启时写入
   `authorization.md`（L0-10），不得修改用户原文。
3. 启动门禁（fail-closed，任一不符即 B7_BLOCKED）：工作树干净、HEAD 为已审核 L1 repair
   baseline `68deca58…` 的线性后代且包含 `f8a4ffa`/`494d20e`/`c22a3a1`/`8a5f264`/`68deca5`
   五个修复提交、`68deca58…` 至 HEAD 区间仅已审核文档纠偏、§2.0 数据库
   身份逐项核验（含专用账号访问 `kol_insight` 被 MySQL 1142 拒绝）与新 round 数据库状态
   门禁（§2.0：before snapshot 只读记录、新 round_id 对应 tenant/user/gateway/Run 为 0、
   历史 retained 行只读不得复用或修改）、Keychain 引用可读取
   （值仅进程内）。2026-08-12 首次尝试即因工作树未提交改动在此 fail-closed。
4. 启动门禁通过后按 §6.1 执行 L0-00…L0-12；任一失败即停止，不进入 L1。L0 产出值
   （tenant/user/gateway IDs、Runtime Config/License ID/version、Gateway build hash、
   catalog/schema digests）逐字录入证据后视为授权固定值，任何未授权变化命中授权包 §3-18。
5. L0 全部通过后进入 L1；L1 通过后进入 L2；L1 失败不得进入 L2。全程不存在第二条授权
   消息要求。
6. 模式 B 授权消息即完整授权面：执行不得超出其 Level、预算、工具、网络 origin 与
   destructive 开关。

### 7.4 执行与收口（角色分离）

1. 按 L0 → L1-00 → L1 → L2 顺序执行；L1 失败不得进入 L2；任一层级命中硬停止条件即终止
   整个 round。
2. 每个场景结束即按授权包 §2 追加证据帧（flush/fsync）。
3. **operator 的收口权限仅限**：追加 `execution_completed` / `execution_stopped` manifest 帧
   与 operator summary 文本。operator **禁止**写 `round_sealed` 帧、`verdict.md`、
   `hashes.sha256`。
4. **independent reviewer 封口**：复核证据后追加 reviewer 帧与 `round_sealed` 帧，单次写入
   `verdict.md`（B7_PASS / B7_FAIL / B7_BLOCKED），最后单次写入 `hashes.sha256`（覆盖目录内
   除自身外全部文件，含 verdict.md）；此后证据目录不可变。
5. 停止或失败后：保留 append-only 证据、不覆盖不删除失败 round、不在同一 round 修代码、
   不自动新建下一 round；修复与新授权是下一轮的前提。**历史失败 round（如
   `REAL_B7_20260812T045636Z_b801c490`）的证据目录只读封存，禁止补写、修改或覆盖。**
