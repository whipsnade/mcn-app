# AGENTS.md

本文件面向 AI 编码代理，概述项目结构、开发命令与必须遵守的约定。详细信息以仓库内的 `README.md`、`docs/runbooks/agent-runtime-v3-cutover.md`、`docs/runbooks/phase-2-runtime.md` 和各模块源码为准。

> **新会话预热**：开始工作前先读 `changelog/` 目录最新 2-3 篇按日期的变更日志（改了什么、为什么、遗留事项），可快速建立上下文。
>
> **每日记录**：每天的功能与架构变更必须追加到当日 `changelog/YYYY-MM-DD.md`（没有则新建；changelog 目录在 .gitignore 中，新增文件需 `git add -f`），结构为 背景与目标 / 主要改动（含关键文件）/ 验证结果 / 遗留事项；写给没有本会话记忆的后来者，记录决策与原因，不只罗列 diff。

## 项目概述

KOL Insight AI：面向品牌用户的网红 KOL 与 MCN 营销效果智能筛选、分析与 BI 报告平台。

- 前端：React 19 + TypeScript + Vite + Tailwind CSS 4 + Motion + Recharts，端口 5173。
- 后端：Python 3.11/3.12 + FastAPI 模块化单体 + SQLAlchemy Async（asyncmy）+ Alembic，端口 8000。
- 数据库：MySQL 8，字符集 `utf8mb4`。
- 外部服务：腾讯 Token Plan 大模型（`TENCENT_PLAN_MODEL` 配置）与 DataTap MCP 网关。除登录外，模型与 MCP 只使用真实服务，不做 mock。
- 测试：Vitest（前端单测）、pytest（后端）、Playwright（E2E）。

业务要点：模拟短信/微信登录（访问令牌在内存，刷新令牌走 HttpOnly Cookie）、新用户一次性 1000 积分、不可变账本、会话按用户隔离、积分预留/结算/失败释放状态机、每次 DataTap MCP 工具调用固定计费 10 积分。充值与真实支付未开放。管理端 `/api/v1/admin` 提供用户管理、积分人工调整（`admin_adjust` 账本，支持 `Idempotency-Key`）与 `agent_tool_calls` 的 unknown 调用人工核对（`reconcile`），写操作落 `admin_audit_logs` 审计表。

### 当前架构：模型主导的统一 Agent 运行时（Agent Runtime v3）

自 2026-08-02 起系统一次性切换为「模型主导 + 可信执行内核」架构（设计 `docs/superpowers/specs/2026-08-02-model-led-agent-runtime-design.md`，实施计划 `docs/superpowers/plans/2026-08-02-model-led-agent-runtime.md`）。模型决定业务分析流程（澄清、工具选择、失败处理、钻取、产物生成），代码只负责能力边界、安全、计费、状态、证据、结构校验与表现层。

