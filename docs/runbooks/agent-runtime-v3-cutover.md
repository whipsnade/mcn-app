# Agent Runtime v3 一次性切换清单（Cutover Checklist）

状态：**待发布**（2026-08-03，Task 27）
适用范围：从旧多链路模型架构一次性切换到模型主导的统一 Agent 运行时（设计
`docs/superpowers/specs/2026-08-02-model-led-agent-runtime-design.md` §18/§19；实施计划
`docs/superpowers/plans/2026-08-02-model-led-agent-runtime.md` Task 26/27）。

这是一次**一次性开关（one-shot switch）**：不设新旧运行时功能开关，不允许部分用户继续
使用旧模型链路；新前端与新后端必须在**同一发布批次**完成契约切换。旧执行表**首次切换不
物理删除**，保留用于回滚；只有稳定运行并经用户单独批准后，才可另立清理迁移删除旧表。

> 当前发布批次覆盖（2026-08-21）：本文中按阶段记录的 `0027–0036`、`0043` 等迁移编号和
> 旧 Gate 状态保留为历史审计记录；当前发布树使用唯一 Alembic head
> `0049_skill_rollout_history`。预发布/生产操作必须以当前发布树执行 `alembic upgrade head`，
> 不得按历史段落停在旧 head；新 Pi Runtime 的通用完成契约及历史 Snapshot 兼容边界以
> `docs/runbooks/pi-agent-gateway.md` 的 2026-08-21 节为准。

---

## 0. 状态摘要

| 检查项 | 状态 |
|---|---|
| 代码与测试（迁移 0027–0036 + 运行时 + 前端 + E2E） | ✅ Gate D 非视觉自动化已通过；最终矩阵见 §5.10 |
| 真实模型 + 真实 DataTap UAT（Gate E） | ⛔ 2026-08-07 仅完成澄清/品牌与活动首轮；活动 child Run 被供应商重连中断，**不得视为通过**（见 §5.10） |
| 生产切档 | ⛔ **禁止**——阻断项未解决前不得执行 §3 步骤 |

---

## 1. UAT 发现的发布阻断项（必须先解决）

来源：`docs/qa/2026-08-02-agent-runtime-uat.md`「未决问题 / 切换阻断项」。
任一未解决，**不得执行切档**。

### 阻断项 1：真实 DataTap 传输层无可靠超时/取消（Incident #8）—— ✅ 已修复（运行时墙钟收口），待真实 UAT 复核

- **现象**：真实品牌/活动/圈选场景中，DataTap 某些统计查询长时间持续返回数据，`transport.call_tool`
  挂起数十分钟；`asyncio.wait_for` 无法打断 httpx C 层阻塞（`PossiblySentTimeout` 分类正确但取消不生效）。
  一个慢查询即可让整个 Run 挂死，是真实 UAT 场景 2/3/4 无法跑完的直接原因。
- **判定标准**：同一服务同一工具的长查询必须在受控超时窗口内以 `result_unknown` 分类收口、
  保持预留并让 Run 继续其他工具；已发出但未知的请求经恢复核对，绝不重复扣费或重复请求。
- **修复（2026-08-04，B8）**：`DataTapTransport` 新增外发阶段墙钟上限 `call_timeout_seconds`，
  Agent 传输（`get_agent_mcp_transport`）经配置项 **`AGENT_MCP_CALL_TIMEOUT_SECONDS`**（默认 150s；
  须小于 `AGENT_TOOL_CALL_STUCK_SECONDS`）启用，legacy 传输缺省不启用、行为不变。
  - 机制：拿到 per-service 队列许可后开始计时（队列等待不耗预算）；超时即取消底层任务，
    宽限 `cancel_grace_seconds`（默认 5s）内等待其真正退出——不用 `asyncio.wait_for`
    （它超时后会无限期等待被取消任务退出，底层不可取消时依旧挂死）；宽限过后仍不死则
    **隔离悬挂任务**（保留引用防 GC、完成时吞噬异常并记录 warning），运行时侧按时收口。
  - 收口语义：超时抛 `PossiblySentTimeout`（可能已发送），`AgentMcpTool` 按既有分类落
    `result_unknown`——保留 10 积分预留、写 `agent_tool_calls.status="unknown"`、
    计细粒度熔断失败（同参数反复超时会被熔断）、Run 继续后续工具；恢复循环 stuck 扫描与
    unknown 只读核对对该类行照常生效，核对确认后 settle/release 幂等、绝不重复扣费或重放。
  - 调查结论：挂起发生在 MCP SDK `streamable_http_client` + `ClientSession.call_tool`
    的流式等待层；httpx `read_timeout` 是"无活动"超时，trickle 数据持续到达时被不断
    重置，故 60s 读超时永不触发。
  - 测试：`tests/mcp_gateway/test_call_timeout.py`（挂起 trickle/吞取消顽固层/快调用/
    队列等待不计预算/分类保持/缺省不启用）、`tests/agent_runtime/tools/test_mcp.py` §8
    （真实传输挂起 → unknown 收口、熔断计数、核对只读不重放 + 结算/释放幂等）。
    **仍待真实 UAT 复跑（`scripts/run_real_agent_uat.sh`）复核场景 2/3/4。**

