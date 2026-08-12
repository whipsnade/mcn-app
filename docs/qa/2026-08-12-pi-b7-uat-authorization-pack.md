# Pi Agent Gateway 真实 B7 UAT 授权包

日期：2026-08-12（第二轮修复，替代同日第一版与修复轮第一版）
配套授权计划：`docs/superpowers/plans/2026-08-12-real-b7-uat-authorization-plan.md`
（round 身份、专用隔离环境、凭证引用、工具/网络 allowlist、预算授权表、Level 0/1/2 场景矩阵、
授权流程 §7：模式 A 两阶段 / 模式 B 一次性）。

```text
Status: READY_FOR_REAL_B7_UAT_REAUTHORIZATION（round REAL_B7_20260812T045636Z_b801c490 已执行并封存：
L0 通过；L1 FAIL——mcp_tool_identity_invalid，0 外发 0 扣费，账务恒等式成立；按「L1 失败不得
进入 L2」规则终止。修复轮（§6.6）已收口；旧一次性授权已随失败 round 消费完毕，真实 B7 必须
由用户按 §5.3 模板重新授权）
Real external calls authorized: NO（旧授权已随失败 round 封存；新授权前任何真实外部调用禁止）
Production cutover authorized: NO
Historical Task 9 rerun authorized: NO
Plan C authorized: NO
```

本授权包只做授权方案设计与证据规范：不执行真实 B7 UAT，不连接真实模型、DataTap、钱包、
预生产或生产环境，不创建真实 round 目录。历史 Pi RPC/POC 真实六场景 Task 9（round
`20260808T060814Z`）永为 `EVALUATED_FAIL`，不在本包任何授权范围内。

状态流转说明：授权包第一版（`f7ab159`）经 2026-08-12 架构复核（Critical 0 / Important 3 /
Minor 1）进入 `NEEDS_AUTHORIZATION_PACK_REPAIR`；修复轮第一版关闭全部发现（§6.1–§6.4）。
第二轮修复（§6.5）：用户选定模式 B（一次性完整授权 L0→L1→L2），专用隔离环境绑定写入
计划 §2.0。模式 B round `REAL_B7_20260812T045636Z_b801c490` 于 2026-08-12 执行：启动门禁
与 L0 全部通过；L1 因真实模型经通用 mcp 代理以裸 remote 名寻址被 `mcp_tool_identity_invalid`
拦截（0 外发、0 扣费、19 条硬停止无一触发）而 FAIL，按规则终止并封存（证据目录只读）。
第三轮修复（§6.6）：MCP 通用代理身份映射 + adapter toolPrefix + Evidence 生成器 +
L0/L1-00 流程与封口角色修订；状态进入 `READY_FOR_REAL_B7_UAT_REAUTHORIZATION`——
真实 B7 必须由用户按 §5.3 模板重新授权（execution commit 为修复提交后的 clean HEAD）。

## 1. 授权包范围

- 本包定义：append-only 证据设计（§2）、硬停止条件（§3）、自审结果（§4）、授权文本模板
  （§5，模式 A 两阶段 + 模式 B 一次性）、修复记录与字段对照（§6）。
- 场景、预算、环境、凭证与 allowlist 的字段状态以授权计划为准；两文档冲突时以更严格者为准。
- 字段状态只允许 `VERIFIED` / `NEEDS_USER_INPUT` / `NEEDS_USER_APPROVAL` / `NOT_APPLICABLE`。

## 2. append-only 证据设计

### 2.1 目录结构（仅设计，本轮不创建真实 round 目录）

```text
docs/qa/evidence/pi-b7/<round_id>/
  authorization.md            # 模式 B 专用：一次性授权文本全文 + round_id + 实际 execution HEAD + 数据库身份 + Keychain reference + 平台消息 ID/任务 ID + 授权文本 SHA-256（L0-10 单次写入，写后不可变）
  authorization-phase-a.md    # 模式 A 专用：阶段 A 授权文本全文 + round_id + 平台消息 ID/任务 ID + 授权文本 SHA-256（单次写入，写后不可变）
  authorization-phase-b.md    # 模式 A 专用：阶段 B 授权文本全文 + 同一 round_id + 平台消息 ID/任务 ID + 授权文本 SHA-256（阶段 B 确认后、首个真实调用前单次写入，写后不可变）
  manifest.jsonl              # canonical JSONL hash chain：round 生命周期事件与全局清单帧（字段见 §2.3）
  scenario-results.jsonl      # canonical JSONL：逐场景结果帧（terminal、外发计数、钱包前后、判定）
  run-identities.jsonl        # canonical JSONL：Session/Run/Attempt/Step/ToolCall/Event source id 对照帧
  accounting-summary.jsonl    # canonical JSONL：账本逐笔 ID、reserve/settle/release、净支出恒等式核算帧
  usage-reconciliation.jsonl  # canonical JSONL：RuntimeUsageRecord 与模型请求对账帧
  artifact-lineage.jsonl      # canonical JSONL：Artifact/Version/Evidence/导出文件血缘与 SHA-256 帧
  event-ordering.jsonl        # canonical JSONL：SSE 序号、message.completed 与终态顺序、续传检查帧
  security-scan.jsonl         # canonical JSONL：secret/脱敏扫描结果帧（只含命中位置与变量名，不含值）
  environment.json            # 单次写入不可变快照：OS/运行时版本、masked origin、隔离声明核对结果
  dependency-versions.json    # 单次写入不可变快照：npm ls --depth=0、后端依赖快照、Gateway dist build hash
  catalog-digests.json        # 单次写入不可变快照：Level 0 读取的目录行 internal_name/service/digest/review_status
  process-cleanup.txt         # 单次写入文本（L2-20）：进程/端口/租约/nonce/测试数据收口记录
  operator-summary.md         # operator 执行摘要（单次写入；非 reviewer 结论）
  verdict.md                  # 单次写入（仅 independent reviewer 封口）：判定 B7_PASS / B7_FAIL / B7_BLOCKED + 理由
  hashes.sha256               # 单次写入（仅 independent reviewer 封口最后一步）：上述全部文件的 SHA-256（自身除外）
```

规则：