- **每条用户消息创建一个独立、可恢复、可审计的 Agent Run**，对应 `agent_runs` / `agent_run_attempts` / `agent_steps` / `agent_tool_calls` / `agent_events`。Run 状态机：`queued → running → clarification_requested / reviewing / completed / failed / paused / cancelled`。只有用户显式点击「继续」恢复 `paused` Run 才复用原 Run；普通多轮消息绝不复用已完成 Run 的执行卡。
- **四个 Profile**（`agent_runtime/profiles.py`，只限能力集合，不包含任何固定业务调用顺序）：`session_analyst_v1`（所有普通会话消息，四种动作全开）、`artifact_reviewer_v1`（正式 Artifact 提交复核，只读，输出 approve/revise/reject）、`kol_detail_v1`（点击圈选达人，轻量 Run）、`utility_v1`（标题/摘要/建议等后台轻量任务）。模型审计用途收敛为 `session_agent` / `artifact_reviewer` / `kol_detail_agent` / `utility` 四类。
- **统一动作协议**（`agent_runtime/schemas.py`，Pydantic 严格判别联合）：`ask_user`、`call_tool`、`submit_review`、`complete`。Artifact 创建/更新/历史读取/计算统一作为受控内部工具经 `call_tool` 执行，顶层动作不再增加业务特例。Engine 在 dispatch 前强制 `action in profile.allowed_actions`（如 `kol_detail_v1` 不允许 `ask_user`），违规与「适配器修复后仍非法的输出」（网关返回可恢复 `InvalidModelOutput`，仅默认动作协议路径转换；Reviewer/Utility 自定义路径仍抛 `ModelPlanInvalidError`）统一按无效动作计数并结构化回喂（`action_not_allowed` / `model_output_invalid`），连续 3 次收口 failed；供应商/鉴权/协议错误仍按系统错误直接收口。
- **统一工具运行时**（`agent_runtime/tools/registry.py`）：已审核 DataTap MCP 工具（固定 10 积分）、历史读取工具（`read_artifact`/`search_evidence`/`read_tool_result`，零积分）、确定性计算工具（`calculate_expression`/`aggregate_metrics`/`calculate_period_comparison`/`normalize_sentiment`/`rank_kols`，零积分；设计 §10.3 的 `validate_artifact_payload` 未实现，schema 校验走 lineage validator）、Artifact Draft 工具（零积分）。模型可见工具 = Profile 允许分类 ∩ 实时审核状态（approved+enabled）∩ 用户渠道权限。服务端 `user_id/session_id/run_id` 保留键在进入工具前被剥离，模型参数不能覆盖。
- **证据与产物**：`evidence_items` 不可变（完整 raw_payload_json + 内容 hash + 截断 preview），模型只能通过只读工具获取。正式 Artifact 强类型（`extra="forbid"`）：`brand_report_v3` / `campaign_report_v2` / `kol_selection_v3` / `kol_analysis_v2` / `kol_detail_v2`，加通用钻取 `insight_board_v1`（8 种 Block）。发布链路强类型边界（`agent_artifacts/validation.py` 的 `ArtifactPayloadValidator`）：schema_version 映射唯一 Pydantic 类型，module/schema_version/artifact_type 固定组合，key 所需 business fields 非空（拒绝裸 key），§2.5 反向聚合（必需章节在 availability 齐全、complete/restricted 双向一致）；create/update Draft 校验并保存标准化 `model_dump(mode="json")`（失败回喂 `artifact_payload_invalid`），publish 事务内锁定 Revision 后二次校验。字段级 lineage（RFC 6901 JSON Pointer）必须递归到当前用户 Session 的 Evidence；正式数值缺 lineage 拒绝进入复核。Draft 可连续更新（`artifact_draft_revisions` 不可变），提交 Reviewer（最多两次 revise，第三次只能 approve/reject）后以 batch 原子发布不可变 `agent_artifact_versions`；发布事务调 `ArtifactLineageFreezer` 把 Evidence 传递闭包固化进 `lineage_snapshot_json`（`evidence_refs_json` 保留模型直接引用），`data_status` 取校验后 payload 真实值。首次 submit_review 后 Batch 冻结 Draft 集合与 completion_text，集合不一致回喂 `review_batch_draft_set_mismatch`；幻觉/他人 draft_id 回喂 `draft_not_found`/`artifact_busy` 并计入无效动作（上限 3 次后 Run failed）；ask_user/complete/paused/cancelled/failed 全部非发布出口释放 Draft owner（保留 Revision），旧 owner 非活动（paused/终态）时新 Run 直接接管。Reviewer 不可被主 Agent 绕过，`restricted` 产物需 Reviewer 明确批准才可发布；导出对历史 NULL/非法 payload 映射稳定 409 `ARTIFACT_EXPORT_UNSUPPORTED`。
- **运行保护**：每个 Run Attempt 上限 30 分钟或 50 次模型决策，触发后 Run 以 `paused` 结束而非失败；resume 创建新 Attempt 并从零计数，`agent_runs.decision_count` 保留跨 Attempt 累计值用于审计。
- **计费与故障**：模型/历史/计算/Artifact 工具零积分，DataTap MCP 每次 10 积分；不设单 Run 预算，每次外部调用前实时检查钱包，余额不足作为结构化工具错误回喂模型。故障分类 `definitely_not_sent`（连接前失败、释放预留，是否重新尝试由模型决定）/ `failed_confirmed`（不自动重放、释放预留）/ `result_unknown`（禁止自动重放、保持预留进入恢复核对）/ `settled`（结算 10 分）。MCP 外发前由 `DurableToolCallCoordinator`（`agent_runtime/tools/mcp.py`）以独立会话单一事务提交调用行 + 10 分预留 + `running`（durable-before-send，commit 后才允许外发；预留与调用行同事务，无悬挂预留窗口）；finalize/reconcile 各自独立事务即时提交，恢复或人工取回的 payload 必须重新过输出 Schema 校验才能写 Evidence。Agent 路径传输固定 `circuit_scope="none"` + `retry_policy="never"`（504/5xx/协议中断/PossiblySentTimeout 属可能已发送，禁止自动重试；旧服务级熔断对新运行时不生效），细粒度熔断为进程级共享实例，熔断键 `service + internal_tool_name + SHA256(normalized_arguments)`，只阻断同参数重复撞击，不影响其他工具/参数。恢复循环除 unknown 外还把超过受控时间（`AGENT_TOOL_CALL_STUCK_SECONDS`，默认 900s，`started_at` 为准）仍处于 running/reserved 的调用先迁移为 unknown 再只读核对，绝不直接释放或重发。
- **历史数据**：只保留账号、钱包/积分账本、管理员审计与收藏（`user_kol_favorites`）；旧会话、任务、Goal、报告、旧 Artifact 与 Quick 状态不迁移、不展示、不读取。旧执行源码已删除（`brainstorm/`、`orchestration/` 整包移除；quick/goals/tasks/artifacts/reporting/workspace/selection 的 `models.py` 仅作为 legacy ORM 保留注册表，标注只读，不再导出执行服务）。
- **kol_detail 请求协调与恢复锚点（G3）**：`KolDetailRunService.create` 缓存未命中后、任何模型/MCP 调用之前先在数据库建立/认领 kol-detail 的 Artifact 身份 + working head（协调事务立即提交，owner=新 Run，`(session_id, artifact_key)` 唯一约束串行化同窗口并发，后到者撞 IntegrityError 后重读幂等返回先到者活动 Run 或其已回填缓存）——两个真实并发 create 最多一个进入引擎，MCP 抓取与 10 积分扣费至多一次；缓存命中路径不加锁。协调事务提交的 Run 处于 running + 本 worker 活跃租约（executor/recovery 不重复领取），崩溃超时由恢复循环接管；引擎正常收口（failed/paused/cancelled）提交终态，引擎抛出未捕获异常时服务尽力置 Run failed + 释放 working head 并提交（不遮蔽原异常）。kol_detail Run 创建时把 platform/kol_uid 触发上下文持久化进 `prompt_snapshot_json`（`kol_detail` 键，与幂等键 `idempotency_key` 不冲突），`RunTranscriptLoader` 对无 `input_message_id` 的 Run 从该快照恢复触发消息，不回退会话最近普通消息；`RunTranscript.user_question` 是显式用户问题锚点（tool_result 回放同为 role="user"，引擎不再从消息尾部反推），经 executor 传入 `AgentEngine.run(user_question=...)`，Memory Header 与 Reviewer 上下文同源。
- **迁移**：`0027_agent_runtime_v3` 新增 19 张新表（agent_sessions/agent_messages/agent_runs/agent_run_attempts/agent_steps/agent_tool_calls/agent_tool_call_reconciliations/evidence_items/agent_events/memory_entries/agent_artifacts/artifact_drafts/artifact_draft_revisions/artifact_review_batches/artifact_review_items/artifact_review_attempts/agent_artifact_versions/artifact_events/kol_detail_cache），并扩展既有 `artifact_read_states`（0022 遗留表）的 module/last_seen_sequence/updated_at 读游标列；`0028_agent_artifact_read_states` 新建独立 `agent_artifact_read_states`（session FK → `agent_sessions.id`，已读水位切换到此表，旧 `artifact_read_states` 不再被新代码读写）并给 `agent_artifact_versions` 增加 `lineage_snapshot_json`（A5 起发布事务写入冻结闭包，旧 Version 为 NULL）；**不 drop 任何旧表**；首次切换保留旧表用于回滚，稳定后需单独迁移 + 单独用户批准才能清理。
- **UAT 已知阻断项**：① 真实 DataTap 长查询传输层挂起（Incident #8）已修复为运行时墙钟收口（B8：Agent 传输 `call_timeout_seconds`，配置 `AGENT_MCP_CALL_TIMEOUT_SECONDS` 默认 150s，超时按 result_unknown 收口、保留预留、Run 继续），待真实 UAT 复核；② 真实模型在 Attempt 预算内无法可靠产出 lineage 有效正式 Artifact（probe 45 决策 / 17 次 revision 仍未过审）仍未解决。详见 `docs/runbooks/agent-runtime-v3-cutover.md` 与 `docs/qa/2026-08-02-agent-runtime-uat.md`。

