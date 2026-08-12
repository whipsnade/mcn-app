# REAL B7 UAT 授权计划（方案 B）

> 本计划只定义真实 B7 UAT 的授权边界、执行前提与场景矩阵；不执行任何真实外部调用。
> 证据目录设计、硬停止条件与授权文本模板见配套授权包
> `docs/qa/2026-08-12-pi-b7-uat-authorization-pack.md`。

```text
Status: AWAITING_USER_AUTHORIZATION
Real external calls authorized: NO
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
| 当前 HEAD | `61576f7a45c3a93063bdaae5328aefd67933df68` | `git rev-parse HEAD` |
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
| 架构复核结论 | Critical 0 / Important 0 / Minor 1（2026-08-12） | 用户任务书事实陈述 |
| 离线进程级 UAT | 17 场景 × 串行 3 轮全绿（fake model + fake DataTap MCP，0 外部网络） | `changelog/2026-08-11.md`、`docs/qa/pi-agent-gateway-local-uat.md` |
| 当前交付状态 | `READY_FOR_REAL_B7_UAT_REVIEW` | `docs/qa/pi-agent-gateway-local-uat.md`、`docs/runbooks/pi-agent-gateway.md` |

**事实判读：** 本地离线 fake topology 通过 ≠ 真实 B7 通过；`READY_FOR_REAL_B7_UAT_REVIEW`
≠ `READY` 被确认，更不等于 B7 PASS 或 production ready。真实 B7 UAT 必须取得用户新的明确授权。

## 1. Round 身份

### 1.1 格式

```text
REAL_B7_<YYYYMMDDTHHMMSSZ>_<SHORT_COMMIT>
```

- `<YYYYMMDDTHHMMSSZ>`：round 创建（授权生效）时刻的 UTC 秒级时间戳，由执行现场生成。
- `<SHORT_COMMIT>`：授权 commit SHA 的前 8 位小写十六进制（与 `git rev-parse --short=8` 一致）。
- round_id 一经写入证据目录 `authorization.md` 即不可变；任何身份字段变化必须终止当前 round
  并重新取得授权、新建 round。
- **本轮不创建任何真实 round**：当前不存在合法 round_id，任何形如 `REAL_B7_*` 的目录在本授权前
  均不得创建。

### 1.2 未来执行时必须固定的字段

| 字段 | 当前值/来源 | 状态 |
| --- | --- | --- |
| round_id | 执行现场按 §1.1 生成 | NEEDS_USER_APPROVAL |
| commit_sha | 候选 `61576f7a45c3a93063bdaae5328aefd67933df68`（当前 HEAD，VERIFIED）；实际执行 commit 以授权文本为准 | NEEDS_USER_APPROVAL |
| branch | `codex/marketing-capability-pack-b0` | VERIFIED |
| migration_head | `0043_billing_downgrade_guard` | VERIFIED |
| Pi SDK 版本 | `pi-coding-agent 0.79.10` / `pi-ai 0.74.2` / `pi-tui 0.74.2` | VERIFIED |
| adapter 版本 | `pi-mcp-adapter 2.20.1` | VERIFIED |
| Gateway build hash | 执行时从授权 commit 全新 `npm ci && npm run build` 后记录 dist 摘要 | NEEDS_USER_APPROVAL |
| Runtime Config ID/version | 测试环境独立 append-only 版本，执行前由用户确认 | NEEDS_USER_APPROVAL |
| License ID/version | 测试环境独立 License，执行前由用户确认 | NEEDS_USER_APPROVAL |
| tenant IDs | 至少两个独立测试租户 | NEEDS_USER_APPROVAL |
| test user IDs | 每租户至少两个独立测试用户 | NEEDS_USER_APPROVAL |
| gateway IDs | 测试 Gateway 固定 id 白名单值 | NEEDS_USER_APPROVAL |
| evidence directory | `docs/qa/evidence/pi-b7/<round_id>/`（结构见授权包 §2） | NEEDS_USER_APPROVAL |
| operator | 执行人 | NEEDS_USER_INPUT |
| independent reviewer | 独立复核人（不得与 operator 同一人） | NEEDS_USER_INPUT |
| start/end time | 授权时间窗口 | NEEDS_USER_APPROVAL |
| authorization message reference | 用户完整确认授权文本的消息引用 | NEEDS_USER_APPROVAL |

## 2. 隔离环境要求（必须由用户逐项确认）

以下每一项均为 `NEEDS_USER_APPROVAL`；任一项不能确认即不得进入 Level 1/2：

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

以下字段全部 `NEEDS_USER_APPROVAL`；任何情况下不得把凭证值写入 Markdown、Git、日志或命令行输出。
「masked fingerprint」只允许 `••••` + 末 4 位或等效掩码形式。

| 凭证变量 | secret store reference | masked fingerprint | key version | owner | rotation/expiry | execution host | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `TENCENT_PLAN_BASE_URL` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `TENCENT_PLAN_MODEL` | NEEDS_USER_APPROVAL | NOT_APPLICABLE（非密钥，模型名） | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `TENCENT_PLAN_API_KEY` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `DATATAP_MCP_ORIGIN` | NEEDS_USER_APPROVAL | NOT_APPLICABLE（非密钥，origin） | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `DATATAP_MCP_TOKEN` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `PI_GATEWAY_INTERNAL_SECRET` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `RUNTIME_SECRET_MASTER_KEYS` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| `JWT_SECRET` | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |
| MySQL credential reference | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL | NEEDS_USER_APPROVAL |

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
| 允许 | 模型端点 origin（`TENCENT_PLAN_BASE_URL` 的授权取值） | NEEDS_USER_APPROVAL |
| 允许 | DataTap MCP origin（默认 `https://datatap.deepminer.com.cn`，VERIFIED 为代码默认值；测试环境实际取值以授权 Runtime Config 的 `datatap_urls` 为准） | NEEDS_USER_APPROVAL |
| 允许 | FastAPI 控制面 origin（`PI_GATEWAY_CONTROL_PLANE_URL`；production 必须 HTTPS，仅 development/test 允许 loopback HTTP） | NEEDS_USER_APPROVAL |
| 禁止 | 上述之外的一切主机：未登记供应商、对象存储、遥测/分析端点、模型自行决定的任意 URL、Pi 内建工具可达的本地资源与 shell | VERIFIED（禁止规则本身为代码强制：Worker `noTools:"builtin"`、adapter `hostConfigDiscovery:"off"`、ResourceLoader 全量关闭自动发现） |