- 追加型文件（8 个 `.jsonl`）只允许在文件末尾追加 canonical JSONL 帧（§2.2）；快照型
  `.json` 与 `.md`/`.txt` 文本各单次写入、写后不可变。
- 每个场景结束立即追加对应帧；任何帧的 write/flush/fsync 失败本身是硬停止条件（§3-14）。
- 失败 round 永久保留，不得覆盖或删除；修复后另起新 round。

### 2.2 写入规则：canonical JSONL 与 hash chain

**canonical 序列化**：UTF-8 编码；键按 ASCII 字典序排列；紧凑分隔符（`,` 与 `:`，无冗余
空白）；非 ASCII 字符不转义；每行恰好一个 JSON object，以 LF（`\n`）结束，无空行，文件以
最后一个 LF 收尾。等价配方：Python
`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 的 UTF-8 字节。

**帧必填字段**（每帧）：

| 字段 | 含义 |
| --- | --- |
| `sequence` | 整数，从 1 起逐帧 +1（文件内连续单调） |
| `timestamp` | UTC ISO-8601 秒级（如 `2026-08-12T06:00:00Z`） |
| `scenario_id` | 场景 ID；round 级帧固定为 `"round"` |
| `prev_hash` | 上一帧的 `record_hash`；首帧固定为 64 个 `0` |
| `record_hash` | 对本帧移除 `record_hash` 字段后的 canonical 字节做 SHA-256（64 位小写十六进制） |
| `type` | 帧类型（如 `round_opened` / `l0_check` / `scenario_result` / `round_sealed`） |
| `payload` | 帧内容对象（§2.3 字段位于其中） |

**写入协议**：追加一帧 → `flush()` → `fsync()` 文件描述符；任一步失败即命中 §3-14，
整个 round 硬停止。

**不可变纪律**：禁止修改、删除、重排、插入任何既有帧；只允许在文件末尾追加新帧。
写错的帧不得修复——以新帧记录更正并引用原帧 `sequence`，原帧保留。

**hash-chain 校验方法**（reviewer 封口前必须对每个 `.jsonl` 逐文件执行）：

1. 逐行读取；每行重新 canonical 序列化并与该行原始字节逐字节比对（检出空白/键序/编码篡改）；
2. `sequence` 必须等于行号（首行 1）；
3. `prev_hash` 必须等于上一帧的 `record_hash`（首帧为 64 个 `0`）；
4. 重算 `record_hash` 并与帧内值比对。

任一项不符即证据被篡改或损坏：命中 §3-14，round 判 `B7_FAIL`，证据原样保留待查。

**快照与文本文件**：3 个 `.json` 快照在对应 L0 预检步各单次写入（同一 canonical 序列化 +
flush/fsync），写后不可变；`authorization.md`（模式 B，L0-10）/ `authorization-phase-a.md`
（模式 A，L0-10）/ `authorization-phase-b.md`（模式 A，阶段 B 确认后、首个真实调用前）/
`process-cleanup.txt`（L2-20）各自单次写入，写后不可变。

**封口（角色分离）**：operator 只允许追加 `execution_completed` / `execution_stopped` manifest
帧并单次写入 `operator-summary.md`；operator **禁止**写 `round_sealed` 帧、`verdict.md`、
`hashes.sha256`。independent reviewer 复核后追加 reviewer 帧与 `round_sealed` 帧（记录每个
`.jsonl` 的末帧 `record_hash` 链头以便肉眼比对），单次写入 `verdict.md`，最后单次写入
`hashes.sha256`（覆盖目录内除自身外全部文件，每行 `<sha256>  <相对路径>`，含 verdict.md）。
封口后目录内任何文件不得再变动。历史失败 round 的证据目录只读封存，禁止补写、修改或覆盖。

### 2.3 manifest.jsonl 帧必须记录的字段（位于帧 payload 内）

| 字段 | 脱敏规则 |
| --- | --- |
| round_id | 明文（身份字段，非机密） |
| commit SHA | 明文（完整 40 位） |
| migration head | 明文（如 `0043_billing_downgrade_guard`） |
| dependency versions | 明文（包名+版本） |
| masked environment fingerprint | 只允许掩码指纹（如 origin 主机名 + `••••last4`），不含凭证值 |
| tenant/user/gateway identity | 测试环境 ID 明文允许；不得包含真实用户手机号明文（掩码） |
| Runtime Config/License version | ID + version 明文允许；secret 只出现 fingerprint/masked |
| scenario ID | 明文 |
| Session/Run/Attempt/Step/ToolCall IDs | 明文（内部 UUID） |
| external call count | 整数 |
| wallet/quota before and after | 整数积分值（测试钱包） |
| ledger transaction IDs | 明文 ID，不含金额以外的敏感字段 |
| usage record IDs | 明文 ID |
| Artifact/Version/Evidence IDs | 明文 ID |
| export file SHA-256 | 明文哈希 |
| sanitized log SHA-256 | 明文哈希（日志本体先脱敏再哈希） |
| terminal status | 稳定终态码 |
| stop condition | 命中的 §3 条目编号，未命中写 `none` |
| reviewer verdict | `B7_PASS` / `B7_FAIL` / `B7_BLOCKED` + reviewer 身份 |

### 2.4 禁止进入证据的内容

以下任何一项出现在证据中即触发 §3-1（凭证/secret 泄露）或相应硬停止：

- secret、token、Cookie、Authorization header；
- HMAC 签名本体与签名串原文；
- Runtime secret 明文（含 secret envelope 解密结果）；
- 完整数据库 DSN（含用户名/密码/主机端口组合）；
- 未脱敏手机号；
- 原始供应商敏感错误（只允许稳定错误码与脱敏摘要）；
- 无必要的用户 Prompt 全文（默认只记哈希与长度，确需引用时最小摘录并说明理由）；
- 原始 DataTap payload 中的敏感字段（Evidence 以归一化后的落库形状为准）。

## 3. 硬停止条件

出现以下任一条件，立即终止整个 round，不得自动重试或继续后续场景：

1. 任何凭证或 secret 泄露（含证据、日志、命令行输出、SSE、管理响应）。
2. 请求到达 §4.3（授权计划）allowlist 之外的主机。
3. catalog/schema digest 漂移（含新工具出现、已审核工具签名变化），或授权计划 §2.1 固定
   quarantine 基线（`insight-cube-mcp`/`query_user_info` 的 digest、行数、review_status）
   发生任何变化。
4. 出现跨租户数据、事件、Artifact 或账务记录。
5. 同一逻辑 MCP 调用重复外发。
6. 未完成 durable preflight 就发生外发。
7. 钱包净支出与已确认外发次数 × 10 不一致。
8. `unknown` 被自动重放。
9. `message.completed` 与 terminal 顺序错误。
10. 出现多个 terminal。
11. Artifact/Version/Evidence lineage 不一致。
12. Excel 和 BI 未绑定同一 Version。
13. 预算任一上限达到（授权计划 §5 任一字段）；L1-SMOKE 的模型逻辑请求达到 2 次上限时，
    在第 3 次请求发生前即停（SDK/业务自动重试一律禁止）。
14. 证据帧无法追加或落盘（write/flush/fsync 失败），或 hash-chain 校验发现篡改/损坏。
15. 外部超时或错误无法稳定分类。
16. Gateway/worker 无法有界退出。
17. kill switch 或 current 回滚不可用。
18. License/Runtime Config 在 round 中发生未授权变化。
19. 测试环境被证明不是隔离环境。

停止后行为（全部强制）：

- 保存 append-only 证据；
- 不覆盖或删除失败 round；
- 不在同一 round 中修代码；
- 不自动新建下一 round；
- 输出 `B7_FAIL` 或 `B7_BLOCKED`；
- 等待新修复计划和新的用户授权。

## 4. 自审结果（2026-08-12 修复轮，文档落库前逐项核对）

1. [x] 授权流程两种合法模式定义清楚且无冲突：模式 A 两阶段（阶段 A
   L0_PRECHECK_AUTHORIZATION / 阶段 B REAL_B7_CALL_AUTHORIZATION，两条独立新消息）；
   模式 B 一次性完整授权（ONESHOT_FULL_AUTHORIZATION，一条新消息填写全部字段，授权
   L0→L1→L2，无第二条授权消息要求）。模式 B 不降低预算、隔离、destructive 开关与停止
   条件要求；L0 失败仍停止、L1 失败不得进入 L2（计划 §7、本包 §5）。本次执行采用模式 B。
2. [x] 两份模板不存在任何"留空后由 operator 回填"的字段；round_id 等全部授权字段必须在
   用户授权消息中给出确定值；`authorization message reference` 一律由用户填
   `THIS_MESSAGE`，operator 只记录平台消息 ID/任务 ID 与授权文本 SHA-256，不改原文。
3. [x] 文档未把任何历史 commit 写成"当前 HEAD"：`61576f7…` 仅标注为修复前 production
   baseline；execution commit 以授权时点 `git rev-parse HEAD` 为准，round_id 的 short
   commit 取自用户最终批准的 execution commit；文档不内嵌任何自指 SHA。
   （2026-08-12 门禁纠偏后：执行身份锚定已审核 L1 repair baseline `68deca58…`，见 §5.3。）
4. [x] 计划中全部 `NEEDS_USER_APPROVAL` / `NEEDS_USER_INPUT` 字段在两份模板中均有填写位
   （逐项对照见 §6.3）。
5. [x] append-only 证据重设计：追加型文件改 canonical JSONL + `prev_hash`/`record_hash`
   链，逐帧 flush/fsync，失败即硬停止；快照单次写入不可变；`verdict.md` 与
   `hashes.sha256` 仅封口写一次；禁止修改/删除/重排既有帧，校验方法已定义（§2.2），
   并经本地脱机自洽性验证（§6.4）。
6. [x] L0 文案：不调用真实模型/DataTap、不发生 Run 级计费；专用空库的环境初始化例外只经
   生产 domain/admin service 幂等写入并写审计/账本（禁止直接写 `encrypted_runtime_secrets`）；
   其余预检保持只读（Runtime Config、License、TenantWallet 与 quota 元数据，SELECT only）
   （计划 §6.1）。
7. [x] 两份文档均无任何明文凭证（只出现变量名、引用占位与掩码位）。
8. [x] 没有把本地离线 fake topology 写成真实 B7；没有把 `READY_FOR_REAL_B7_UAT_REVIEW`
   写成 PASS（全文无 B7 PASS 表述）。
9. [x] 没有授权历史真实 Task 9（round `20260808T060814Z` 永为 `EVALUATED_FAIL`）、
   生产切流或方案 C。
10. [x] 每个外部调用场景都有预算字段与停止条件；每个状态变更场景都有显式 `+D(...)`
    授权位（阶段 B 模板逐项开关）。
11. [x] 每个证据字段都有脱敏规则（§2.3/§2.4）。
12. [x] 所有失败均保留 append-only round（§2.1/§3 停止后行为）；本授权包本身不触发任何
    外部连接（纯 Markdown，无可执行内容）。

## 5. 授权文本模板（两种授权模式）

> 供用户复制、填写并在新消息中完整确认。模式选择：
>
> - 模式 A（两阶段，推荐）：§5.1 与 §5.2 各自在**独立的新消息**中完整确认；阶段 B 模板只能
>   在 L0 全部通过后提出，且必须在阶段 A 授权消息之后的另一条新消息中确认。
> - 模式 B（一次性完整授权）：§5.3 在**一条新消息**中完整确认，一次性授权 L0→L1→L2，无
>   第二条授权消息要求。本次执行（2026-08-12）采用模式 B。
>
> 通用规则：
>
> - 所有模板不存在任何可留空后补的字段；任何留空/占位项视为未授权，对应模板整体无效。
> - `authorization message reference` 一律由用户填写 `THIS_MESSAGE`。operator 收到授权
>   消息后，在 evidence 的 `authorization-phase-{a,b}.md`（模式 A）或 `authorization.md`
>   （模式 B）中记录：平台消息 ID/任务 ID（如平台提供）、收到时刻（UTC）、授权文本全文的
>   SHA-256；授权文本全文按原字节收录，operator 不得修改用户原授权文本。
> - 两种模式互斥：一个 round 只能按一种模式授权。模式 B 下 L0 产出值（tenant/user/gateway
>   IDs、Runtime Config/License ID/version、Gateway build hash、catalog/schema digests）
>   逐字录入证据后视为授权固定值，任何未授权变化命中 §3-18。

### 5.1 模式 A 阶段 A 模板（L0_PRECHECK_AUTHORIZATION）

```text
REAL B7 UAT 阶段 A 授权确认（L0_PRECHECK_AUTHORIZATION）