## 项目结构

```text
backend/            FastAPI 后端
  app/
    api/router.py   /api/v1 路由聚合（auth、users、wallet、admin、favorites、agent_runtime、agent_artifacts）
    core/           配置（pydantic-settings）、错误、安全、日志脱敏
    db/             SQLAlchemy Base、引擎与会话
    identity/       用户、模拟认证提供商、JWT；dependencies.py 含 require_admin
    billing/        钱包、账本、积分预留/结算/管理员调整（admin_adjust）
    admin/          管理端账号/积分管理与 agent_tool_calls 人工核对（/api/v1/admin）
    favorites/      收藏（/api/v1/favorites，platform+kol_uid 身份 + snapshot_json 快照）
    agent_runtime/  模型主导 Agent 运行时（当前核心）：
                    models.py Session/Run/Attempt/Step/ToolCall/Evidence/Event/Memory ORM
                    schemas.py 四动作协议与 Agent API DTO
                    profiles.py 四个 Profile（session_analyst_v1/reviewer/kol_detail/utility）
                    state.py / repository.py Run 状态机、Attempt、租约与持久化
                    events.py / sse.py 持久事件流与 Last-Event-ID 断线续传
                    memory.py 分层记忆（最近消息+摘要+Artifact 目录+按需历史读取）
                    engine.py 统一模型动作循环（session_analyst）
                    executor.py / recovery.py 租约执行器、恢复循环、unknown 核对
                    reviewer.py Reviewer 内部 Run 与批次复核驱动
                    kol_detail.py KOL 详情轻量 Run 与 24h 会话缓存
                    utility.py 标题/摘要/建议等后台任务
                    model_gateway.py 统一模型适配 + thinking 流分离 + 非法输出分层
                    thinking.py   AgentEventThinkingSink（真实 thinking → thinking.* 事件）
                    circuit_breaker.py 细粒度熔断
                    evidence.py Evidence 入库/不可变/预览
                    tools/ 可信工具运行时：registry.py 注册表、contracts.py 契约、
                            mcp.py MCP 桥+计费、history.py 历史读取、calculation.py 确定性计算、
                            artifacts.py Artifact Draft 工具
    agent_artifacts/ 强类型产物（当前核心）：
                    models.py Artifact/Draft/Revision/Review/Version/未读/缓存 ORM
                    payloads/ 五类强类型 payload + insight_board_v1（extra=forbid）
                    lineage.py 字段级来源链校验与递归固化
                    keys.py Artifact key 标准化（NFKC/SHA-256）
                    service.py Draft/Review/原子发布/版本/未读水位
                    builders/ 把已选 Evidence + 确定性计算转成强类型 Draft
                    exporters/ 只读已发布 Version 生成 Excel（品牌/圈选）
                    router.py /schemas.py Artifact 列表/详情/版本/已读/导出 API
    mcp_gateway/    DataTap MCP 客户端、工具审核注册/校验、计费记账（复用不复制）
    model/          腾讯 Token Plan 适配层（OpenAI 兼容 + reasoning 分离）、契约与依赖
    selection/      scoring_v2.py 严格八维 KOL 评分（复用）、normalizers.py/schemas.py 证据归一化（复用）
    quick/          仅保留 models.py（legacy ORM 只读，路由已移除）
    goals/          仅保留 models.py（legacy ORM 只读）
    tasks/          仅保留 models.py（legacy ORM 只读）
    artifacts/      仅保留 models.py（legacy ORM 只读）
    reporting/      仅保留 models.py + templates（legacy ORM 只读）
    selection/      仅保留 models.py + scoring_v2.py + normalizers.py + schemas.py（legacy ORM 只读）
    workspace/      仅保留 models.py（legacy ORM 只读）
  migrations/       Alembic 迁移（0001_… 顺序编号；head 为 0027_agent_runtime_v3）
  tests/            pytest，目录结构与 app/ 对齐；agent_runtime/ 与 agent_artifacts/ 为新运行时测试
src/                React 前端
  api/              API Client 与类型契约；agent.ts / agentArtifacts.ts 为新 Agent API
  state/            agentEvents.ts Run SSE reducer
  hooks/            useAgentRun、useAgentWorkspace（新运行时）
  components/       ChatArea、SessionList、WorkspaceTabs 等；agent/ 为 Run 卡/步骤/thinking/澄清；
                    artifacts/ 为三个 BI Tab 与五类 Artifact 视图、达人详情
  test/             Vitest setup、fixtures、SSE 模拟
e2e/                Playwright 端到端测试（agent-runtime.spec.ts / artifact-workspace.spec.ts）
docs/               架构设计、分阶段计划、运行手册（runbooks）、QA 记录
server.ts           旧的 Express/Gemini 原型，仅 dev:legacy 保留，不是当前架构
```