### 阻断项 2：真实模型无法在 Attempt 预算内可靠产出 lineage 有效正式 Artifact —— 必须解决后才能切档

- **现象**：真实模型能驱动 MCP 抓数，但会反复修订 Draft lineage（probe 观察 brand run
  45 决策 / 17 次 revision 仍未过审）并触发 Attempt 保护（50 决策 / 30 分钟）暂停。这与
  `prompts.py`「完整 prompt 工程在后续任务完成」一致：schema 注入、evidence 映射、
  builder 工具化指引不完整，正式 Artifact 交付不可靠。
- **判定标准**：在单个 Attempt 预算（50 决策 / 30 分钟）内，模型能够提交并让 Reviewer
  approve 一个 lineage 完整的正式 Artifact（`validate_and_freeze_lineage` 通过、`lineage_ok=True`）；
  多次独立运行不依赖放宽 Attempt 上限。
- **修复方向**：补齐 Artifact 构建指引（schema 注入、evidence 映射、builder 工具化）。
  **未修复**。

### 其余 UAT 缺口（切档前应一并关闭）

- **kol_detail_v1 Profile 未允许 `MCP_TOOLS`**：生产接线无法触达真实 `kol_detail` MCP 工具，
  达人详情真实 fetch 链路当前不可用（缓存链路可用）。需修复 Profile 或工具分类。
- **生产引擎静态工具注册缺失**：`app/main.py` 的 `create_agent_runtime` 只注入 MCP 目录工具，
  未注册 calculation/history/artifact 静态工具（UAT 测试自带完整注册表）——不补齐则生产
  Run 无法产出正式 Artifact。修复后应复用 `agent_runtime/tools/registry.py` 的 `register()`。
- **`test_real_providers.py::test_real_tencent_adapter_uses_confirmed_model`** 与本环境不符
  （断言 `deepseek-v4-pro`，`backend/.env` 为 `glm-5.2`）——历史遗留，非本次引入。

### Gate 口径说明：DataTap 供应商 SLA 观察项（与产品代码 Gate 区分）

- `kol_detail` 与趋势类端点（`social_statistic_trend` 等）在供应商侧的稳定性问题
  （如外发 >150s 触发墙钟收口、供应商 5xx/超时）**单独列为供应商 SLA 观察项**，UAT
  记录中以 `DATATAP_SLA` 字样标注（逐轮记录见 `docs/qa/agent-runtime-uat-rounds.md`），
  **不计入产品代码 Gate 阻断项**——这类失败升级给 DataTap 渠道跟进，不阻塞 §2 的
  切档判定；产品 bug（错误参数、契约违反、计费/证据错误）仍按 Gate 处理。
- 产品侧策略不变：Agent 传输 150s 外发墙钟（`AGENT_MCP_CALL_TIMEOUT_SECONDS`）超时按
  `result_unknown` 收口、保留 10 积分预留、Run 继续后续工具；**unknown 绝不自动重放**，
  由恢复循环只读核对后幂等 settle/release。

---

## 2. 发布阻断条件（设计 §19）

出现任一情况**不得切换**：

1. MCP 调用可能重复执行、重复扣费或错误释放 unknown 预留；
2. 正式 Artifact 存在无法追溯到当前 Session Evidence 的数值；
3. 跨用户 Session、Evidence、Artifact 或达人详情越权；
4. 任一强类型 Artifact 无法被对应 BI 消费，或声明支持 Excel 的 Artifact 无法导出；
5. Run 恢复导致步骤重放、当前 Attempt 保护计数未重置或新消息复用旧执行卡；
6. 新 Run 写入 Reviewer Driver/Review Batch/Item/Attempt，或多 Artifact 的逐项发布/部分完成聚合失真；
7. 四个旧快捷入口、API 或缓存仍可从新系统触达；
8. 前后端无法在同一发布批次完成契约切换。

## 3. 切档执行步骤

> 前置：§1 两个阻断项已解决并复跑真实 UAT 通过；设计 §19 阻断条件逐条核实无命中。