我授权在以下约束内执行一次真实 B7 UAT round 的 Level 0 零外发预检：

- authorization phase: L0_PRECHECK_AUTHORIZATION
- round_id: <REAL_B7_YYYYMMDDTHHMMSSZ_short8，operator 提出的完整确定值>
- execution commit SHA: <完整 40 位，授权时点 git rev-parse HEAD>
- branch: <分支名>
- migration head: <授权时 alembic heads 实际值>
- 隔离测试环境标识: <环境/账号标识；确认与开发/预生产/生产物理或账户级隔离>
- 隔离测试数据库引用: <secret store 引用，非 DSN；确认独立实例或独立 schema>
- 隔离要求: 确认授权计划 §2 全部 12 项隔离要求成立: <YES>
- evidence directory: docs/qa/evidence/pi-b7/<round_id，与上行逐字一致>/
- operator: <执行人>
- independent reviewer: <独立复核人，不得与 operator 同一人>
- 时间窗口: <起止时间，含时区>
- 阶段 A 授权范围（仅以下三项，逐项确认）:
  - 本地构建（不连接任何外部环境）: <YES>
  - 隔离测试数据库只读预检（Runtime Config/License/TenantWallet/quota 元数据，SELECT only）: <YES>
  - 证据目录创建与首帧写入: <YES>