`package.json` 中的 `server.ts`、`@google/genai` 属于遗留原型；当前系统以后端 FastAPI 为准，新功能不要改 server.ts。`src/api/` 下的旧 `sessions.ts` / `tasks.ts` / `brainstorm.ts` / `taskStream.ts` 等是遗留 API Client，新运行时已不消费，属于待清理的只读残留，不要在它们之上新增功能。

## 本地启动

前置：Node.js + npm、Python 3.11/3.12、运行中的 MySQL 8。

1. 建库（开发库 `kol_insight` 与测试库 `kol_insight_test`，均 `utf8mb4`），并按 README 创建只能访问测试库的 `kol_test` 账号。
2. `cp .env.example .env`，填写 MySQL 密码、随机 JWT 密钥（≥32 字符）、`TENCENT_PLAN_API_KEY`、`DATATAP_MCP_TOKEN`。
3. 后端依赖：`cd backend && python -m venv .venv && .venv/bin/pip install -e '.[dev]'`
4. 迁移：`cd backend && .venv/bin/alembic upgrade head`（测试库用 README 中的 `APP_ENV=test … alembic upgrade head` 命令单独迁移）。
5. 启动后端：`cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 5`（超时参数防止前端 SSE 长连接把 reload 的优雅停机卡死）。
6. 启动前端：`npm install && npm run dev`，访问 `http://127.0.0.1:5173`。Vite 将 `/api` 代理到 `127.0.0.1:8000`。
7. 开发环境短信验证码固定为 `000000`。