1. **测试库迁移**：在独立测试库（`kol_insight_test`）执行
   `cd backend && APP_ENV=test .venv/bin/alembic upgrade head`，确认到唯一 head
   `0036_export_claim_token`（0027–0036 顺序应用）；
   全量 pytest（含 `test_legacy_routes_removed.py` 的旧路由 404 断言）通过。
2. **生产备份**：切换前对生产库执行完整备份（含全部旧表——它们要在回滚时恢复读取），
   并记录备份文件路径与时间戳；同时备份 `/home/kol_insight/` 下的 `backend/.env`。
3. **同一发布批次部署新后端 + 新前端**：同步 `backend/` 代码到 UAT/生产
   （不覆盖远端 `.env`），执行 `alembic upgrade head`（只新增新表）；构建 `dist/` 并同步前端。
   重启 `systemctl restart kol-insight.service`。
4. **路由冒烟**：确认旧执行入口不可达——`/api/v1/quick/*`、`/api/v1/sessions/{id}/brainstorm`、
   `/api/v1/sessions/{id}/tasks`、手动 `/kol-analysis` 均返回 404；新 `/api/v1/agent/*` 可用；
   `GET /healthz` 返回 ok，`GET /api/v1/agent/sessions` 公网期望 401。
5. **功能冒烟**：真实账号完成 会话 → 澄清 → 品牌/活动/圈选 → 直接发布 → BI 展示 →
   达人详情 → 品牌/活动/圈选三类 Excel 导出的冒烟；确认三个 BI Tab / 两个达人子 Tab
   仅显示更新、不自动跳转，快捷四入口消失。
6. **积分抽查**：核对每笔 settled DataTap 调用 `points_settled == 10`、`points_reserved == 0`，
   与 `wallet_ledger` 一致；制造一个 504/超时验证 `result_unknown` 保持预留且不重放；
   `unknown` 经恢复核对或管理员 reconcile 后正确结算/释放。
7. **发布闸门复核**：对照 §2 逐条记录通过依据。
8. **回滚预案确认**：首次切换**不 drop 旧表**；冒烟失败时按 §4 回滚应用版本，旧数据未删除
   可恢复旧系统。

## 4. 回滚应用版本

- 冒烟失败或命中任一发布阻断条件时：关闭新任务 → 回滚后端代码与前端 `dist/` 到上一版本 →
  重启服务（旧执行路由随之恢复）→ 旧 Agent 新表保留用于排障但不再写入 → 运行只读健康检查
  与 focused 回归（租约、积分、版本门控）→ 开放任务。
- 数据库迁移只按 Alembic 的可逆 downgrade 执行；**不要**手工删除账本、调用记录、Evidence、
  Artifact 或会话历史。新表未被写入时可直接整体回滚 v3 迁移链——
  `alembic downgrade 0026_brand_report_v2_payload`（逐版本回滚 0029 → 0028 → 0027，
  回到 v3 之前的 0026）；已写入则保留新表、仅回滚应用版本，
  由后续排障决定清理。
- 稳定运行并经用户单独批准后，才可另立**清理迁移**物理删除旧会话、任务、Goal、报告、旧 Artifact、
  旧 MCP/Quick 状态表。清理前必须再次备份并列出准确表名，**不得**在首次切换迁移中隐式删除。

## 5. 直接发布 Runtime 修订（Gate A，迁移 0030）

来源：设计 `docs/superpowers/specs/2026-08-05-marketing-report-runtime-revision-design.md`
§四/§十；实施计划 `docs/superpowers/plans/2026-08-05-direct-publish-run-lifecycle.md`
（Tasks 1–6）。本节是 v3 切档之上的一次**增量修订**，删除新执行链的模型 Reviewer，
改由确定性发布门禁逐 Artifact 发布 Version。旧 Review 表与 `reviewing` 状态**首期保留
只读兼容，不物理删除**。

### 5.1 部署前清零活动 `reviewing` Run

- 直接发布改造（Task 4）后，引擎不再启动 `artifact_reviewer_v1`，也不再进入 `reviewing`。
- 部署前**必须等待所有活动 `reviewing` Run 清零**：旧代码下进入 `reviewing` 且租约过期
  的历史 Run，在新代码恢复扫描中被**直接收口为 `failed`**（`error_code="LEGACY_REVIEWING_UNSUPPORTED"`），
  Draft 保留、不删除、不启动 Reviewer。切档前清零可避免这类 Run 在切档瞬间被新代码批量收口。
- 校验：`SELECT id FROM agent_runs WHERE status='reviewing'` 应为空；非空时先在旧代码下
  让其自然结束或人工标记终态，再切档。