- 阶段 A 禁止事项确认（真实模型调用 / DataTap 外发 / 钱包与额度变更 / 一切 destructive state change 均禁止）: <YES>
- 本次授权仅覆盖 L0；L1/L2 须阶段 B 另行授权: <YES>
- stop conditions: 确认授权包 §3 全部 19 条硬停止条件生效: <YES>
- authorization message reference: THIS_MESSAGE

只有用户在新消息中完整确认本授权文本后才允许执行 L0；本文档、Git commit、测试通过或
READY 状态均不构成授权。阶段 A 不授权任何真实模型/DataTap/钱包操作。
```

### 5.2 模式 A 阶段 B 模板（REAL_B7_CALL_AUTHORIZATION）

```text
REAL B7 UAT 阶段 B 授权确认（REAL_B7_CALL_AUTHORIZATION）

本人在阶段 A（L0_PRECHECK_AUTHORIZATION）全部通过后，授权执行真实调用部分：

- authorization phase: REAL_B7_CALL_AUTHORIZATION
- round_id: <与阶段 A 逐字一致的值>
- execution commit SHA: <与阶段 A 逐字一致的完整 40 位>
- branch: <分支名>
- L0 通过确认（L0-01..L0-12 全部通过、证据帧已写入 manifest.jsonl）: <YES>

L0 固定值（与 evidence 内 L0 快照/帧逐字节一致）:
- Gateway build hash: <L0-04 dist 摘要 SHA-256>
- dependency snapshot: <dependency-versions.json 的 SHA-256>
- catalog/schema digests: <catalog-digests.json 的 SHA-256；29 行 digest 以该文件为准>
- Runtime Config ID / version: <ID> / <version>
- License ID / version: <ID> / <version>
- tenant IDs: <至少两个独立测试租户 ID>
- test user IDs: <每租户至少两个测试用户 ID>
- gateway IDs: <测试 Gateway 固定 id 白名单值>

网络 origin（3 项；授权之外的主机一律禁止）:
- 模型 origin: <TENCENT_PLAN_BASE_URL 授权取值>
- DataTap origin: <授权 Runtime Config 的 datatap_urls 取值>
- FastAPI 控制面 origin: <PI_GATEWAY_CONTROL_PLANE_URL 取值>

凭证元数据（9 项；只记录引用与掩码，任何凭证值不得出现）:
- TENCENT_PLAN_BASE_URL: ref=<> fingerprint=<> key_version=<> owner=<> expiry=<> host=<> status=<>
- TENCENT_PLAN_MODEL: ref=<> fingerprint=NOT_APPLICABLE key_version=<> owner=<> expiry=<> host=<> status=<>
- TENCENT_PLAN_API_KEY: ref=<> fingerprint=<> key_version=<> owner=<> expiry=<> host=<> status=<>
- DATATAP_MCP_ORIGIN: ref=<> fingerprint=NOT_APPLICABLE key_version=<> owner=<> expiry=<> host=<> status=<>
- DATATAP_MCP_TOKEN: ref=<> fingerprint=<> key_version=<> owner=<> expiry=<> host=<> status=<>
- PI_GATEWAY_INTERNAL_SECRET: ref=<> fingerprint=<> key_version=<> owner=<> expiry=<> host=<> status=<>
- RUNTIME_SECRET_MASTER_KEYS: ref=<> fingerprint=<> key_version=<> owner=<> expiry=<> host=<> status=<>
- JWT_SECRET: ref=<> fingerprint=<> key_version=<> owner=<> expiry=<> host=<> status=<>
- MySQL credential: ref=<> fingerprint=<> key_version=<> owner=<> expiry=<> host=<> status=<>

执行边界:
- 最终 L1 MCP 工具: <服务 slug / 工具内部名>
- 允许的 Level: <L1 / L1+L2>
- 预算（13 项，全部必填数值）:
  - 最大 round 数: <n>
  - 最大 Run 数（全 round 合计）: <n>
  - 每场景最大 Run 数: <n>
  - 最大 MCP 外发次数（全 round 合计）: <n>（固定每次 10 积分）
  - 单 Run 最大 MCP 次数: <n>
  - 测试钱包初始余额: <n 积分>
  - 最大允许积分净支出: <n 积分>
  - 用户周期额度: <period 周期> / <points_limit 积分上限>
  - 最大模型请求数: <n>
  - 最大输入 token: <n>
  - 最大输出 token: <n>
  - 最大模型费用: <金额与币种>
  - 最大执行时长: <时长>