## 验证命令

改动后必须运行与改动范围对应的检查，全部通过才算完成。

```bash
# 后端（在 backend/ 目录下）
.venv/bin/ruff check app tests
.venv/bin/pytest -q

# 前端（仓库根目录）
npm run test     # Vitest 单测，范围是 src/
npm run lint     # 实际是 tsc --noEmit 类型检查
npm run build    # 生产构建

# E2E（首次需 npx playwright install chromium）
npm run test:e2e
```

真实模型 + 真实 DataTap 的 UAT 默认被 pytest 跳过（`real_services` marker，`RUN_REAL_SERVICES=1` 启用），单独入口 `cd backend && ./scripts/run_real_agent_uat.sh`（强制覆盖测试隔离变量，不触碰 dev DB）。Task 26 已执行过真实 UAT；本次切档前的复跑入口与记录见 `docs/qa/2026-08-02-agent-runtime-uat.md`。

## 代码约定

- Python：ruff，行宽 100，目标 `py311`。后端使用 async SQLAlchemy 2.0 风格与 pydantic-settings 配置。
- TypeScript：`tsc --noEmit` 作为 lint；路径别名 `@/*` 指向仓库根目录。React 组件与其测试文件同目录（`Xxx.tsx` / `Xxx.test.tsx`）。
- 数据库变更必须新增 Alembic 迁移（`backend/migrations/versions/`，沿用 `NNNN_描述.py` 编号格式），不可手改已合入的迁移。**新执行功能不得读取/写入旧会话、任务、Goal、报告、Quick 表**（legacy ORM 只读）。
- API 契约：前端类型集中在 `src/api/agent.ts` / `src/api/agentArtifacts.ts`（新 Agent API）与 `src/api/contracts.ts`；后端 schema 在各模块 `schemas.py`，两端改动需保持一致。
- Agent 契约（当前核心）：
  - 端点前缀 `/api/v1/agent`：`POST/GET /agent/sessions`、`GET/PATCH/DELETE /agent/sessions/{session_id}`、`POST /agent/sessions/{session_id}/messages`（写消息 + 建 `session_analyst_v1` Run，`Idempotency-Key` 幂等 + 活动 Run 并发 409）、`GET /agent/runs/{run_id}`、`GET /agent/runs/{run_id}/events`（SSE，Last-Event-ID 续传）、`POST /agent/runs/{run_id}/cancel`、`POST /agent/runs/{run_id}/resume`、`POST /agent/sessions/{session_id}/kol-details`（创建 `kol_detail_v1` 轻量 Run）、`GET /agent/sessions/{session_id}/artifacts`、`GET /agent/artifacts/{artifact_id}`、`GET /agent/artifacts/{artifact_id}/versions/{version}`、`PUT /agent/sessions/{session_id}/artifact-read-state`、`GET /agent/artifacts/{artifact_id}/export`。
  - SSE 事件：`run.started/paused/resumed/completed/failed/cancelled`、`thinking.started/delta/completed/failed`、`tool.started/succeeded/failed/unknown`、`artifact.draft.created/updated`、`review.started/revision_requested/approved/rejected`、`artifact.published`、`message.completed`；payload 必须带 `run_id`，前端按 sequence 幂等归并。事件顺序固定：thinking/tool/review/artifact → assistant message → `message.completed` → `run.completed|failed|cancelled`（终态事件是该 Run 最后一条用户可见事件）；thinking 事件只在执行层为用户可见 Run（session_analyst 主 Run / kol_detail Run）注入 `AgentEventThinkingSink`（`agent_runtime/thinking.py`，delta 脱敏 + 累计 64 KiB 上限）且供应商真实返回 reasoning_content/`<think>` 时产生，Reviewer/Utility 内部 Run 不发。
  - 归属失败统一 404（不泄漏存在性）；导出不支持类型或未发布 draft → 409 `ARTIFACT_EXPORT_UNSUPPORTED`。
  - Artifact 是强类型不可变 Version，`data_status` 只能 `complete/restricted`；业务数值允许 `null` 但对应路径必须 partial/unavailable 并给 limitation，前端显示「数据受限」，不得把 `null` 当 0。
  - 不设固定阶段清单/固定工具顺序；不要在引擎里加 brand_analysis_stages、GoalPolicy、固定工具数或 KOL fallback。模型只能经 `call_tool` 间接读写数据，不能直接持有数据库连接或 DataTap 密钥。