### 5.2 升级迁移 0030

- 迁移链固定 `0029_agent_run_created_at → 0030_direct_publish_runtime`；
  `cd backend && .venv/bin/alembic upgrade head` 应到 head `0030_direct_publish_runtime`，
  `alembic heads` 只剩一个 head。
- 0030 只**新增**对象，downgrade 只移除本迁移创建的对象，**不触碰 Review/Reviewer 相关表**：
  - 新表 `artifact_publish_attempts`（`idempotency_key` 唯一幂等；状态
    `validating/published/validation_failed/failed`；FK→`agent_runs/agent_artifacts/
    artifact_draft_revisions/agent_artifact_versions`）；
  - `agent_artifact_versions` 新增 nullable JSON `validation_json`（新 Version 的
    `review_json=None`）；
  - `memory_entries` 类型 Check Constraint 重建加入 `confirmed_scope`。

### 5.3 新动作协议（FOUR_ACTIONS）

- 模型每轮只输出四种动作：`ask_user / call_tool / publish_artifacts / complete`
  （`FOUR_ACTIONS`）；**`submit_review` 已不再是合法动作**，模型输出会被协议拒绝。
- `publish_artifacts` 是**非终态**动作：调用 `ArtifactPublicationService` 逐 Artifact
  事务发布，结果回喂主模型，模型可继续生成下游产物或调用 `complete`。
- 无法修复的 Draft 由受控工具 `abandon_draft`（经 `call_tool`）标记 failed 并写一条
  `artifact_publish_attempts(status="failed")` 保存结构化原因，**不得用 `complete`
  静默遗留活动 Draft**。
- `complete` 前引擎查询当前 Run 拥有的 Draft 状态：若仍有未发布且未 failed/abandoned 的
  Draft，回喂 `error_code="ACTIVE_DRAFTS_REMAIN"` 让模型继续，**不直接终态**。
- 终态聚合：全部预期产物成功 → `completed`；至少一个成功且存在失败/放弃 →
  `completed_with_warnings`；无成功且无法继续 → `failed`。零发布产物收口为 `failed`
  （Task 4 收口补丁）。

### 5.4 直接发布事件顺序

每个发布成功的 Artifact 发一条 `artifact.published`（逐项即时发，缩小崩溃窗口），
`publish_artifacts` 动作处理完所有项后发一条汇总事件 `artifact.publish.completed`
（Draft 事件带 `artifact_id/draft_id/module/parent_artifact_id/status`，发布汇总项带
`draft_id`、状态及可选 `artifact_id`/`version`），
均**在 `message.completed` 之前**。终态事件（`run.completed`/
`run.completed_with_warnings`/`run.failed`/`run.cancelled`）由
`AgentEventStream.settle_terminal` 在统一事务边界收口，是该 Run 最后一条用户可见事件。
发布循环中崩溃、接管后直接 `complete` 的窗口由 complete 前缺失事件幂等补发兜底。
发布同一 Draft Revision 幂等（`idempotency_key`），不生成重复 Version；已发布 Version
永不更新；一个 Draft 发布失败**不回滚**其他成功项。

### 5.5 旧 Review 表只读保留与回滚约束

- `artifact_reviews`、`artifact_review_batches/items/attempts`、`agent_artifact_versions.review_json`、
  `agent_runs.review_count` 等旧 Reviewer 表/字段**首期保留**，新执行路径停止写入但
  不删除，用于回滚读取。
- 回滚到旧代码前**不得删除 Review 表**：回滚应用版本后旧系统仍需读取这些表恢复
  Reviewer 能力。新表（`artifact_publish_attempts`、`validation_json` 列）未被新代码写入时
  可随 0030 downgrade 整体回滚；已写入则保留新表、仅回滚应用版本。
- 0030 downgrade 会先 `DELETE FROM memory_entries WHERE memory_type='confirmed_scope'`
  再重建 `ck_memory_entries_type` CHECK 约束——MySQL 重建 CHECK 会校验既有行，
  残留 confirmed_scope 行会让 downgrade 失败，故必须先清行。已验证 downgrade/upgrade
  往返（含落库 confirmed_scope 行的场景）。
- `reviewing` 仅为历史兼容状态：新 Run 不再进入；`completed_with_warnings` 视为终态，
  纳入终态集合、租约、取消、暂停和 executor 扫描。

### 5.6 Gate A 审查修复（2026-08-06）

Gate A 审查发现的 5 项必修 + 2 项次要问题，已在同一迁移/代码批次修复，切档前核对：