digest 漂移处置：catalog/schema digest 与授权记录不一致时，registry 自动隔离该工具；
执行现场必须立即停止整个 round，不得在 round 中审批或豁免新工具。

## 5. 预算授权表

以下金额/上限全部由用户填写并批准；本计划不预填任何数值。固定项已标注 VERIFIED。

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

## 6. 场景矩阵

授权层级：`L0`（零外部调用预检，不授权任何真实连接）→ `L1`（最小真实冒烟，需独立批准）→
`L2`（完整真实 B7，需独立批准）。标记 `+D(...)` 的场景包含 destructive test-state change，
必须由用户在授权文本中逐项显式开启，不能由通用授权隐含允许。

所有场景的预算上限均 `NEEDS_USER_APPROVAL`，且不得超出 §5 全局预算；停止条件均为
「命中授权包 §3 任一硬停止条件即终止整个 round」，下文不再逐场景重复全局条件，只列
场景特有停止条件。

### 6.1 Level 0：零外部调用预检（12 项）

L0 不发起任何真实模型/DataTap/钱包连接；数据库只读核对仅限测试环境。任一失败即停止，
不进入 L1。

| ID | 预检项 | 通过判据 | 状态 |
| --- | --- | --- | --- |
| L0-01 | Git commit/branch 核对 | 与授权文本一致、工作树干净 | NEEDS_USER_APPROVAL |
| L0-02 | migration head 核对 | `alembic heads` 唯一且为 `0043_billing_downgrade_guard` | NEEDS_USER_APPROVAL |
| L0-03 | 依赖版本核对 | `npm ls --depth=0` 与锁定版本一致；后端依赖与授权记录一致 | NEEDS_USER_APPROVAL |
| L0-04 | Gateway build 核对 | 从授权 commit 全新构建，dist 摘要落 evidence | NEEDS_USER_APPROVAL |
| L0-05 | Runtime Config/License 形状核对 | append-only、scope/status/版本号符合 §2；只读查询 | NEEDS_USER_APPROVAL |
| L0-06 | catalog/schema digest 核对 | 测试环境目录全部 approved+enabled，digest 落 `catalog-digests.json` | NEEDS_USER_APPROVAL |
| L0-07 | 租户/用户隔离配置核对 | ≥2 测试租户、每租户 ≥2 测试用户、membership 形状正确 | NEEDS_USER_APPROVAL |
| L0-08 | 钱包/额度配置形状核对 | 测试钱包初始余额、预留=0、用户周期额度形状；只读 | NEEDS_USER_APPROVAL |
| L0-09 | kill switch/current 回滚准备核对 | 配置可置位、回滚路径演练脚本就绪（不执行真实切换） | NEEDS_USER_APPROVAL |
| L0-10 | append-only evidence 目录可写核对 | 创建 `<round_id>/` 并写入 `authorization.md`/`manifest.json` 首帧 | NEEDS_USER_APPROVAL |
| L0-11 | 日志脱敏检查 | 抽查启动日志无 token/key/DSN/手机号明文 | NEEDS_USER_APPROVAL |
| L0-12 | 网络出口核对 | 进程外发目标仅 §4.3 允许主机（静态配置核对，不发真实请求） | NEEDS_USER_APPROVAL |