- 预算到顶行为（VERIFIED，计划强制，无需填写）: 达到任一上限立即停止整个 round，不自动重试、不进入后续场景
- destructive test-state changes（逐项勾选，未勾选即禁止）:
  - Gateway restart / worker kill: <YES/NO>
  - Gateway draining 置位/复位: <YES/NO>
  - License suspend: <YES/NO>
  - Runtime Config 新版本激活: <YES/NO>
  - kill switch 置位/复位: <YES/NO>
  - current/pi 租户 backend 切换: <YES/NO>
  - 测试钱包余额调整（含余额不足场景置低）: <YES/NO>
  - 测试数据清理（结束后）: <YES/NO>
- 清理/保留策略: <证据目录 append-only 永久保留；测试租户/用户/钱包/账本数据的保留期与清理范围>
- evidence directory: docs/qa/evidence/pi-b7/<round_id，与阶段 A 逐字一致>/
- operator: <执行人，与阶段 A 一致>
- independent reviewer: <独立复核人，与阶段 A 一致，不得与 operator 同一人>
- 时间窗口: <起止时间，含时区；不得早于阶段 A 窗口>
- stop conditions: 再次确认授权包 §3 全部 19 条硬停止条件生效: <YES>
- authorization message reference: THIS_MESSAGE

本确认必须发生在阶段 A 授权消息之后的另一条新消息中；两阶段缺一不可。
阶段 B 确认前，任何真实模型/DataTap/钱包操作均禁止；确认后不得超出授权的
Level、预算、工具与 destructive 开关。
```

### 5.3 模式 B 一次性完整授权模板（ONESHOT_FULL_AUTHORIZATION）

```text
REAL B7 UAT 一次性完整授权确认（ONESHOT_FULL_AUTHORIZATION，模式 B）

我授权在以下约束内执行一次真实 B7 UAT round，L0→L1→L2 连续执行，无需第二条授权消息：

- authorization mode: ONESHOT_FULL_AUTHORIZATION（模式 B）
- round_id 身份规则: REAL_B7_<启动门禁通过时点 UTC 秒级时间戳>_<execution commit 前 8 位>
- execution commit 身份规则: 启动门禁通过时点、最终文档纠偏后的 clean HEAD；必须是
  68deca58f2d2cd8aafb96d9ea47a0a60462142fa（已审核 L1 repair baseline）的线性后代，
  且包含 f8a4ffa / 494d20e / c22a3a1 / 8a5f264 / 68deca5 五个修复提交；
  68deca58 至 HEAD 区间只允许已审核的文档纠偏；工作树必须干净；
  禁止 checkout/reset/rebase/amend；不再要求 61576f7..HEAD 为 docs-only
- branch: <分支名>
- migration head: 0043_billing_downgrade_guard
- 启动门禁数据库身份核验（逐项，任一不符即 B7_BLOCKED）:
  - SELECT DATABASE() == kol_insight_b7_uat: <YES>
  - CURRENT_USER() == kol_b7_uat@localhost: <YES>
  - charset == utf8mb4 / collation == utf8mb4_unicode_ci: <YES>
  - 专用账号访问 kol_insight 被 MySQL 1142 拒绝: <YES>
  - 禁止 kol_insight / kol_insight_test / 任何开发、预生产、生产或正式客户数据库: <YES>
  - 禁止 DROP/CREATE/重建该库；禁止普通 pytest / 离线 UAT harness / 迁移 downgrade: <YES>
- 凭证引用（只记引用，值仅进程内，禁止 echo/日志/写文件）:
  - MySQL 密码: macOS Keychain service=com.kol-insight.real-b7-uat.mysql account=kol_b7_uat@127.0.0.1
  - DataTap Token: macOS Keychain service=com.kol-insight.real-b7-uat.datatap account=DATATAP_MCP_TOKEN
  - Runtime master keys: macOS Keychain service=com.kol-insight.real-b7-uat.runtime-secret-master-keys account=v1（active version=v1）
  - 模型配置: 主仓库未跟踪文件 backend/.env（TENCENT_PLAN_BASE_URL / TENCENT_PLAN_MODEL / TENCENT_PLAN_API_KEY，仅进程内）
- L0 授权范围（零模型/DataTap 外发；任一失败即停止）:
  - 本地构建（不连接任何外部环境）: <YES>
  - L0 控制面以 loopback 占位 DATATAP_MCP_ORIGIN 启动（零真实 discovery；真实 discovery 仅属 L1-00）: <YES>
  - 专用库环境初始化（仅经生产 domain/admin service，幂等 + 审计/账本：两个 synthetic
    tenant、每 tenant ≥2 synthetic 用户、TenantWallet 各 2000 积分且 reserved=0、用户周期
    额度 2000、License 含 kol_selection/brand_analysis/campaign_analysis/kol_detail/utility、
    租户级 Pi Runtime Config draft→单独激活；禁止直接写 encrypted_runtime_secrets）: <YES>
  - 专用库只读预检（Runtime Config/License/TenantWallet/quota 元数据，SELECT only）: <YES>
  - 证据目录创建与首帧写入: <YES>
- L1-00 外部调用预检（真实 discovery，仅 negotiation/list-tools；0 ToolCall、0 积分、0 模型请求）: <YES>
- 固定 quarantine 基线确认（insight-cube-mcp / query_user_info /
  digest aa4933db9542bc5802a6a1a31b6dd274ea08e562ca01cf1ef1f1fd2a56afeb49 / quarantined；
  不登记 allowlist、不进入 claim adapter_catalog；digest、行数或状态任何变化即硬停止）: <YES>
- L1 授权范围（单 tenant/单 user/单 Run；失败不得进入 L2；禁止自动重试）:
  - 最终 L1 MCP 工具: <服务 slug / 工具内部名，如 insight-cube-mcp / match_best_tag>
  - 上限: 最多 2 次模型逻辑请求（第一次工具调用、第二次消费结果并完成；第 3 次请求发生前硬停止）、≤1 次 DataTap 外发、≤10 积分: <YES>