- **跨 Session 引用已发布报告**：`_validate_artifact_version_ids` 不再要求 Version
  同 Session，恢复设计 §0/§5.4「跨 Session 可复用当前用户已发布 Artifact」；跨用户、
  草稿、不存在的 Version 仍 404。
- **Retry 继承输入引用**：`retry_run` 从原 Run `prompt_snapshot_json` 继承输入
  Evidence / Artifact Version / parent_run_id，与原 Run 产出合并冻结到新 Run，避免
  重试重新查 MCP 或结果漂移。
- **发布失败进入终态聚合**：引用失败（draft 不存在 / 他人持有）持久化为 failed
  `ArtifactPublishAttempt`（`artifact_id`/`draft_revision_id` 可为 NULL），
  `_publish_outcome_artifact_ids` 用 `rejected_draft_id` 聚合——Run 不会在存在
  失败发布项时被错误标记 completed，统一收口 `ALL_ARTIFACTS_FAILED`。
- **0030 downgrade 清 confirmed_scope**：见 §5.5。
- **幂等哈希含引用**：消息幂等 payload 哈希纳入 `parent_run_id` + 排序后的
  `artifact_version_ids`，同文本切换报告版本/父 Run 复用同 key 返回 409，不复用错误 Run。
- **次要：发布尝试表 run_id 索引**：`ix_artifact_publish_attempts_run_id` 支撑终态聚合
  按 run_id 扫描。
- **次要：remember_scope 校验来源消息**：`source_message_id` 必须存在、属本 Session
  且为 user 消息；空 values 拒绝。

### 5.7 Gate B：Evidence 上传与归一化诊断（2026-08-06，迁移 0031/0032）

上传、归一化诊断与结构化失败反馈已落地，切档后核对：

- **用户上传**：`POST /api/v1/agent/sessions/{id}/uploads`（multipart），仅 `.csv` /
  `.xlsx`（拒绝 `.xlsm` 等宏格式 415）；单文件 ≤ 20 MiB（413）、数据行 ≤ 50,000
  （400 `rows_exceeded`）。文件按 SHA-256 命名存本地 `AGENT_UPLOAD_STORAGE_DIR`
  （默认 `.data/agent-uploads`，服务启动自动创建），路径只由服务生成、绝不拼接
  用户文件名；上传行落 `agent_uploads`（不可变元数据），解析结果写 upload
  Evidence（`source_type=user_upload`、`tool_call_id` 为 NULL、`upload_id` 有值，
  `evidence_items` XOR 约束保证二者必居其一；upload Evidence 的 `run_id` 为 NULL，
  迁移 0032 将其改可空）。
- **上传目录运维**：目录属应用私有数据，须纳入备份（含 sha256 与
  `agent_uploads` 行一致核对）；删除文件时同步删除对应 `agent_uploads` 行与
  upload Evidence（当前无删除端点，仅备份/清理指引）。
- **消息引用上传**：`POST .../messages` 的 `upload_ids`（≤ 10）必须属当前用户 +
  当前 Session 且状态 `parsed`，否则 404（`upload_not_found` /
  `upload_not_available`）；引用写入 Run `prompt_snapshot_json.upload_ids` 并参与
  幂等哈希；未被引用的 Session 上传不混入模型上下文。
- **归一化诊断**：DataTap 成功 payload 的字段映射（`NormalizationRegistry`）随
  Evidence 落库（`normalization_status` / `field_mapping_json` /
  `unmapped_fields_json` / `truncated`）；时间键统一 `TIME_KEYS`（含「日」「周」），
  DataTap 返回日/周列时不丢数据；未识别业务字段是 `incomplete` 诊断而非无 Evidence。
- **MCP 结构化失败反馈**：所有失败/空结果回喂 `ToolFailureFeedback` JSON
  （`error_type` / `points_state` / `same_fingerprint_retry_allowed` /
  `suggested_actions` 等），并持久化到 `agent_tool_calls.safe_error_message`；
  `definitely_not_sent` 同参数只允许重试一次（幂等回放 failed 行时
  `same_fingerprint_retry_allowed=false`）；`result_unknown` 保持预留并等待恢复
  核对（禁止自动重放）；崩溃恢复（Transcript）回放同一结构化反馈。
- **Lineage 冻结**：`FrozenEvidenceSource` 快照新增 `tool_call_id`（MCP）与
  `upload_id` / `upload_sha256` / `upload_filename` / `uploaded_at`（上传）字段，
  已发布 Version 可追溯到源文件哈希。
- **归一化配置**：`AGENT_UPLOAD_STORAGE_DIR` / `AGENT_UPLOAD_MAX_BYTES` /
  `AGENT_UPLOAD_MAX_ROWS`（`.env.example` 已同步）。