### 6.2 Level 1：最小真实冒烟（1 个场景，需独立批准）

| 字段 | 内容 |
| --- | --- |
| 场景 ID | L1-SMOKE |
| 形状 | 单租户、单用户、单 Run、单个已审核只读 MCP 工具、一次外发、固定最多 10 积分 |
| 候选工具 | `match_best_tag`（insight-cube-mcp）；最终工具选择 NEEDS_USER_APPROVAL |
| 真实模型 / 真实 DataTap | 是 / 是（各一次） |
| 验证点 | durable preflight 提交 → adapter 外发 → finalize 全链路；Evidence、租户账本（恰好 −10）、usage 记录、审计行齐备 |
| 禁止事项 | 禁止自动重试；失败立即停止，不进入 L2 |
| 预期 terminal | `completed`（或稳定分类的失败——仍判 L1 不通过并停止） |
| 回滚动作 | 无需状态回滚；证据保留 |
| 授权级别 | L1（需用户在授权文本中单独开启） |

### 6.3 Level 2：完整真实 B7（20 个场景）

预算列统一为 `NEEDS_USER_APPROVAL`（最大 Run/MCP/token/积分，且 ≤ §5 全局预算）。

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

## 7. 授权流程

1. 用户阅读本计划与配套授权包，逐字段填写授权文本模板（授权包 §5）并在**新消息**中完整确认。
2. operator 按 §1.1 生成 round_id，创建证据目录并写入 `authorization.md` 首帧。
3. 按 L0 → L1 → L2 顺序执行；L1 失败不得进入 L2；任一层级命中硬停止条件即终止整个 round。
4. 每个场景结束即追加证据；round 结束由 independent reviewer 出具 `verdict.md`
   （B7_PASS / B7_FAIL / B7_BLOCKED）。
5. 停止或失败后：保留 append-only 证据、不覆盖不删除失败 round、不在同一 round 修代码、
   不自动新建下一 round；修复与新授权是下一轮的前提。
