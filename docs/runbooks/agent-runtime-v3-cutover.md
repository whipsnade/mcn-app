# Agent Runtime v3 一次性切换清单（Cutover Checklist）

状态：**待发布**（2026-08-03，Task 27）
适用范围：从旧多链路模型架构一次性切换到模型主导的统一 Agent 运行时（设计
`docs/superpowers/specs/2026-08-02-model-led-agent-runtime-design.md` §18/§19；实施计划
`docs/superpowers/plans/2026-08-02-model-led-agent-runtime.md` Task 26/27）。

这是一次**一次性开关（one-shot switch）**：不设新旧运行时功能开关，不允许部分用户继续
使用旧模型链路；新前端与新后端必须在**同一发布批次**完成契约切换。旧执行表**首次切换不
物理删除**，保留用于回滚；只有稳定运行并经用户单独批准后，才可另立清理迁移删除旧表。

---

## 0. 状态摘要

| 检查项 | 状态 |
|---|---|
| 代码与测试（迁移 0027–0029 + 运行时 + 前端 + E2E） | ✅ 已完成，全量验证矩阵通过（见 §4） |
| 真实模型 + 真实 DataTap UAT（Task 26） | ⚠️ 已执行，运行时机制部分验证；阻断项 1 已修复待复核，**仍存 1 个阻断项**（见 §1） |
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
6. Reviewer 可以被主 Agent 绕过，或多 Artifact 发生部分发布；
7. 四个旧快捷入口、API 或缓存仍可从新系统触达；
8. 前后端无法在同一发布批次完成契约切换。

## 3. 切档执行步骤

> 前置：§1 两个阻断项已解决并复跑真实 UAT 通过；设计 §19 阻断条件逐条核实无命中。

1. **测试库迁移**：在独立测试库（`kol_insight_test`）执行
   `cd backend && APP_ENV=test .venv/bin/alembic upgrade head`，确认到 head
   `0029_agent_run_created_at`（v3 迁移链 0027_agent_runtime_v3 → 0028_agent_artifact_read_states
   → 0029 顺序应用）；
   全量 pytest（含 `test_legacy_routes_removed.py` 的旧路由 404 断言）通过。
2. **生产备份**：切换前对生产库执行完整备份（含全部旧表——它们要在回滚时恢复读取），
   并记录备份文件路径与时间戳；同时备份 `/home/kol_insight/` 下的 `backend/.env`。
3. **同一发布批次部署新后端 + 新前端**：同步 `backend/` 代码到 UAT/生产
   （不覆盖远端 `.env`），执行 `alembic upgrade head`（只新增新表）；构建 `dist/` 并同步前端。
   重启 `systemctl restart kol-insight.service`。
4. **路由冒烟**：确认旧执行入口不可达——`/api/v1/quick/*`、`/api/v1/sessions/{id}/brainstorm`、
   `/api/v1/sessions/{id}/tasks`、手动 `/kol-analysis` 均返回 404；新 `/api/v1/agent/*` 可用；
   `GET /healthz` 返回 ok，`GET /api/v1/agent/sessions` 公网期望 401。
5. **功能冒烟**：真实账号完成 会话 → 澄清 → 品牌/活动/圈选 → Reviewer 发布 → BI 展示 →
   达人详情 → 品牌/圈选 Excel 导出 的冒烟；确认三个 BI Tab / 两个达人子 Tab 正常，快捷四入口消失。
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
（payload 带 `artifact_id/module/parent_artifact_id/status`，发布项另带 `version`），
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
- `reviewing` 仅为历史兼容状态：新 Run 不再进入；`completed_with_warnings` 视为终态，
  纳入终态集合、租约、取消、暂停和 executor 扫描。

## 6. 参考

- 真实 UAT 记录与账本验证：`docs/qa/2026-08-02-agent-runtime-uat.md`（结构化结果
  `outputs/agent-runtime-uat-results.json`，不提交 Git）；逐轮历史追加记录
  `docs/qa/agent-runtime-uat-rounds.md`（每轮 UAT 收尾自动追加，不覆盖）。
- 运行/恢复/账务排查：`docs/runbooks/phase-2-runtime.md`。
- 架构设计与 §19 发布阻断条件：`docs/superpowers/specs/2026-08-02-model-led-agent-runtime-design.md`。
- 真实 UAT 复跑入口：`cd backend && ./scripts/run_real_agent_uat.sh`（Task 26 已执行，本次切档前须复跑）。