### 5.8 Gate B：`dispatch_count` 指纹计数与 0034 降级约束（2026-08-06）

- `agent_tool_calls.dispatch_count`（迁移 0034，默认 1）追踪每个 `logical_call_id`
  的真实外发次数：`definitely_not_sent` 允许同指纹真实重试一次（总外发 ≤ 2），
  其余终态禁止重发；第二次 DNR 的最终反馈（`same_fingerprint_retry_allowed=false`）
  在 `finalize_release` 持有调用行锁时与 `dispatch_count` 同事务持久化，数据库与
  返回给模型的反馈完全一致（崩溃后 Transcript 恢复仍为 false）。
- **0034 downgrade 不可逆**：一旦产生过 `dispatch_count != 1` 的历史调用行，
  downgrade 会拒绝执行（`AssertionError`）。该计数是**不可逆的历史事实**——
  drain/terminate active runs 只能消除在飞调用，无法消除已落库的
  `dispatch_count=2` 行；把历史 count 改回 1 再降级是**禁止的**（会静默重置计数，
  upgrade 后可能允许第三次外发）。如必须降级，需单独设计状态备份/恢复迁移，
  不能重置计数。

## 6. 参考

- 真实 UAT 记录与账本验证：`docs/qa/2026-08-02-agent-runtime-uat.md`（结构化结果
  `outputs/agent-runtime-uat-results.json`，不提交 Git）；逐轮历史追加记录
  `docs/qa/agent-runtime-uat-rounds.md`（每轮 UAT 收尾自动追加，不覆盖）。
- 运行/恢复/账务排查：`docs/runbooks/phase-2-runtime.md`。
- 架构设计与 §19 发布阻断条件：`docs/superpowers/specs/2026-08-02-model-led-agent-runtime-design.md`。
- 真实 UAT 复跑入口：`cd backend && ./scripts/run_real_agent_uat.sh`（Task 26 已执行，本次切档前须复跑）。

### 5.9 Gate C：三类报告、KOL 评分与 Excel（2026-08-06）

- **KOL 评分升级为 kol_value_score_v3**：效果与匹配度 70 + 价格效率 30；历史
  `kol_score_v2` 快照仍可读取（score_snapshot 判别联合）。有效报价 ≥3 才计算
  价格效率（不足时价格章节 restricted）；报价缺失置后；效果 <35/70 最高「观察」。
  指标称「投放性价比指数」，不称 ROI。
- **导出缓存（迁移 0035）**：`artifact_exports` 唯一 `(artifact_version_id,
  template_version)`，同一 Version 只构建一次；渲染失败可安全重试；导出绝不调用
  模型/MCP。存储目录 `AGENT_EXPORT_STORAGE_DIR`（默认 `.data/agent-exports`）。
- **Excel 契约**：品牌 8 Sheet、达人 4 Sheet（Top20 全详情块）、活动 9/10 Sheet
  （ROI 数据可靠时才生成第 10 个）。模板由 `scripts/build_agent_artifact_templates.py`
  从用户来源模板清洗（删样例数据与图表、隐藏 TEMPLATE_VERSION 元数据）；
  图表由导出器现场重建；空章节保留表头写受限说明、不画误导图表。
- **0035 回滚**：`artifact_exports` 为纯新增表，downgrade 直接 drop 表与索引，
  不影响 0034 的 dispatch_count 语义。

### 5.10 Gate D/E：直接发布前端、上传与 UAT 状态（2026-08-07）

- **当前迁移头与部署顺序**：唯一 Alembic head 是 `0036_export_claim_token`。先备份生产库和
  应用私有上传目录，再 drain 活跃 Run / 确认 `reviewing` 为零；部署后端代码与迁移 0027–0036，
  再部署同批前端 `dist/`，最后重启单 worker 服务。不得只发布一侧。
- **权限与数据边界**：上传 `upload_ids` 必须同用户同 Session 且为 `parsed`；Artifact Version
  引用只允许同用户已发布版本；导出/达人详情均按当前用户和会话归属校验，404 不泄漏资源存在。
  上传目录、`agent_uploads` 与 upload Evidence 进入备份范围，禁止手工按原文件名拼路径。
- **直接发布与监控**：新 Run 不得创建 Reviewer Driver、Batch、Item 或 Attempt；逐项监控
  `artifact.publish.completed`（published/validation_failed/failed）、Run 的
  `completed_with_warnings`、`agent_tool_calls` 的 `unknown`/`failed_confirmed`、预留积分、
  `artifact_exports` claim token，以及上传解析失败。unknown 保留预留，只能核对，禁止自动重放。
  前端须以 `draft_id` 归并空 `artifact_id` 的失败发布项，不能额外生成“准备中”草稿卡。
