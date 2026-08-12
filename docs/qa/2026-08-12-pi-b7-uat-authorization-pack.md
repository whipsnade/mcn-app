# Pi Agent Gateway 真实 B7 UAT 授权包

日期：2026-08-12
配套授权计划：`docs/superpowers/plans/2026-08-12-real-b7-uat-authorization-plan.md`
（round 身份、隔离环境、凭证引用、工具/网络 allowlist、预算授权表、Level 0/1/2 场景矩阵）。

```text
Status: AWAITING_USER_AUTHORIZATION
Real external calls authorized: NO
Production cutover authorized: NO
Historical Task 9 rerun authorized: NO
Plan C authorized: NO
```

本授权包只做授权方案设计：不执行真实 B7 UAT，不连接真实模型、DataTap、钱包、预生产或生产
环境，不创建真实 round 目录。历史 Pi RPC/POC 真实六场景 Task 9（round `20260808T060814Z`）
永为 `EVALUATED_FAIL`，不在本包任何授权范围内。

## 1. 授权包范围

- 本包定义：append-only 证据设计（§2）、硬停止条件（§3）、自审结果（§4）、授权文本模板（§5）。
- 场景、预算、环境、凭证与 allowlist 的字段状态以授权计划为准；两文档冲突时以更严格者为准。
- 字段状态只允许 `VERIFIED` / `NEEDS_USER_INPUT` / `NEEDS_USER_APPROVAL` / `NOT_APPLICABLE`。

## 2. append-only 证据设计

### 2.1 目录结构（仅设计，本轮不创建真实 round 目录）

```text
docs/qa/evidence/pi-b7/<round_id>/
  authorization.md            # 用户授权文本全文 + round_id + 授权消息引用（首帧，写入后不可变）
  manifest.json               # 全局与逐场景清单（字段见 §2.2，append-only）
  environment.json            # 环境形状：OS/运行时版本、masked origin、隔离声明核对结果
  dependency-versions.json    # npm ls --depth=0、后端依赖快照、Gateway dist build hash
  catalog-digests.json        # Level 0 读取的目录行：internal_name/service/digest/review_status
  scenario-results.json       # 逐场景结果：terminal、外发计数、钱包前后、判定
  run-identities.json         # Session/Run/Attempt/Step/ToolCall/Event source id 对照
  accounting-summary.json     # 账本逐笔 ID、reserve/settle/release、净支出恒等式核算
  usage-reconciliation.json   # RuntimeUsageRecord 与模型请求对账
  artifact-lineage.json       # Artifact/Version/Evidence/导出文件血缘与 SHA-256
  event-ordering.json         # SSE 序号、message.completed 与终态顺序、续传检查
  security-scan.txt           # secret/脱敏扫描结果（只含命中位置与变量名，不含值）
  process-cleanup.txt         # 进程/端口/租约/nonce/测试数据收口记录
  verdict.md                  # 独立 reviewer 判定：B7_PASS / B7_FAIL / B7_BLOCKED + 理由
  hashes.sha256               # 上述全部文件的 SHA-256（自身除外），封口用
```

规则：

- 目录与文件 append-only：只允许追加，禁止改写、删除、重排已写入内容；`hashes.sha256` 在
  round 封口时一次性写入。
- 每个场景结束立即追加对应记录；`manifest.json` 无法追加本身是硬停止条件（§3-14）。
- 失败 round 永久保留，不得覆盖或删除；修复后另起新 round。

### 2.2 manifest.json 必须记录的字段

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

### 2.3 禁止进入证据的内容

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
3. catalog/schema digest 漂移（含新工具出现、已审核工具签名变化）。
4. 出现跨租户数据、事件、Artifact 或账务记录。
5. 同一逻辑 MCP 调用重复外发。
6. 未完成 durable preflight 就发生外发。
7. 钱包净支出与已确认外发次数 × 10 不一致。
8. `unknown` 被自动重放。
9. `message.completed` 与 terminal 顺序错误。
10. 出现多个 terminal。
11. Artifact/Version/Evidence lineage 不一致。
12. Excel 和 BI 未绑定同一 Version。
13. 预算任一上限达到（授权计划 §5 任一字段）。
14. evidence manifest 无法追加。
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