- 注释与文档：仓库内 Markdown 文档使用中文；代码注释可中英混用，保持与所在文件一致。

## 测试策略

- 后端 pytest：`backend/tests/conftest.py` 默认注入测试环境变量，固定使用独立测试库 `kol_insight_test` 与专用账号 `kol_test`；数据库 fixture 以事务回滚方式隔离每个用例，绝不写开发库。运行 pytest 前测试库需已迁移到 head。
  - 新运行时核心测试：`tests/agent_runtime/`（state/repository/events/sse/actions/profiles/model_gateway/tools/engine/executor/recovery/reviewer/utility/kol_detail/api/legacy_routes_removed）与 `tests/agent_artifacts/`（payloads/lineage/keys/drafts/review_batch/read_state/builders/export/cache/api）。
  - `tests/agent_runtime/test_legacy_routes_removed.py` 断言旧执行入口（`/api/v1/quick/*`、`/sessions/{id}/brainstorm`、`/sessions/{id}/tasks`、旧 cancel/retry/events、手动 /kol-analysis 等）返回 404。
- 前端 Vitest：jsdom 环境，setup 在 `src/test/setup.ts`，SSE 用 `src/test/fakeSse.ts` 模拟。
- Playwright：自动拉起 8000 端口 FastAPI（注入测试环境变量）与 5173 端口 Vite，覆盖 1440×900、1024×768、390×844 三种视口；`reuseExistingServer: false`，端口被占用会直接失败——运行前确认两个端口空闲。规格为 `e2e/agent-runtime.spec.ts` 与 `e2e/artifact-workspace.spec.ts`（route mock 注入新 Agent API/SSE fixture）。