- L2 授权范围: L2-01 至 L2-20 全部 20 个场景；核验场景复用已有 Run/Artifact；unknown 禁止自动重放: <YES>
- 允许的 Level: <L1 / L1+L2>
- 预算（13 项，全部必填数值）:
  - 最大 round 数: <n>
  - 最大 Run 数（全 round 合计）: <n>
  - 每场景最大 Run 数: <n>
  - 最大 MCP 外发次数（全 round 合计）: <n>（固定每次 10 积分）
  - 单 Run 最大 MCP 次数: <n>
  - 测试钱包初始余额: <n 积分>
  - 最大允许积分净支出: <n 积分>
  - 用户周期额度: <period 周期> / <points_limit 积分上限>
  - 最大模型请求数: <n>
  - 最大输入 token: <n>
  - 最大输出 token: <n>
  - 最大模型费用: <金额与币种>
  - 最大执行时长: <时长>
- 预算到顶行为（VERIFIED，计划强制，无需填写）: 达到任一上限立即停止整个 round，不自动重试、不进入后续场景
- destructive test-state changes（逐项勾选，未勾选即禁止）:
  - Gateway restart / worker kill: <YES/NO>
  - Gateway draining 置位/复位: <YES/NO>
  - License suspend: <YES/NO>
  - Runtime Config 新版本激活: <YES/NO>
  - kill switch 置位/复位: <YES/NO>
  - current/pi 租户 backend 切换: <YES/NO>
  - 测试钱包余额调整（含余额不足场景置低）: <YES/NO>
  - 测试数据清理（结束后）: <YES/NO>
- 清理/保留策略: <证据目录 append-only 永久保留；数据库证据 retained_by_policy 至独立
  reviewer 封口，清除需用户另行授权>
- evidence directory: docs/qa/evidence/pi-b7/<round_id，按身份规则展开>/
- operator: <执行人>
- independent reviewer: <独立复核人，不得与 operator 同一人>
- 时间窗口: <起止时间，含时区>
- stop conditions: 确认授权包 §3 全部 19 条硬停止条件生效: <YES>
- authorization message reference: THIS_MESSAGE