- **人工验收边界**：BI 查看历史 Version、未读“更新”、重试 child Run、达人缓存命中与三类导出
  必须以 DOM/ARIA、网络/SSE、下载版本做验收；本批明确未做像素/截图视觉验收。
- **真实 UAT 状态**：2026-08-07 测试库 UAT 验证了零 MCP 的澄清和
  `brand_report_v3` restricted lineage_ok；活动回答 child Run 被真实模型供应商重连阻断并
  SIGINT 收尾。记录见 `docs/qa/agent-runtime-uat-rounds.md` 与最新
  `outputs/agent-runtime-uat-results.json`。未完成所有场景、unknown 恢复核对和 Reviewer 零写入
  证明前，生产切档仍为 **禁止**。
- **回滚**：若冒烟、权限、账本或发布监控异常，立即关闭新任务，回滚后端与前端应用版本；
  不删除 Review/Artifact/Evidence/账本/上传文件。迁移只按明确的 Alembic downgrade 执行，
  且先检查 0034 dispatch_count 的不可逆约束与 0030 confirmed_scope 清理要求；已写入数据时
  仅回滚应用版本并保留表用于排障。

### 5.11 2026-08-21 Direct MCP / 取消修复候选门禁

- 新 Pi Runtime 的 MCP 成功返回遵循标准 `CallToolResult` 透传；计费与结果旁路解耦，
  不恢复 Evidence Bridge、`unsupported_content` 业务门禁或固定 Artifact 推导。
- 取消链路必须同时满足持久取消栅栏、Gateway/worker/provider abort、ACK-loss 终态收口、
  Recovery 不重放 unknown，以及前端真实 `run.cancelled` 事件；这不是前端视觉状态放宽。
- candidate-r8（run `32473941437`）的 Backend 失败已确认是 CI 拓扑测试首次构建
  `pi-gateway` 时未安装该 workspace 依赖；其余 job 全绿，失败为
  `1 failed, 2179 passed, 29 skipped`。candidate-r9 (`3c01d13`，Actions run
  `32476331375`) 已补齐依赖并取得全绿，且已完成预发布部署。
- 唯一“瑞幸咖啡”Direct MCP + 取消真实 Web UAT 已执行一次，但在 Run 创建前因 enabled+approved
  MCP 目录 58 超过 Pi adapter 上限 32 而返回 `runtime_adapter_catalog_too_large`，没有进入模型、
  DataTap 或取消阶段。在该预检阻断被新的明确授权处理、真实 Web UAT 通过并完成回滚/监控封口前，生产
  切档仍为禁止；本修复阶段不执行灰度或生产写入，也不得用第二次 Web UAT 覆盖本次失败证据。

### 5.12 2026-08-21 Pi Adapter Catalog 容量修复（当前口径）

- 上一节的 `32` 仅是历史 control-plane catalog 防御性上限，并非 Pi SDK、模型可见工具数或业务 allowlist。
  新 Pi 路径现使用 128 条目上限与 canonical JSON 128 KiB 上限；不截断、不分页、不按用户文本或 Profile
  选择固定工具，不修改工具审核状态或 allowlist。
- `RuntimeConfigService`、后端 Pi Gateway DTO 与 `pi-gateway/src/protocol.ts` 共用相同边界与 canonical
  字节计算。`quarantined`、`unknown`、`query_user_info` 继续排除；schema/字段 digest、重复身份、敏感字段、
  parser/DTO 对称校验、单一 MCP proxy、`directTools=false`、`scriptMode=false`、output guard、租户/Session/
  Run 归属与 10 积分结算边界均保留。
- 候选 `0615533c5f65bfd55c57fbbd181fbfa622c13282` 的唯一 Actions run `32480577421` 全绿；受影响 Backend
  47 项与 Pi Gateway 56 项定向测试通过，Ruff、typecheck、build 通过，独立审查 Critical=0 / Important=0。
  没有数据库迁移，历史 Snapshot/Version/Run 不回写，正式 Artifact Schema 与通用完成不变量不变。
- 精确候选已部署到 UAT，health/ready 正常，迁移 head 为 `0049_skill_rollout_history`；只读回滚检查确认
  `uat-pi-r9-20260821` 的新 `session_analyst_v1` Snapshot 完整包含 58 项（24/16/10/8）。当前浏览器实际
  登录的是另一个个人租户，未向错误租户发起请求；专用账号认证与真实 Web UAT 尚未完成。