## 配置与安全

- 所有密钥（MySQL 密码、JWT 密钥、`TENCENT_PLAN_API_KEY`、`DATATAP_MCP_TOKEN`）只放在未跟踪的 `.env`；`.env.example` 仅保留占位符，严禁写入真实凭证。
- `app/core/config.py` 在启动时做硬性校验：`MCP_CALL_POINTS` 必须为 10、密钥不得为空。模型供应商可自由配置：`TENCENT_PLAN_BASE_URL` / `TENCENT_PLAN_MODEL` / `TENCENT_PLAN_API_KEY` 支持任意 OpenAI 兼容端点（腾讯 Token Plan、月之暗面 Kimi 等）；`TENCENT_PLAN_REASONING_EFFORT`（low/high/max）为可选思考深度，仅 k3 等推理模型生效，缺省不向端点发送该参数。
- `AUTH_MODE=mock` 仅允许 `development` 与 `test`；`production` 下检测到 mock 认证会拒绝启动。
- 测试账号 `kol_test` 只能访问 `kol_insight_test`，禁止授予开发库或生产库权限。
- 工具启用流程：远程发现的工具默认 quarantined；启用 = 在 `mcp_gateway/registry.py` 的 `DYNAMIC_TOOL_ALLOWLIST` 登记（内部名、审核描述、输出 Schema）并将 `review_status` 置 approved，启动时按实时签名复核，digest 变化会重新隔离。`remote_name` 一律取审核内部名（与实时网关工具名一致）。
- 普通用户的会话、消息、Run、Evidence、Artifact、达人缓存查询必须始终带当前认证用户 + Session 归属条件（用户数据隔离），新增查询时不得遗漏；归属失败统一 404。
- 模型永远不接触 DataTap token、数据库 DSN、JWT 密钥或完整管理员信息；`unknown` MCP 调用禁止自动重放，只能经恢复核对（`agent_tool_call_reconciliations`）后结算/释放。

## 运行手册

- 一次性切换、发布阻断条件与回滚清单见 `docs/runbooks/agent-runtime-v3-cutover.md`（含 UAT 发现的两个 must-resolve 阻断项）。
- 第二阶段运行/恢复/回滚与真实供应商授权、UAT 服务器部署约定见 `docs/runbooks/phase-2-runtime.md`。