模式 B 规则：本消息即完整授权面，无第二条授权消息要求；L0 任一失败即停止，L1 失败不得进入
L2；L0 产出值（tenant/user/gateway IDs、Runtime Config/License ID/version、Gateway build
hash、catalog/schema digests）逐字录入证据后视为固定值，任何未授权变化命中 §3-18。
本授权不降低任何预算、隔离、destructive 开关或停止条件要求。文档、Git commit、测试通过或
READY 状态均不构成授权之外的执行许可。
```

## 6. 授权包修复记录（2026-08-12，NEEDS_AUTHORIZATION_PACK_REPAIR 收口）

### 6.1 状态流转

授权包第一版（`f7ab159`）经 2026-08-12 架构复核：Critical 0 / Important 3 / Minor 1，
状态进入 `NEEDS_AUTHORIZATION_PACK_REPAIR`。修复轮第一版为纯文档修复（不执行真实 B7、
不连接任何环境、不创建真实 round、不修改生产代码或迁移），全部 4 项发现关闭。第二轮修复
（§6.5，同为纯文档）：用户选定模式 B 一次性完整授权为本次执行形态，专用隔离环境绑定写入
计划 §2.0；状态进入 `AUTHORIZED_MODE_B_ROUND_NOT_OPENED`。该 round
（`REAL_B7_20260812T045636Z_b801c490`）随后执行：L0 通过、L1 FAIL（
`mcp_tool_identity_invalid`），按规则终止封存；第三轮修复（§6.6）后状态进入
`READY_FOR_REAL_B7_UAT_REAUTHORIZATION`——旧授权已消费，真实 B7 需重新授权。

### 6.2 逐项关闭证据

| 发现 | 级别 | 修复 | 关闭证据 |
| --- | --- | --- | --- |
| I-1 授权流程单阶段：零外发预检与真实调用混在同一授权面；L0 只读边界文案不清 | Important | 计划 §7 重写为两阶段（阶段 A 仅放行本地构建 + 隔离测试库只读预检 + 证据首帧写入；阶段 B 在 L0 通过后由用户另一条新消息确认才放行真实调用）；计划 §6/§6.1 与模板同步；本包 §5 拆为 §5.1/§5.2 | 阶段 B 确认前禁止真实模型/DataTap/钱包操作的硬规则写入计划 §7.2、§5.1/§5.2 模板结尾；L0 文案明确"不调用真实模型/DataTap、不变更钱包/额度、阶段 A 后可只读访问隔离测试库元数据" |
| I-2 round_id 可留空由 operator 回填；授权消息引用未锚定 | Important | 删除模板"留空则回填"例外；计划 §1.1 规定 operator 在授权前提出完整确定 round_id（不建目录、不连环境）；本包 §5 规则：用户填 `THIS_MESSAGE`，operator 只记录平台消息 ID/任务 ID 与授权文本 SHA-256，不得修改原文 | 全文无"授权后回填"通道（`留空`/`回填` 仅以禁止性语义出现）；模板所有字段均须在授权消息中给出确定值 |
| I-3 commit 身份错误：`61576f7` 被写成"当前 HEAD"，执行 commit 未锚定授权时点 | Important | 计划 §0 改标 `61576f7…` 为修复前 production baseline，文档基线 HEAD `f7ab159` 标注为撰写时点历史事实；§1.1/§1.2 规定 execution commit = 授权时点最终修复提交后 `git rev-parse HEAD`、round_id short commit 取自用户批准值、文档不内嵌自指 SHA（2026-08-12 门禁纠偏后锚定已审核 L1 repair baseline `68deca58…`） | 全文无"当前 HEAD = 61576f7"表述；`61576f7` 仅以 baseline 语义出现 |
| M-1 append-only 证据语义不足：JSON 整文件"追加"无法防改写、无校验方法 | Minor | 本包 §2 重设计：8 个追加型文件改 canonical JSONL + `prev_hash`/`record_hash` 链，逐帧 flush/fsync；3 个快照单次写入不可变；`verdict.md`/`hashes.sha256` 仅封口写一次；明文禁止修改/删除/重排/插入既有帧并定义 4 步链式校验；§3-14 覆盖写入失败与链校验失败 | 本地脱机自洽性验证通过（§6.4）：合法链接受，篡改字节/删帧/重排/重序列化均被拒绝 |

### 6.3 待批准字段 → 模板填写位对照

计划中每一个 `NEEDS_USER_APPROVAL` / `NEEDS_USER_INPUT` 字段在两份模板中的填写位：

| 计划字段（来源） | 模板填写位 |
| --- | --- |
| round_id（§1.2） | §5.1 `round_id`；§5.2 复述并绑定同一值 |
| commit_sha（§1.2） | §5.1 `execution commit SHA`；§5.2 复述 |
| Gateway build hash（§1.2） | §5.2 `Gateway build hash`（L0-04 产出） |
| Runtime Config ID/version（§1.2） | §5.2 `Runtime Config ID / version` |
| License ID/version（§1.2） | §5.2 `License ID / version` |
| tenant IDs（§1.2） | §5.2 `tenant IDs` |
| test user IDs（§1.2） | §5.2 `test user IDs` |
| gateway IDs（§1.2） | §5.2 `gateway IDs` |
| evidence directory（§1.2） | §5.1 `evidence directory`；§5.2 复述 |
| operator（§1.2，NEEDS_USER_INPUT） | §5.1 `operator`；§5.2 复述 |
| independent reviewer（§1.2，NEEDS_USER_INPUT） | §5.1 `independent reviewer`；§5.2 复述 |
| start/end time（§1.2） | §5.1 `时间窗口`；§5.2 复述/延长 |
| authorization message reference（§1.2） | §5.1/§5.2 各自 `THIS_MESSAGE` + operator 记录消息 ID 与 SHA-256 |
| 隔离环境 12 项要求（§2） | §5.1 `隔离测试环境标识` / `隔离测试数据库引用` / `隔离要求: <YES>`；§2-12 清理保留部分另由 §5.2 `清理/保留策略` 覆盖 |
| 9 项凭证 × 7 列元数据（§3） | §5.2 凭证元数据 9 行 block（ref/fingerprint/key_version/owner/expiry/host/status） |
| 29 个工具 schema digest（§4.1） | §5.2 `catalog/schema digests`（`catalog-digests.json` 的 SHA-256 锚定全部 29 行） |
| 3 个网络 origin（§4.3） | §5.2 网络 origin block（模型/DataTap/FastAPI 控制面） |
| 13 个预算数值（§5） | §5.2 预算 13 行（固定 10 积分与到顶即停为 VERIFIED 复述，不占填写位） |
| 最终 L1 MCP 工具（§6.2） | §5.2 `最终 L1 MCP 工具` |
| 允许的 Level（§6 授权层级） | §5.2 `允许的 Level: <L1 / L1+L2>` |
| 8 个 destructive 开关（§6 各 `+D(...)` 场景） | §5.2 destructive test-state changes 8 行逐项 `<YES/NO>` |
| 清理/保留策略（§2-12、L2-20） | §5.2 `清理/保留策略` |
| 19 条硬停止条件（授权包 §3） | §5.1 `stop conditions: <YES>` 确认；§5.2 同名字段再次确认 |

### 6.4 修复轮验证

- 模板回填扫描：两份文档不存在"留空后由 operator 回填"的字段（`留空`/`回填` 仅出现在
  禁止性规则文本与历史设计文档引用中）。
- 字段对照：§6.3 逐项覆盖计划全部 `NEEDS_USER_APPROVAL` / `NEEDS_USER_INPUT` 字段。
- JSONL/hash-chain 自洽性：以内存构造 3 帧样例（不创建任何证据目录、不连接任何环境），
  校验器接受；篡改任一字节、删除一帧、重排帧序、非 canonical 重序列化均被拒绝。
- 六类 secret 扫描（本修复轮全部改动文件）：API key/JWT 形态、Bearer 头、私钥块、
  DSN 连接串、secret/password 赋值形态、未脱敏手机号——0 命中。
- `git diff --check` 通过。
- 本轮未运行 pytest/npm/构建/迁移/UAT（纯文档任务）；未启动 Gateway/FastAPI，未连接
  数据库/模型/DataTap/钱包，未创建真实 evidence round，未运行历史 Task 9。

### 6.5 第二轮修复（2026-08-12：授权模式消歧 + 专用环境绑定）

背景：首次真实 B7 执行尝试在启动门禁 fail-closed（B7_BLOCKED：工作树存在两份未提交授权
文档修改）；未开启 round、未连接任何环境、未读取任何凭证。用户随后决定：授权流程支持两种
合法模式，本次执行采用模式 B（一次性完整授权 L0→L1→L2）；把已创建并核验的专用隔离环境
写入授权文档；execution commit 消歧（不写死 `f7ab159`，以最终修复提交后的 clean HEAD 为
候选）。

改动（纯文档，零外部调用）：

- 计划 §7 重写为两种授权模式：模式 A（两阶段，推荐，§7.1/§7.2）与模式 B（一次性完整授权，
  §7.3）；模式互斥；模式 B 不降低预算、隔离、destructive 开关与 19 条硬停止条件；L0 失败
  仍停止、L1 失败不得进入 L2。本包 §5 同步：§5.1/§5.2 归入模式 A，新增 §5.3 模式 B 一次性
  完整授权模板（含启动门禁数据库身份核验、凭证引用、L0/L1/L2 授权范围、13 项预算、8 个
  destructive 开关填写位）。
- 计划 §2.0 新增专用环境绑定（VERIFIED）：数据库 `kol_insight_b7_uat`（host `127.0.0.1:3306`、
  user identity `kol_b7_uat@localhost`、`utf8mb4`/`utf8mb4_unicode_ci`、73 表、migration
  head `0043_billing_downgrade_guard`）、专用账号访问 `kol_insight.users` 被 MySQL 1142
  拒绝的隔离证明、初始状态（tenants=0、encrypted_runtime_secrets=0、2 条系统迁移种子保留）、
  `retained_by_policy` 保留策略；计划 §3 凭证表 reference 列按 Keychain 三引用与
  `backend/.env` 引用核验为 VERIFIED（只记引用不记值）。
- 计划 §6.1 L0 边界澄清：零真实外发与无 Run 级计费不变；专用空库初始化例外只经生产
  domain/admin service 幂等写入并写审计/账本（两 synthetic tenant、用户、各 2000 积分钱包、
  周期额度 2000、全能力 License、租户级 Pi Runtime Config draft→激活），禁止直接写
  `encrypted_runtime_secrets`；新增 L0-00 初始化核验项。
- execution commit 消歧：`f7ab159` 仅为第一版文档基线历史事实；execution commit 候选为
  本修复提交后的 clean HEAD，round_id 用其前 8 位（计划 §0/§1.1/§1.2）；文档不内嵌自指 SHA。
- 证据规范：模式 B 专用 `authorization.md`（含授权文本全文、实际 execution HEAD、数据库
  身份与 Keychain reference、消息 ID 与 SHA-256）写入规则加入 §2.1/§2.2。

验证（本轮全部改动文件）：

- grep 核验：`kol_insight_b7_uat` 与 `kol_b7_uat` 存在于计划/授权包/runbook/changelog；
  禁止 `kol_insight`/`kol_insight_test` 条款存在；两种授权模式定义无冲突；模式 B 无
  "第二条授权消息"阻断。
- 六类 secret 扫描：API key/JWT 形态、Bearer 头、私钥块、DSN 连接串、secret/password
  赋值形态、未脱敏手机号——0 命中。
- `git diff --check` 通过；提交前 `git diff --cached --check` 与提交后 `git show --check`
  通过；提交后工作树干净。
- 本轮未运行 pytest/npm/构建/迁移/UAT；未启动 Gateway/FastAPI；未连接数据库/模型/DataTap；
  未读取 Keychain 明文；未创建 round。

### 6.6 第三轮修复（2026-08-12：L1 失败根因修复 + 流程修订）

背景：模式 B round `REAL_B7_20260812T045636Z_b801c490` 执行——启动门禁与 L0 全部通过，
L1-SMOKE 中真实模型经通用 `mcp` 代理工具以裸 remote 名（`match_best_tag`）寻址，计费扩展的
代理分支只接受 prefixed 名（`insight_cube_mcp_match_best_tag`），必然本地拦截
`mcp_tool_identity_invalid`：0 外发、0 扣费、0 ToolCall，账务恒等式成立，19 条硬停止无一
触发。按「L1 失败不得进入 L2」终止并封存。该 round 已经架构独立复核确认为 **B7_FAIL**；
其证据目录因 operator 曾提前写入 `hashes.sha256` 而保持只读历史现场——不再向该目录补写
`verdict.md` 或任何文件，禁止修改、删除或覆盖。离线 UAT 此前未暴露该分歧：fake model 脚本
只发 prefixed 名。

代码与测试修复（修复分支 `codex/real-b7-l1-repair`，本轮不执行真实 B7）：

- `pi-gateway/src/mcp-accounting-extension.ts`：通用代理分支改为接受全部已审核寻址形式——
  catalog 内部名（= adapter_visible_name）、裸 remote 名、旧 prefixed 名；无 server 时按
  bindings 全局唯一性推导 server；重名且无 server 本地 fail-closed
  `mcp_tool_identity_ambiguous`；未知名 `mcp_tool_identity_invalid`；两者均 0 preflight、
  0 外发；禁止对重名候选取第一个；计费身份恒为 catalog 内部名。修正「proxy 只暴露 prefixed
  名」的错误注释。
- `pi-gateway/src/resource-loader.ts`：`.mcp.json` 增加 `toolPrefix: "none"`——adapter 的
  模型可见名与 claim 投影的裸名对齐（此前默认前缀模式下裸名在 adapter 分发面
  `tool_not_found`）；prefixed 旧名仍由扩展映射身份，但分发面不再接受，fail-closed
  释放不计费。
- 离线 UAT（`backend/tests/integration/`）：fake model 增加真实通用代理形状
  （`step_mcp_proxy`，裸 remote 名、server 可选）；新增 5 个进程级场景——真实 L1 路径复现
  （裸名全链路：durable preflight → fake 外发 → finalize → Evidence → ToolCall → 10 积分）、
  无 server 唯一映射、重名 fail-closed、重名+显式 server 精确映射、旧 prefixed 名安全失败
  不计费；红灯复现（修复前扩展 → 0 外发断言失败）与绿灯证据齐备。
- `backend/scripts/b7_evidence.py`（新增，已跟踪）：可复用 B7 证据生成器——canonical JSONL
  hash chain（逐帧 flush/fsync、sequence/prev_hash/record_hash、外部链头锚点校验）、strict
  pydantic DTO（terminal 封闭集合、run.started 不计入、error_code 拒绝模型名、账务恒等式
  本地校验）、命名 ORM 属性 builder（禁止 positional 拼装）、跨文件一致性校验、correction
  帧追加；`backend/tests/scripts/test_b7_evidence.py` 16 项单测。

授权/封口流程修订（本包与计划）：

- 计划 §2.1 固定 quarantine 基线（`insight-cube-mcp`/`query_user_info` + 精确 digest），
  新授权消息必须显式确认，任何变化即硬停止（§3-3）。
- 计划 §2.2/§6.1/§6.2：L0 保持零真实模型/DataTap 请求（控制面以 loopback 占位 origin 启动）；
  真实 DataTap discovery 移到新增的 **L1-00 外部调用预检**（仅 negotiation/list-tools，
  0 ToolCall、0 积分、0 模型请求）。
- 计划 §6.2 与本包 §3-13/§5.3：L1 模型逻辑请求上限明确为最多 2 次（第一次工具调用、第二次
  消费结果并完成；第 3 次请求发生前硬停止；禁止 SDK/业务自动重试）。
- 封口角色分离（本包 §2、计划 §7.4）：operator 只能追加 `execution_completed`/
  `execution_stopped` 帧与 `operator-summary.md`；`round_sealed` 帧、`verdict.md`、
  `hashes.sha256` 只由 independent reviewer 写入；历史失败 round 禁止补写或覆盖。

验证：见 `changelog/2026-08-12.md` 修复轮条目（vitest/typecheck/build/pytest/ruff/git checks
全绿，全程离线）。