## 4. 自审结果（2026-08-12，文档落库前逐项核对）

1. [x] 所有真实调用字段均为 `NEEDS_USER_APPROVAL`（或明确标注 `NEEDS_USER_INPUT` 的人名类字段）。
2. [x] 两份文档均无任何明文凭证（只出现变量名与引用占位）。
3. [x] 没有把本地离线 fake topology 写成真实 B7。
4. [x] 没有把 `READY_FOR_REAL_B7_UAT_REVIEW` 写成 PASS（全文无 B7 PASS 表述）。
5. [x] 没有授权历史真实 Task 9（round `20260808T060814Z` 永为 `EVALUATED_FAIL`）。
6. [x] 没有授权生产切流。
7. [x] 没有授权方案 C。
8. [x] 每个外部调用场景都有预算字段（`NEEDS_USER_APPROVAL`）与停止条件。
9. [x] 每个状态变更场景都有显式授权位（`+D(...)` 逐项开关，见授权文本模板）。
10. [x] 每个证据字段都有脱敏规则（§2.2/§2.3）。
11. [x] 所有失败均保留 append-only round（§2.1/§3 停止后行为）。
12. [x] 本授权包本身不触发任何外部连接（纯 Markdown，无可执行内容）。

## 5. 授权文本模板

> 供用户未来复制、填写并在**新消息**中完整确认。任何留空项视为未授权。

```text
REAL B7 UAT 授权确认

我授权在以下约束内执行一次真实 B7 UAT round：

- round_id: <REAL_B7_YYYYMMDDTHHMMSSZ_short8>（留空则由 operator 在授权生效时按格式生成并回填引用）
- commit SHA: <完整 40 位>
- branch: <分支名>
- 环境: <独立 B7 测试环境标识；确认与开发/预生产/生产隔离>
- migration head: <授权时 alembic heads 实际值>
- tenant IDs: <至少两个独立测试租户 ID>
- test user IDs: <每租户至少两个测试用户 ID>
- 测试钱包初始余额 / 用户周期额度: <积分值 / period+limit>
- 最大 round 数: <n>；最大 Run 数: <n>；每场景最大 Run 数: <n>
- 最大 MCP 外发次数: <n>；单 Run 最大 MCP 次数: <n>（固定每次 10 积分）
- 最大允许积分净支出: <n>
- 最大模型请求数: <n>；最大输入 token: <n>；最大输出 token: <n>；最大模型费用: <n>
- 最大执行时长: <时长>
- 允许的 Level: <L0 / L0+L1 / L0+L1+L2>
- 允许的 destructive test-state changes（逐项勾选，未勾选即禁止）:
  - Gateway restart / worker kill: <YES/NO>
  - Gateway draining 置位/复位: <YES/NO>
  - License suspend: <YES/NO>
  - Runtime Config 新版本激活: <YES/NO>
  - kill switch 置位/复位: <YES/NO>
  - current/pi 租户 backend 切换: <YES/NO>
  - 测试钱包余额调整（含余额不足场景置低）: <YES/NO>
  - 测试数据清理（结束后）: <YES/NO，附保留/清理策略>
- evidence directory: docs/qa/evidence/pi-b7/<round_id>/
- operator: <执行人>
- independent reviewer: <独立复核人，不得与 operator 同一人>
- 时间窗口: <起止时间，含时区>
- stop conditions: 确认授权包 §3 全部 19 条硬停止条件生效：<YES>
- authorization message reference: <本确认消息的定位引用>

只有用户在新的消息中完整确认本授权文本后，才允许执行。
本文档、Git commit、测试通过或 READY 状态均不构成真实外部调用授权。
```