- 当前停止门为 `PREPROD_DEPLOYED_CATALOG_REPAIR_VERIFIED / WEB_UAT_AUTH_CONFIRMATION_REQUIRED`。
  专用账号确认后只允许一次瑞幸咖啡 Run，验证 MCP Result 直通与取消闭环；在此之前不得合入 main、生产部署
  或灰度。

#### 当前唯一真实 Web UAT 结果

- 专用账号已核对为 `UAT 瑞幸咖啡 Tester`，只创建并发送一次新 Session/Run。首次候选 claim 409 的真实
  原因为 `pi_gateway_claim_catalog_invalid`：后端 claim DTO 的 canonical 校验未兼容服务端 Pydantic
  catalog 对象；不是 catalog 数量、allowlist 或审核状态问题。
- 线性提交 `5682b6a`、`fc4e70c` 补齐 DTO mapping 回归与最小修复，受影响 Backend 定向测试 `48 passed`。
  该修复保持 128/128 KiB 边界、字段/schema digest、重复身份、敏感字段、单一 proxy、directTools/scriptMode
  和计费/归属边界不变；未新增迁移，未回写历史 Snapshot。
- 同一个 Run 的 Attempt 1 在热修复后成功领取，产生内部工具事件，但没有外部 `AgentToolCall`、DataTap
  dispatch 或标准 MCP Result 到模型；最终以 `pi_model_provider_error` failed 收口。取消未点击、未测量，
  不得报告为取消通过；Run、Attempt、租约、预留、active_run 和 worker 已清理。
- 因真实功能 UAT 未通过，当前发布状态为
  `REAL_UAT_CATALOG_CLAIM_FIXED_PROVIDER_FAILED / NOT_READY_FOR_FINAL_FUNCTIONAL_UAT`。不合入 main、
  不部署生产、不灰度；该历史结果不能由第二次 Web UAT 覆盖，当前 provider 诊断授权与停止门见 §5.13。

### 5.13 2026-08-21 Provider failure metadata 诊断门

旧真实 Run 的 provider 根因不可恢复：`worker-entry.ts` 仅记录 `stopReason="error"`，旧
`PiModelProviderError` 无分类，Child IPC 仅有 `errorCode`，stdout/stderr 被忽略。新实现只从 SDK
`AssistantMessage.errorMessage` 提取受限 metadata，使用严格 `provider_failure_v1`，原文不进入任何持久化、
IPC、HTTP、事件或日志边界；未知分类必须为 `unknown`。

该 DTO 只允许 `pi_model_provider_error`，保留 provider failure 的业务终态和“不自动重试/不创建 Attempt 2”
语义；取消/`aborted` 仍由取消栅栏收口为 `run.cancelled`。Recovery 对已提交的 terminal ACK 丢失按原 durable
终态处理，不重新执行模型或 MCP。该修复未改 provider 请求协议、DataTap allowlist、Artifact Schema 或历史数据。

未完成候选 CI、预发布诊断探针 A/B 前，不得进行新的 Web UAT。A/B 使用真实预发布 provider/model 但总模型请求不超过
3 次，DataTap 与钱包均为 0；只有 A/B 全通过且 58 项 Snapshot 只读核验通过，才允许唯一一次瑞幸咖啡 Web UAT。

### 5.14 2026-08-21 实际探针与唯一 UAT 收口

- 候选 `31796539b3297941fe1d4be48ffae5437d773b37` 的唯一 CI `32485676754` 全绿，并已按精确 HEAD 部署预发布；
  后端与 Pi Gateway 健康检查通过。
- A 使用 `tencent-plan / glm-5.2` 1 次请求成功；B 使用同一 provider/model 2 次请求完成 no-op tool-result
  continuation。总模型请求 3，DataTap/钱包均为 0，未出现 provider 鉴权、限流、上下文、协议或上游错误。
- Snapshot 只读核验为 58 项、digest `1467342826d397c8dfb3653b476e3612ded999b5ada957a401b2a7c56fbd541f`。
  唯一瑞幸咖啡 Web UAT 的业务 Run `8e711362-638a-461f-9cb4-2896e81d1ccd` 在标准 MCP Result 到达前发生
  `event_buffer_overflow`，Recovery 创建 Attempt 2；用户取消后最终为 `run.cancelled`，但已违反 Attempt=1
  预算，且 MCP Result 门槛未满足。
- 该结果不是 `READY_FOR_FINAL_FUNCTIONAL_UAT_REVIEW`。后续只能修复并定向验证事件缓冲/恢复边界；本轮不创建第二
  个 Web UAT、不合入 main、不部署生产、不执行灰度。
