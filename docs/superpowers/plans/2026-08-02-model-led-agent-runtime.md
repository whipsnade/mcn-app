# 模型主导统一 Agent 运行时 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 用全新的 Session/Run/Artifact 运行时一次性替换现有 Brainstorm、GoalPlanner、TaskGoal、业务 Agent Loop 与 Quick 功能，使模型自主控制分析流程，而代码只负责可信执行、证据、计费、状态和展示。

**Architecture:** 新后端以 app/agent_runtime 负责会话、Run、模型循环、工具执行、事件与恢复，以 app/agent_artifacts 负责强类型产物、Draft、Reviewer、版本、导出和达人详情缓存。现有身份、钱包、DataTap Registry/Transport、模型供应商适配、KOL 评分算法和 Excel 模板复用；旧模型路由在最终切换提交中统一取消注册。前端使用新的 Agent API、Run SSE reducer 和强类型 Artifact 视图，固定保留“品牌分析 / 活动分析 / 达人”三个 BI Tab。

**Tech Stack:** Python 3.11/3.12、FastAPI、Pydantic v2、SQLAlchemy Async、Alembic、MySQL 8、OpenAI-compatible SDK、DataTap MCP、React 19、TypeScript、Vite、Recharts、Vitest、pytest、Playwright、openpyxl。

**Review Status:** 已完成三轮独立计划评审；第三轮提出的逐达人八维评分快照缺口已修订，按三轮上限转用户人工确认。

---

## 0. 执行约束与文件边界

实施前在独立 worktree 和 codex/ 前缀分支执行。不要在当前 main 工作区直接开发；不要提交未跟踪的 outputs/。

设计依据：

- docs/superpowers/specs/2026-08-02-model-led-agent-runtime-design.md
- AGENTS.md
- docs/runbooks/phase-2-runtime.md

### 新后端模块

- backend/app/agent_runtime/models.py：Session、Message、Run、Attempt、Step、ToolCall、Evidence、Event、Memory。
- backend/app/agent_runtime/schemas.py：动作协议与 Agent API DTO。
- backend/app/agent_runtime/profiles.py：四个 Profile 的版本、Prompt、动作和工具权限。
- backend/app/agent_runtime/repository.py：租约、状态机、Attempt、Step 和事件持久化。
- backend/app/agent_runtime/events.py：持久事件流与 SSE 断线续传。
- backend/app/agent_runtime/memory.py：最近消息、摘要、Artifact 目录和按需历史读取。
- backend/app/agent_runtime/tools/：工具注册、MCP 桥、历史读取、确定性计算和 Artifact 工具。
- backend/app/agent_runtime/engine.py：统一模型动作循环。
- backend/app/agent_runtime/reviewer.py：Reviewer 内部 Run 与批次复核驱动。
- backend/app/agent_runtime/recovery.py：租约、unknown 调用和 paused Run 恢复。
- backend/app/agent_runtime/router.py：/api/v1/agent 会话、Run 和 SSE API。
- backend/app/agent_artifacts/models.py：Artifact、Draft Revision、Review Batch/Attempt、Version、未读游标和缓存。
- backend/app/agent_artifacts/payloads/：五类强类型 payload 与 insight_board_v1。
- backend/app/agent_artifacts/lineage.py：字段级来源链校验和递归固化。
- backend/app/agent_artifacts/service.py：Draft、Review、原子发布、版本查询和未读水位。
- backend/app/agent_artifacts/exporters/：品牌与圈选 Excel。
- backend/app/agent_artifacts/router.py：Artifact、版本、已读和导出 API。

### 新前端模块

- src/api/agent.ts、src/api/agentArtifacts.ts：新 API。
- src/state/agentEvents.ts：Run SSE reducer。
- src/hooks/useAgentRun.ts、src/hooks/useAgentWorkspace.ts：Run 订阅和会话状态。
- src/components/agent/：Run 卡、步骤、thinking、澄清和暂停恢复。
- src/components/artifacts/：三个 BI Tab、五类强类型视图、通用钻取与达人详情。

### 复用而不复制

- backend/app/identity/ 与 backend/app/billing/ 保持现有公开契约。
- backend/app/mcp_gateway/registry.py、transport.py、validation.py 继续负责审核工具和远端协议。
- backend/app/model/tencent_plan.py 的 OpenAI 兼容请求和 reasoning 分离能力下沉为新运行时公共入口。
- backend/app/selection/scoring_v2.py 的严格缺失为 0、跨平台 Top20 评分逻辑通过 adapter 复用。
- backend/app/reporting/templates/brand_report_v2.xlsx 和 backend/app/selection/templates/KOL匹配度分析报告.xlsx 复制为新版本受控模板，旧模板不原地覆盖。

## 阶段一：数据模型与可信状态内核

### Task 1: 建立 Agent Runtime 与 Artifact ORM

**Files:**
- Create: backend/app/agent_runtime/__init__.py
- Create: backend/app/agent_runtime/models.py
- Create: backend/app/agent_artifacts/__init__.py
- Create: backend/app/agent_artifacts/models.py
- Modify: backend/app/db/models.py
- Test: backend/tests/agent_runtime/test_models.py
- Test: backend/tests/agent_artifacts/test_models.py

- [ ] **Step 1: 写 ORM 元数据失败测试**

~~~python
def test_agent_runtime_tables_are_registered() -> None:
    expected = {
        "agent_sessions", "agent_messages", "agent_runs", "agent_run_attempts",
        "agent_steps", "agent_tool_calls", "evidence_items", "agent_events",
        "agent_tool_call_reconciliations",
        "memory_entries", "agent_artifacts", "artifact_drafts",
        "artifact_draft_revisions", "artifact_review_batches",
        "artifact_review_items", "artifact_review_attempts",
        "agent_artifact_versions", "artifact_events",
        "artifact_read_states", "kol_detail_cache",
    }
    assert expected.issubset(Base.metadata.tables)
~~~

- [ ] **Step 2: 运行测试确认因表不存在而失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_models.py tests/agent_artifacts/test_models.py -q

Expected: FAIL，缺少 agent_sessions 等表。

- [ ] **Step 3: 实现聚焦的 ORM 文件**

关键约束必须在 ORM 中直接表达：

~~~python
class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint("run_kind IN ('user','internal')", name="ck_agent_runs_kind"),
        CheckConstraint("visibility IN ('user','internal')", name="ck_agent_runs_visibility"),
        Index("ix_agent_runs_status_lease", "status", "lease_expires_at"),
    )

class ArtifactDraft(Base):
    __tablename__ = "artifact_drafts"
    __table_args__ = (UniqueConstraint("artifact_id", name="uq_artifact_drafts_artifact"),)

class ArtifactReviewAttempt(Base):
    __tablename__ = "artifact_review_attempts"
    __table_args__ = (
        UniqueConstraint("review_item_id", "attempt", name="uq_artifact_review_attempt"),
    )
~~~

使用 String(36) UUID，所有 JSON 列明确 nullable；不可变 Revision/Version/Attempt 不提供 updated_at。agent_steps 唯一 (run_id, sequence)，agent_events 唯一 (run_id, sequence)，artifact_events 唯一 (session_id, sequence)。

agent_tool_call_reconciliations 保存 unknown 调用的自动/人工核对历史：tool_call_id、source(upstream_probe/admin)、decision(confirm_success/confirm_failure/keep_unknown)、actor_user_id nullable、note、created_at。核对记录不可更新。

- [ ] **Step 4: 注册新模型并运行元数据测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_models.py tests/agent_artifacts/test_models.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_runtime backend/app/agent_artifacts backend/app/db/models.py backend/tests/agent_runtime backend/tests/agent_artifacts
git commit -m "feat: add unified agent runtime data model"
~~~

### Task 2: 新增 0027 数据库迁移

**Files:**
- Create: backend/migrations/versions/0027_agent_runtime_v3.py
- Modify: backend/tests/test_phase2_migrations.py
- Modify: backend/tests/test_schema.py

- [ ] **Step 1: 将迁移 head 预期改为 0027 并添加约束测试**

测试至少检查：

- down_revision 等于 0026_brand_report_v2_payload；
- agent_artifacts 唯一 (session_id, artifact_key)；
- artifact_drafts 唯一 artifact_id；
- agent_steps 含 attempt_id 外键；
- agent_artifact_versions 含 source_draft_revision_id 与 parent_artifact_version_id；
- agent_tool_calls.logical_call_id 全局唯一；
- agent_tool_call_reconciliations 外键指向 agent_tool_calls；
- kol_detail_cache 唯一 (user_id, session_id, platform, kol_uid)。

- [ ] **Step 2: 运行迁移测试确认失败**

Run: cd backend && .venv/bin/pytest tests/test_phase2_migrations.py tests/test_schema.py -q

Expected: FAIL，head 仍为 0026。

- [ ] **Step 3: 编写只新增表、不删除旧表的迁移**

upgrade 按父表到子表顺序创建；downgrade 反序删除新表。首次切换不得 drop sessions、analysis_tasks、task_goals、analysis_reports、quick_mcp_calls 等旧表。

- [ ] **Step 4: 在测试库执行迁移并验证单一 head**

Run: cd backend && APP_ENV=test .venv/bin/alembic upgrade head

Expected: 成功升级到 0027_agent_runtime_v3。

Run: cd backend && .venv/bin/pytest tests/test_phase2_migrations.py tests/test_schema.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/migrations/versions/0027_agent_runtime_v3.py backend/tests/test_phase2_migrations.py backend/tests/test_schema.py
git commit -m "feat: migrate unified agent runtime tables"
~~~

### Task 3: 实现 Run 状态机、Attempt 和租约

**Files:**
- Create: backend/app/agent_runtime/state.py
- Create: backend/app/agent_runtime/repository.py
- Test: backend/tests/agent_runtime/test_repository.py
- Test: backend/tests/agent_runtime/test_state.py

- [ ] **Step 1: 写状态转换与 Attempt 保护测试**

覆盖 queued→running、running→reviewing/paused/completed/failed/cancelled、paused→running；禁止 completed 再运行。验证首次执行创建 attempt=1，显式 resume 创建 attempt=2 且 decision_count 从 0 开始，Run 累计值不清零。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_state.py tests/agent_runtime/test_repository.py -q

Expected: FAIL，AgentRunRepository 未定义。

- [ ] **Step 3: 实现状态机与数据库锁**

~~~python
ATTEMPT_MAX_DECISIONS = 50
ATTEMPT_MAX_SECONDS = 30 * 60

async def begin_attempt(self, run_id: str, *, resumed: bool) -> AgentRunAttempt:
    run = await self.lock_run(run_id)
    if resumed and run.status != "paused":
        raise InvalidRunTransition("run_not_paused")
    # insert next attempt; reset only attempt counters
~~~

claim_lease 必须校验 terminal、cancel_requested 和 lease_expires_at；pause 释放租约，resume 仅能由用户 API 调用。

- [ ] **Step 4: 运行状态测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_state.py tests/agent_runtime/test_repository.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_runtime/state.py backend/app/agent_runtime/repository.py backend/tests/agent_runtime
git commit -m "feat: add resumable agent run state machine"
~~~

### Task 4: 实现持久事件流与 SSE

**Files:**
- Create: backend/app/agent_runtime/events.py
- Create: backend/app/agent_runtime/sse.py
- Test: backend/tests/agent_runtime/test_events.py
- Test: backend/tests/agent_runtime/test_sse.py

- [ ] **Step 1: 写事件序号、用户隔离和 Last-Event-ID 测试**

测试同一 Run 并发 append 得到连续 sequence；其他用户读取返回 404；断线后 last_event_id=2 只返回 3 以后；terminal 事件后流结束。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_events.py tests/agent_runtime/test_sse.py -q

Expected: FAIL。

- [ ] **Step 3: 实现数据库为权威源的事件流**

复用旧 tasks/events.py 的轮询+进程内 Broker 思路，但事件 ID 对外使用每 Run sequence，不使用数据库全局自增。SSE heartbeat 保持 15 秒，payload 均附 run_id。

- [ ] **Step 4: 运行事件测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_events.py tests/agent_runtime/test_sse.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_runtime/events.py backend/app/agent_runtime/sse.py backend/tests/agent_runtime
git commit -m "feat: add durable agent run event stream"
~~~

## 阶段二：统一模型循环与可信工具

### Task 5: 冻结动作协议和 Agent Profile

**Files:**
- Create: backend/app/agent_runtime/schemas.py
- Create: backend/app/agent_runtime/profiles.py
- Create: backend/app/agent_runtime/prompts.py
- Test: backend/tests/agent_runtime/test_actions.py
- Test: backend/tests/agent_runtime/test_profiles.py

- [ ] **Step 1: 写严格判别联合测试**

~~~python
def test_submit_review_requires_unique_non_empty_drafts() -> None:
    action = AgentActionAdapter.validate_python({
        "action": "submit_review",
        "artifact_draft_ids": ["a", "b"],
        "completion_text": "分析完成",
        "summary": "完成品牌与达人产物",
    })
    assert action.artifact_draft_ids == ("a", "b")

@pytest.mark.parametrize("payload", [
    {"action": "call_tool", "internal_tool_name": "x", "arguments": {}, "extra": 1},
    {"action": "submit_review", "artifact_draft_ids": []},
])
def test_invalid_actions_are_rejected(payload): ...
~~~

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_actions.py tests/agent_runtime/test_profiles.py -q

Expected: FAIL。

- [ ] **Step 3: 实现 ask_user/call_tool/submit_review/complete**

四个 Profile 固定为 session_analyst_v1、artifact_reviewer_v1、kol_detail_v1、utility_v1。Profile 只声明允许工具与输出，不写品牌/活动/KOL 的固定阶段或调用顺序。

- [ ] **Step 4: 运行契约测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_actions.py tests/agent_runtime/test_profiles.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_runtime/schemas.py backend/app/agent_runtime/profiles.py backend/app/agent_runtime/prompts.py backend/tests/agent_runtime
git commit -m "feat: define unified agent actions and profiles"
~~~

### Task 6: 统一模型调用、Thinking 与内部审计

**Files:**
- Modify: backend/app/model/contracts.py
- Modify: backend/app/model/tencent_plan.py
- Create: backend/app/agent_runtime/model_gateway.py
- Test: backend/tests/agent_runtime/test_model_gateway.py
- Modify: backend/tests/model/test_structured_stream.py

- [ ] **Step 1: 写有 thinking、无 thinking、非法 JSON 三类测试**

断言 reasoning_content 或 think 标签被流式分离；最终只把 JSON 交给 Pydantic。供应商没有 reasoning 时不得发送 thinking.started。Reviewer/Utility 的 thinking 只写 visibility=internal Step。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_model_gateway.py tests/model/test_structured_stream.py -q

Expected: FAIL，新 gateway 不存在。

- [ ] **Step 3: 实现统一 gateway**

~~~python
class AgentModelGateway:
    async def decide(
        self,
        *,
        run: AgentRun,
        profile: AgentProfile,
        messages: list[ModelMessage],
        thinking_sink: ThinkingSink | None,
    ) -> AgentAction:
        # persist step before provider call
        # stream only provider-exposed reasoning
        # parse final JSON after reasoning separation
~~~

thinking_text 脱敏后最多 64 KiB；模型、Profile/version、Prompt 快照、request_id、token usage 持久化到内部或用户 Run。

- [ ] **Step 4: 运行模型测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_model_gateway.py tests/model/test_structured_stream.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/model backend/app/agent_runtime/model_gateway.py backend/tests/agent_runtime backend/tests/model/test_structured_stream.py
git commit -m "feat: unify model decisions and thinking stream"
~~~

### Task 7: 实现新 Tool Registry 与 Profile 权限

**Files:**
- Create: backend/app/agent_runtime/tools/__init__.py
- Create: backend/app/agent_runtime/tools/contracts.py
- Create: backend/app/agent_runtime/tools/registry.py
- Test: backend/tests/agent_runtime/tools/test_registry.py

- [ ] **Step 1: 写工具白名单和服务端上下文注入测试**

验证模型不能覆盖 user_id/session_id/run_id；Reviewer 无 MCP 权限；Utility 无 MCP 权限；session_analyst 只能看到审核通过且用户渠道允许的工具。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/tools/test_registry.py -q

Expected: FAIL。

- [ ] **Step 3: 实现统一工具契约**

~~~python
class TrustedTool(Protocol):
    name: str
    input_model: type[BaseModel]
    points_cost: int
    external_side_effect: bool
    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult: ...
~~~

工具返回统一的 status、safe_summary、evidence_id、cursor、truncated 和 error_type。

- [ ] **Step 4: 运行 Registry 测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/tools/test_registry.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_runtime/tools backend/tests/agent_runtime/tools
git commit -m "feat: add profile-scoped trusted tool registry"
~~~

### Task 8: 接入 MCP、Evidence、计费与细粒度熔断

**Files:**
- Create: backend/app/agent_runtime/tools/mcp.py
- Create: backend/app/agent_runtime/circuit_breaker.py
- Create: backend/app/agent_runtime/evidence.py
- Modify: backend/app/mcp_gateway/datatap.py
- Modify: backend/app/mcp_gateway/transport.py
- Modify: backend/app/admin/router.py
- Modify: backend/app/admin/schemas.py
- Modify: backend/app/admin/service.py
- Test: backend/tests/agent_runtime/tools/test_mcp.py
- Test: backend/tests/agent_runtime/test_circuit_breaker.py
- Test: backend/tests/agent_runtime/test_evidence.py
- Modify: backend/tests/mcp_gateway/test_transport_policy.py
- Create: backend/tests/admin/test_agent_tool_call_reconciliation.py

- [ ] **Step 1: 写三种故障分类和积分测试**

覆盖 definitely_not_sent 释放、failed_confirmed 释放、result_unknown 保持预留、settled 结算 10 分；相同 service+tool+normalized args 熔断，不同参数和不同工具不受影响。连续让趋势工具失败超过旧 service threshold 后，情感工具和不同参数趋势调用仍必须到达 fake transport。

- [ ] **Step 2: 写外发前持久化与 logical_call_id 幂等测试**

进程在远端返回后、Evidence 入库前崩溃时，不得用相同 logical_call_id 重发；Evidence 保存完整 raw_payload_json、hash 和有限 preview。

- [ ] **Step 3: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/tools/test_mcp.py tests/agent_runtime/test_circuit_breaker.py tests/agent_runtime/test_evidence.py tests/mcp_gateway/test_transport_policy.py tests/admin/test_agent_tool_call_reconciliation.py -q

Expected: FAIL。

- [ ] **Step 4: 实现 Agent 归属的计费桥**

不要修改旧 McpAccounting 的 AnalysisTask 语义。直接在 agent_runtime/tools/mcp.py 新建 AgentMcpAccounting，共用 WalletService 的 reserve/settle/release，且 agent_tool_calls 不依赖 analysis_tasks。幂等键固定为：

~~~text
agent-mcp:{logical_call_id}:reserve
agent-mcp:{logical_call_id}:settle
agent-mcp:{logical_call_id}:release
~~~

- [ ] **Step 5: 禁用 Agent Transport 的服务级熔断**

给 DataTapTransport 增加明确的 circuit_scope 参数，允许 legacy 默认 service、新 Agent 固定 none。scope=none 时 _enter_circuit/_record_failure/_record_success 不维护服务级 open 状态；队列并发限制和超时仍保留。新 Agent 只使用 circuit_breaker.py 的 service+tool+arguments_hash 保护，禁止同时叠加两层熔断。

- [ ] **Step 6: 实现不可变 Evidence 和参数指纹**

参数先按工具 Schema 归一化，再 canonical JSON + SHA-256。Evidence 归属固定为 user/session/run，模型只得到 evidence_id 和截断预览。

- [ ] **Step 7: 实现 unknown 自动核对与管理员入口**

McpTransport 增加可选 reconcile_tool_call(upstream_request_id)；恢复流程按 logical_call_id 只核对、不重放。可确认成功且能取回 payload 时创建 Evidence 并 settle；确认失败时 release；确认成功但结果不可回取时 settle 并回喂 result_unavailable，不得生成 Evidence；无法核对时保持 reserved/unknown 并追加 keep_unknown 审计。

新增 POST /api/v1/admin/agent-tool-calls/{call_id}/reconcile，使用 require_admin 和 Idempotency-Key，只允许 confirm_success/confirm_failure/keep_unknown，并写 admin_audit_logs。管理员不能伪造 Evidence；confirm_success 无 payload 时只能结算并标记结果不可用。

- [ ] **Step 8: 运行测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/tools/test_mcp.py tests/agent_runtime/test_circuit_breaker.py tests/agent_runtime/test_evidence.py tests/mcp_gateway/test_transport_policy.py tests/admin/test_agent_tool_call_reconciliation.py tests/billing -q

Expected: PASS。

- [ ] **Step 9: 提交**

~~~bash
git add backend/app/agent_runtime backend/app/mcp_gateway/datatap.py backend/app/mcp_gateway/transport.py backend/app/admin backend/tests/agent_runtime backend/tests/mcp_gateway/test_transport_policy.py backend/tests/admin
git commit -m "feat: add metered MCP tools and immutable evidence"
~~~

### Task 9: 实现分层记忆、历史读取与确定性计算

**Files:**
- Create: backend/app/agent_runtime/memory.py
- Create: backend/app/agent_runtime/tools/history.py
- Create: backend/app/agent_runtime/tools/calculation.py
- Test: backend/tests/agent_runtime/test_memory.py
- Test: backend/tests/agent_runtime/tools/test_history.py
- Test: backend/tests/agent_runtime/tools/test_calculation.py

- [ ] **Step 1: 写上下文预算和用户隔离测试**

默认上下文只含最近消息、Session Summary、Run 摘要、Artifact 目录、工具成本和钱包余额；完整 Evidence 不自动注入。跨 Session read_artifact/search_evidence/read_tool_result 返回 404。

- [ ] **Step 2: 写确定性工具测试**

覆盖 calculate_expression、aggregate_metrics、calculate_period_comparison、normalize_sentiment、rank_kols。rank_kols 复用 scoring_v2 严格 missing_as_zero，并默认跨平台 engagement_total Top20。

- [ ] **Step 3: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_memory.py tests/agent_runtime/tools/test_history.py tests/agent_runtime/tools/test_calculation.py -q

Expected: FAIL。

- [ ] **Step 4: 实现游标读取和计算工具**

大结果只返回有限分片、总行数和 next_cursor。计算结果保存 settled tool call，供 lineage.derivation.tool_call_id 引用。

- [ ] **Step 5: 运行测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_memory.py tests/agent_runtime/tools/test_history.py tests/agent_runtime/tools/test_calculation.py -q

Expected: PASS。

- [ ] **Step 6: 提交**

~~~bash
git add backend/app/agent_runtime/memory.py backend/app/agent_runtime/tools backend/tests/agent_runtime
git commit -m "feat: add layered memory and deterministic tools"
~~~

## 阶段三：Artifact、Lineage 与 Reviewer

### Task 10: 实现五类强类型 Payload 与通用 Insight

**Files:**
- Create: backend/app/agent_artifacts/payloads/__init__.py
- Create: backend/app/agent_artifacts/payloads/common.py
- Create: backend/app/agent_artifacts/payloads/brand.py
- Create: backend/app/agent_artifacts/payloads/campaign.py
- Create: backend/app/agent_artifacts/payloads/kol_selection.py
- Create: backend/app/agent_artifacts/payloads/kol_analysis.py
- Create: backend/app/agent_artifacts/payloads/kol_detail.py
- Create: backend/app/agent_artifacts/payloads/insight.py
- Test: backend/tests/agent_artifacts/test_payloads.py

- [ ] **Step 1: 把设计文档第 12.1 节转成契约测试**

每类测试 exact schema_version、extra=forbid、Top20/Top5 长度、URL 协议、缺失 numeric=null 时必须有 limitation、complete/restricted 聚合、narrative 只能 supporting_paths 引用 data。kol_selection_v3 的 scoring.version 固定 kol_score_v2，八维键与 10/8/8/20/15/15/10/14 权重必须精确匹配；每个 item 必须冻结 score_snapshot 的 total/rating/stars/data_completeness 和 dimensions.{八维}.{raw_score,weight,weighted_score,source,missing_reason}，缺任一字段即拒绝。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_payloads.py -q

Expected: FAIL。

- [ ] **Step 3: 实现共同外壳和具体模型**

~~~python
class ArtifactPayloadBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    module: Literal["brand", "campaign", "kol"]
    data_status: Literal["complete", "restricted"]
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...]
    methodology: Methodology
~~~

不要用 dict[str, Any] 代替设计中已经冻结的 data/scope/narrative 字段。

- [ ] **Step 4: 运行 Payload 测试**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_payloads.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_artifacts/payloads backend/tests/agent_artifacts/test_payloads.py
git commit -m "feat: define typed analysis artifact payloads"
~~~

### Task 11: 实现字段级 Lineage 校验

**Files:**
- Create: backend/app/agent_artifacts/lineage.py
- Create: backend/app/agent_artifacts/schemas.py
- Test: backend/tests/agent_artifacts/test_lineage.py

- [ ] **Step 1: 写 RFC 6901、归属和递归来源测试**

覆盖 evidence source、artifact source、确定性 derivation；拒绝不存在 source_path、跨 Session Evidence、未 settled tool_call、循环 Artifact 引用、缺失 numeric lineage。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_lineage.py -q

Expected: FAIL。

- [ ] **Step 3: 实现 validator**

~~~python
async def validate_and_freeze_lineage(
    *,
    payload: ArtifactPayload,
    refs: tuple[LineageRef, ...],
    owner: OwnerScope,
) -> FrozenLineage:
    # validate JSON Pointers
    # recursively expand artifact_version refs to evidence
    # require every schema-marked numeric leaf
    # reject cycles, cross-session sources and mutable sources
~~~

- [ ] **Step 4: 运行 Lineage 测试**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_lineage.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_artifacts/lineage.py backend/app/agent_artifacts/schemas.py backend/tests/agent_artifacts/test_lineage.py
git commit -m "feat: enforce field-level artifact lineage"
~~~

### Task 12: 实现 Draft Revision、Artifact Key 与未读事件

**Files:**
- Create: backend/app/agent_artifacts/keys.py
- Create: backend/app/agent_artifacts/service.py
- Test: backend/tests/agent_artifacts/test_keys.py
- Test: backend/tests/agent_artifacts/test_drafts.py
- Test: backend/tests/agent_artifacts/test_read_state.py

- [ ] **Step 1: 写 key 标准化和并发 Draft 测试**

验证 NFKC、trim、空白折叠、英文小写、SHA-256；模型不能提交 artifact_key。相同 Artifact 同时被两个活动 Run 更新时，第二个返回 artifact_busy。

- [ ] **Step 2: 写不可变 Revision 和事件水位测试**

每次更新插入新 Revision；发布后工作头回到 idle，历史 Revision 保留；Draft 更新递增 Session artifact sequence；已读只推进到前端已渲染的 sequence。

- [ ] **Step 3: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_keys.py tests/agent_artifacts/test_drafts.py tests/agent_artifacts/test_read_state.py -q

Expected: FAIL。

- [ ] **Step 4: 实现服务**

Artifact Key 规则逐字实现设计文档第 8.1 节。parent_artifact_version_id 只写 Draft Revision 与 Published Version，不写稳定 Artifact 行。

- [ ] **Step 5: 运行测试**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_keys.py tests/agent_artifacts/test_drafts.py tests/agent_artifacts/test_read_state.py -q

Expected: PASS。

- [ ] **Step 6: 提交**

~~~bash
git add backend/app/agent_artifacts/keys.py backend/app/agent_artifacts/service.py backend/tests/agent_artifacts
git commit -m "feat: add versioned artifact draft workflow"
~~~

### Task 13: 实现 Reviewer 内部 Run 与批量原子发布

**Files:**
- Create: backend/app/agent_runtime/reviewer.py
- Modify: backend/app/agent_artifacts/service.py
- Test: backend/tests/agent_runtime/test_reviewer.py
- Test: backend/tests/agent_artifacts/test_review_batch.py

- [ ] **Step 1: 写三次调用/两次 revise 测试**

前两次允许 approve/revise/reject；第三次 revise 映射 reject。每次调用都有独立 internal AgentRun、Profile/Prompt/Step/token 审计，且不增加父 Attempt decision_count。

- [ ] **Step 2: 写多 Artifact 原子发布测试**

两个 Draft 均 approve 才在同一事务插入两个 Version 并写 assistant 消息；任一 reject 不发布任何 Version。Draft 修改后旧 approve 失效；未修改 approve 可复用。

- [ ] **Step 3: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_reviewer.py tests/agent_artifacts/test_review_batch.py -q

Expected: FAIL。

- [ ] **Step 4: 实现 Reviewer 与 review attempts**

Reviewer 只获得用户问题、不可变 Draft Revision、解析后的 lineage、Schema 和限制，不注册 MCP 工具。发布事务同时锁 Batch、Item、Revision、Draft、Artifact。

- [ ] **Step 5: 运行测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_reviewer.py tests/agent_artifacts/test_review_batch.py -q

Expected: PASS。

- [ ] **Step 6: 提交**

~~~bash
git add backend/app/agent_runtime/reviewer.py backend/app/agent_artifacts/service.py backend/tests/agent_runtime backend/tests/agent_artifacts
git commit -m "feat: add independent artifact review and atomic publish"
~~~

## 阶段四：Agent 执行、业务产物与导出

### Task 14: 实现统一 Session Agent Engine

**Files:**
- Create: backend/app/agent_runtime/engine.py
- Create: backend/app/agent_runtime/context.py
- Test: backend/tests/agent_runtime/test_engine.py

- [ ] **Step 1: 写四动作主循环测试**

覆盖 ask_user 落澄清消息并结束本 Run、call_tool 回喂结果、submit_review 进入复核、complete 无正式 Artifact 直接写消息。验证每条新用户消息创建新 Run，只有 paused/resume 复用。

- [ ] **Step 2: 写保护和失败恢复测试**

50 决策或 30 分钟暂停；余额不足作为工具结果交模型；取消停止新调用；非法动作作为结构化 validation_error 回喂，连续无效达到内部安全阈值后 failed。

- [ ] **Step 3: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_engine.py -q

Expected: FAIL。

- [ ] **Step 4: 实现最小统一循环**

~~~python
while run.status == "running":
    await guard_attempt_limits(run, attempt)
    context = await context_builder.build(run)
    action = await model_gateway.decide(run=run, profile=profile, messages=context)
    match action.action:
        case "ask_user": ...
        case "call_tool": ...
        case "submit_review": ...
        case "complete": ...
~~~

不要加入 brand_analysis_stages、GoalPolicy、固定工具数或 KOL fallback。

- [ ] **Step 5: 运行 Engine 测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_engine.py -q

Expected: PASS。

- [ ] **Step 6: 提交**

~~~bash
git add backend/app/agent_runtime/engine.py backend/app/agent_runtime/context.py backend/tests/agent_runtime/test_engine.py
git commit -m "feat: implement model-led session agent engine"
~~~

### Task 15: 实现租约执行器、恢复与 Utility

**Files:**
- Create: backend/app/agent_runtime/executor.py
- Create: backend/app/agent_runtime/recovery.py
- Create: backend/app/agent_runtime/utility.py
- Modify: backend/app/main.py
- Test: backend/tests/agent_runtime/test_executor.py
- Test: backend/tests/agent_runtime/test_recovery.py
- Test: backend/tests/agent_runtime/test_utility.py

- [ ] **Step 1: 写进程重启、unknown 和 Utility 测试**

过期租约可被新 worker 领取；完整 Step 不重放；unknown MCP 只调用 reconcile_tool_call、不重发原工具；自动核对成功/失败分别 settle/release，无法核对保持预留并产生运维告警；Utility 创建 internal Run 并只读短上下文；Utility 失败不改变父 Run 结果。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_executor.py tests/agent_runtime/test_recovery.py tests/agent_runtime/test_utility.py -q

Expected: FAIL。

- [ ] **Step 3: 实现 TaskRunner 等价的新 AgentRunExecutor**

main lifespan 启动 executor 和 recovery loop；关闭时停止领取新 Run，等待当前事务安全点。Recovery 每轮扫描过期租约和 unknown 调用，把核对结果写 agent_tool_call_reconciliations；unknown 不得沿用旧 release_expired_unknown 的超时自动释放策略。Run Summary、Session Title、建议统一走 utility_v1。

- [ ] **Step 4: 运行测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_executor.py tests/agent_runtime/test_recovery.py tests/agent_runtime/test_utility.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_runtime backend/app/main.py backend/tests/agent_runtime
git commit -m "feat: add agent execution recovery and utilities"
~~~

### Task 16: 接入 KOL 评分、名单和 KOL 分析产物

**Files:**
- Create: backend/app/agent_artifacts/builders/kol_selection.py
- Create: backend/app/agent_artifacts/builders/kol_analysis.py
- Create: backend/app/agent_runtime/tools/artifacts.py
- Test: backend/tests/agent_artifacts/test_kol_selection_builder.py
- Test: backend/tests/agent_artifacts/test_kol_analysis_builder.py

- [ ] **Step 1: 写严格评分和 Top20 测试**

严格复用 kol_score_v2 八维与既定权重：行业兴趣 10、目标地区 8、目标年龄 8、互动表现 20、活跃粉丝 15、内容质量 15、粉丝规模 10、互动粉丝比 14。任一维度缺失/无效按 0 且不重分配；growth_rate 与 quoted_price 只展示/筛选，不进入总分。每个 item 冻结完整 score_snapshot；八维原始输入、raw_score、weighted_score、total/rating/stars/data_completeness 均通过 rank_kols 的 settled 调用建立 lineage。默认 engagement_total 降序跨平台 Top20；数据不足必须 restricted。

- [ ] **Step 2: 写 KOL Analysis 父版本测试**

kol_analysis_v2 Version 必须固定 parent_artifact_version_id；下一版名单生成分析时可以复用稳定 kol-analysis Artifact，但旧版父绑定不改变。

- [ ] **Step 3: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_kol_selection_builder.py tests/agent_artifacts/test_kol_analysis_builder.py -q

Expected: FAIL。

- [ ] **Step 4: 实现 Builder Adapter 与 Artifact 工具**

Builder 只把模型已选择的 Evidence 和确定性计算结果转换为强类型 Draft，不自行决定要查哪些 MCP。

- [ ] **Step 5: 运行测试**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_kol_selection_builder.py tests/agent_artifacts/test_kol_analysis_builder.py tests/selection/test_scoring_v2.py -q

Expected: PASS。

- [ ] **Step 6: 提交**

~~~bash
git add backend/app/agent_artifacts/builders backend/app/agent_runtime/tools/artifacts.py backend/tests/agent_artifacts
git commit -m "feat: build typed KOL selection and analysis artifacts"
~~~

### Task 17: 实现达人详情 Profile 与 Session 缓存

**Files:**
- Create: backend/app/agent_runtime/kol_detail.py
- Create: backend/app/agent_artifacts/builders/kol_detail.py
- Test: backend/tests/agent_runtime/test_kol_detail.py
- Test: backend/tests/agent_artifacts/test_kol_detail_cache.py

- [ ] **Step 1: 写缓存、主页和 5 条热帖测试**

同 user/session/platform/kol_uid 24 小时内命中不扣分；跨 Session 不复用；过期可刷新；热帖最多 5 条；URL 仅 http/https；缺 URL 显示 limitation。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_kol_detail.py tests/agent_artifacts/test_kol_detail_cache.py -q

Expected: FAIL。

- [ ] **Step 3: 实现 kol_detail_v1 轻量 Run**

实现 KolDetailRunService.create(user_id, session_id, platform, kol_uid, selection_artifact_id?, selection_version?)。它创建 user-visible、profile=kol_detail_v1、run_kind=user 的辅助 Run；先读缓存，过期后由模型自主选择允许的详情/热帖工具；产物仍走 Reviewer 和不可变 kol_detail_v2 Version。

辅助 Run 使用独立并发 lane：同 Session 只限制一个活动 session_analyst_v1 Run，但允许一个不同 artifact_key 的 kol_detail_v1 与主分析并行；同 platform+kol_uid 已有活动详情 Run 时幂等返回原 Run。

- [ ] **Step 4: 运行测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_kol_detail.py tests/agent_artifacts/test_kol_detail_cache.py -q

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_runtime/kol_detail.py backend/app/agent_artifacts/builders backend/tests/agent_runtime backend/tests/agent_artifacts
git commit -m "feat: add reviewed KOL detail agent and cache"
~~~

### Task 18: 实现已发布 Artifact Excel 导出

**Files:**
- Create: backend/app/agent_artifacts/exporters/__init__.py
- Create: backend/app/agent_artifacts/exporters/brand.py
- Create: backend/app/agent_artifacts/exporters/kol_selection.py
- Create: backend/app/agent_artifacts/templates/brand_report_v3.xlsx
- Create: backend/app/agent_artifacts/templates/kol_selection_v3.xlsx
- Test: backend/tests/agent_artifacts/test_brand_export.py
- Test: backend/tests/agent_artifacts/test_kol_selection_export.py

- [ ] **Step 1: 复制受控模板并写只读 Version 测试**

品牌导出读取 brand_report_v3，圈选导出读取 kol_selection_v3；圈选工作簿逐达人输出八个 score_snapshot.dimensions.*.raw_score 列，以及 total、rating、stars、data_completeness，不使用 weighted_score 代替原始分。Draft、活动、KOL Analysis、详情、Insight 返回 409 ARTIFACT_EXPORT_UNSUPPORTED。导出不得调用模型/MCP。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_brand_export.py tests/agent_artifacts/test_kol_selection_export.py -q

Expected: FAIL。

- [ ] **Step 3: 实现模板填充和图表重建**

复用现有 exporter 的文件名清洗、数字格式和 openpyxl 图表重建经验；不要依赖模板图表往返保留。空章节不绘制误导图。

- [ ] **Step 4: 运行导出测试并人工打开样例**

Run: cd backend && .venv/bin/pytest tests/agent_artifacts/test_brand_export.py tests/agent_artifacts/test_kol_selection_export.py -q

Expected: PASS，工作簿可被 openpyxl 二次加载且 Sheet/图表引用范围正确。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/agent_artifacts/exporters backend/app/agent_artifacts/templates backend/tests/agent_artifacts
git commit -m "feat: export published brand and KOL artifacts"
~~~

## 阶段五：新 API 与前端全量迁移

### Task 19: 实现 /api/v1/agent API

**Files:**
- Create: backend/app/agent_runtime/router.py
- Create: backend/app/agent_artifacts/router.py
- Modify: backend/app/api/router.py
- Test: backend/tests/agent_runtime/test_api.py
- Test: backend/tests/agent_artifacts/test_api.py

- [ ] **Step 1: 写 Session/Run/Artifact API 契约测试**

覆盖创建/列表/详情/重命名/软删除 Session，发消息创建 Run，Run 查询/取消/恢复/SSE，POST /agent/sessions/{session_id}/kol-details 创建 kol_detail_v1 辅助 Run，Artifact 列表/版本/已读/导出，归属失败统一 404。

- [ ] **Step 2: 写幂等与冲突测试**

同 Idempotency-Key+相同消息返回同 Run；不同 payload 返回 409；同 Session 可存在历史 Run，但同时只允许一个活动 session_analyst_v1 Run。kol_detail_v1 可以与主 Run 并行，但相同 artifact_key 只能有一个活动详情 Run；内部 Reviewer Run 不出现在用户列表。

- [ ] **Step 3: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_api.py tests/agent_artifacts/test_api.py -q

Expected: FAIL。

- [ ] **Step 4: 实现 Router 与 DTO**

POST /agent/sessions/{id}/messages 在同一事务写 user message、Run 和 attempt=1，commit 后提交 executor。POST /agent/sessions/{id}/kol-details 请求体固定为 platform、kol_uid、selection_artifact_id/version 可选，响应包含 run_id 与 artifact_id，不接受客户端指定 Profile。artifact-read-state 使用 module+last_seen_sequence 并单调推进。

- [ ] **Step 5: 运行 API 测试**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_api.py tests/agent_artifacts/test_api.py -q

Expected: PASS。

- [ ] **Step 6: 提交**

~~~bash
git add backend/app/agent_runtime/router.py backend/app/agent_artifacts/router.py backend/app/api/router.py backend/tests/agent_runtime backend/tests/agent_artifacts
git commit -m "feat: expose unified agent and artifact APIs"
~~~

### Task 20: 建立前端 Agent API、事件状态与 Hook

**Files:**
- Create: src/api/agent.ts
- Create: src/api/agentArtifacts.ts
- Create: src/state/agentEvents.ts
- Create: src/hooks/useAgentRun.ts
- Create: src/hooks/useAgentWorkspace.ts
- Test: src/api/agent.test.ts
- Test: src/api/agentArtifacts.test.ts
- Test: src/state/agentEvents.test.ts
- Test: src/hooks/useAgentRun.test.tsx
- Test: src/hooks/useAgentWorkspace.test.tsx

- [ ] **Step 1: 写 TypeScript DTO 与 reducer 失败测试**

事件覆盖 run、thinking、tool、artifact、review、message；按 sequence 幂等，SSE 重连使用 Last-Event-ID。无 thinking 事件时状态只显示“正在处理”且没有可展开文本。agent.ts 还要覆盖 createKolDetailRun(sessionId, platform, kolUid, selectionRef?)。

- [ ] **Step 2: 运行测试确认失败**

Run: npm run test -- src/api/agent.test.ts src/state/agentEvents.test.ts src/hooks/useAgentRun.test.tsx

Expected: FAIL。

- [ ] **Step 3: 实现 API 和 reducer**

ApiRun 与五类 Artifact payload 使用判别联合，禁止在 UI 层以 Record<string, unknown> 消费核心 data。Hook 切换 Session 时以 generation token 丢弃迟到响应。

- [ ] **Step 4: 运行前端状态测试**

Run: npm run test -- src/api/agent.test.ts src/api/agentArtifacts.test.ts src/state/agentEvents.test.ts src/hooks/useAgentRun.test.tsx src/hooks/useAgentWorkspace.test.tsx

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add src/api/agent* src/state/agentEvents* src/hooks/useAgent* 
git commit -m "feat: add frontend agent runtime state"
~~~

### Task 21: 实现会话 Run 卡、Thinking、澄清和暂停恢复

**Files:**
- Create: src/components/agent/AgentRunCard.tsx
- Create: src/components/agent/AgentRunSteps.tsx
- Create: src/components/agent/AgentThinking.tsx
- Create: src/components/agent/AgentClarification.tsx
- Modify: src/components/ChatArea.tsx
- Test: src/components/agent/AgentRunCard.test.tsx
- Test: src/components/ChatArea.test.tsx

- [ ] **Step 1: 写独立执行卡测试**

同一会话两次分析显示两个 Run 卡；完成 Run 不吸收下一轮事件；thinking 实时展开、结束折叠；Reviewer 只显示状态；paused 有继续按钮；澄清 chips 只填入输入框。

- [ ] **Step 2: 运行测试确认失败**

Run: npm run test -- src/components/agent/AgentRunCard.test.tsx src/components/ChatArea.test.tsx

Expected: FAIL。

- [ ] **Step 3: 实现会话展示**

工具步骤展示安全名称、状态、耗时和积分，不显示原始敏感参数。Thinking 缺失时渲染不可展开“正在处理”。

- [ ] **Step 4: 运行组件测试**

Run: npm run test -- src/components/agent/AgentRunCard.test.tsx src/components/ChatArea.test.tsx

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add src/components/agent src/components/ChatArea.tsx src/components/ChatArea.test.tsx
git commit -m "feat: render independent agent run cards"
~~~

### Task 22: 实现三个 BI Tab 与五类 Artifact 视图

**Files:**
- Create: src/components/artifacts/ArtifactWorkspace.tsx
- Create: src/components/artifacts/BrandArtifactView.tsx
- Create: src/components/artifacts/CampaignArtifactView.tsx
- Create: src/components/artifacts/KolSelectionArtifactView.tsx
- Create: src/components/artifacts/KolAnalysisArtifactView.tsx
- Create: src/components/artifacts/KolDetailArtifactDialog.tsx
- Create: src/components/artifacts/InsightBoardView.tsx
- Create: src/components/artifacts/ArtifactStatus.tsx
- Test: src/components/artifacts/ArtifactWorkspace.test.tsx
- Test: src/components/artifacts/BrandArtifactView.test.tsx
- Test: src/components/artifacts/KolSelectionArtifactView.test.tsx
- Test: src/components/artifacts/KolDetailArtifactDialog.test.tsx

- [ ] **Step 1: 写导航、版本、Draft 和未读测试**

固定“品牌分析/活动分析/达人”，达人固定“KOL 分析/圈选达人”；Draft 更新只打圆点不自动切 Tab；Published 可选历史 Version；restricted 显示受限标记；Insight 挂父产物下。

- [ ] **Step 2: 写图表和详情测试**

品牌 8 章节按 v3 数据渲染；活动按 v2；圈选 Top20、评分说明和趋势现状。每张达人卡展示 score_snapshot.total/rating/stars/data_completeness；评分说明逐维展示八个 raw_score、weight、missing_reason，缺失维度明确显示 0 分，不用 weighted_score 冒充原始分。点击圈选项通过 createKolDetailRun 后订阅辅助 Run，详情展示主页、受众、趋势和最多 5 条热帖。null 显示“数据受限”，不得转 0。

- [ ] **Step 3: 运行测试确认失败**

Run: npm run test -- src/components/artifacts

Expected: FAIL。

- [ ] **Step 4: 实现视图**

复用 reportPrimitives.tsx 的格式化和 Recharts 组件，但每个强类型视图直接消费自己的 DTO。InsightBoard 只接受设计允许的 8 种 Block。

- [ ] **Step 5: 运行组件测试**

Run: npm run test -- src/components/artifacts

Expected: PASS。

- [ ] **Step 6: 提交**

~~~bash
git add src/components/artifacts src/components/reportPrimitives.tsx
git commit -m "feat: render typed analysis artifact workspace"
~~~

### Task 23: 一次性切换 App 并删除四个 Quick 功能

**Files:**
- Modify: src/App.tsx
- Modify: src/components/WorkspaceTabs.tsx
- Modify: src/components/MobileWorkspaceNav.tsx
- Modify: src/components/FavoritesPanel.tsx
- Delete: src/components/EvaluatePanel.tsx
- Delete: src/components/EvaluatePanel.test.tsx
- Delete: src/components/KolRecommendPanel.tsx
- Delete: src/components/KolRecommendPanel.test.tsx
- Delete: src/components/TopPostsPanel.tsx
- Delete: src/components/TopPostsPanel.test.tsx
- Delete: src/state/QuickFeatureCache.tsx
- Delete: src/state/QuickFeatureCache.test.tsx
- Delete: src/api/quick.ts
- Delete: src/api/quick.test.ts
- Modify: src/App.test.tsx
- Modify: src/components/WorkspaceTabs.test.tsx

- [ ] **Step 1: 先把 App 测试改为新入口**

断言不再出现达人推荐、活动评估、小红书爆贴、抖音爆贴；收藏保留；会话使用 useAgentWorkspace；右侧使用 ArtifactWorkspace。历史收藏点击先展示保存的 snapshot；有活动 Session 时可通过新 kol-details API 刷新，没有 Session 时显示“新建会话后刷新”，不得回退旧 Quick API。

- [ ] **Step 2: 运行测试确认旧 UI 失败**

Run: npm run test -- src/App.test.tsx src/components/WorkspaceTabs.test.tsx

Expected: FAIL，仍存在 Quick Tab。

- [ ] **Step 3: 切换 App 并删除 Quick 文件**

顶部工作区只保留智能会话与收藏。品牌/活动/达人属于右侧 BI，不放回顶部快捷入口。KOL 详情从圈选 Artifact 点击打开。

- [ ] **Step 4: 运行前端全量测试与类型检查**

Run: npm run test

Expected: PASS。

Run: npm run lint

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add -A src
git commit -m "feat: switch frontend to unified agent workspace"
~~~

### Task 24: 取消注册旧模型 API 并清理旧执行源码

**Files:**
- Create: backend/app/favorites/__init__.py
- Create: backend/app/favorites/router.py
- Create: backend/app/favorites/schemas.py
- Create: backend/app/favorites/service.py
- Modify: backend/app/api/router.py
- Modify: backend/app/main.py
- Modify: backend/app/db/models.py
- Modify: backend/app/admin/service.py
- Preserve as legacy ORM only: backend/app/quick/models.py
- Preserve as legacy ORM only: backend/app/goals/models.py
- Preserve as legacy ORM only: backend/app/tasks/models.py
- Preserve as legacy ORM only: backend/app/artifacts/models.py
- Preserve as legacy ORM only: backend/app/reporting/models.py
- Preserve as legacy ORM only: backend/app/selection/models.py
- Preserve for Evidence normalization: backend/app/selection/normalizers.py
- Preserve for Evidence normalization: backend/app/selection/schemas.py
- Preserve as strict scorer: backend/app/selection/scoring_v2.py
- Preserve as legacy ORM only: backend/app/workspace/models.py
- Delete: backend/app/quick/agent.py
- Delete: backend/app/quick/errors.py
- Delete: backend/app/quick/router.py
- Delete: backend/app/quick/schemas.py
- Delete: backend/app/quick/service.py
- Delete: backend/app/brainstorm/
- Delete: backend/app/goals/context.py
- Delete: backend/app/goals/evaluation.py
- Delete: backend/app/goals/logs.py
- Delete: backend/app/goals/planner.py
- Delete: backend/app/goals/policies.py
- Delete: backend/app/goals/respond.py
- Delete: backend/app/goals/schemas.py
- Delete: backend/app/goals/summary.py
- Delete: backend/app/goals/validation.py
- Delete: backend/app/orchestration/
- Delete: backend/app/tasks/dependencies.py
- Delete: backend/app/tasks/errors.py
- Delete: backend/app/tasks/events.py
- Delete: backend/app/tasks/executor.py
- Delete: backend/app/tasks/followups.py
- Delete: backend/app/tasks/recovery.py
- Delete: backend/app/tasks/repository.py
- Delete: backend/app/tasks/router.py
- Delete: backend/app/tasks/schemas.py
- Delete: backend/app/tasks/service.py
- Delete: backend/app/tasks/state.py
- Delete: backend/app/workspace/router.py
- Delete: backend/app/workspace/schemas.py
- Delete: backend/app/workspace/serializers.py
- Delete: backend/app/workspace/service.py
- Delete: backend/app/artifacts/backfill.py
- Delete: backend/app/artifacts/router.py
- Delete: backend/app/artifacts/service.py
- Delete: backend/app/reporting/analysis_reports.py
- Delete: backend/app/reporting/blocks.py
- Delete: backend/app/reporting/brand_assembler.py
- Delete: backend/app/reporting/brand_exporter.py
- Delete: backend/app/reporting/brand_narrative.py
- Delete: backend/app/reporting/brand_payload.py
- Delete: backend/app/reporting/builders.py
- Delete: backend/app/reporting/router.py
- Delete: backend/app/reporting/schemas.py
- Delete: backend/app/reporting/service.py
- Delete: backend/app/selection/analysis.py
- Delete: backend/app/selection/contract.py
- Delete: backend/app/selection/detail_snapshots.py
- Delete: backend/app/selection/detail_views.py
- Delete: backend/app/selection/exporter.py
- Delete: backend/app/selection/router.py
- Delete: backend/app/selection/scoring.py
- Delete: backend/app/selection/service.py
- Delete: backend/app/selection/top10_enrichment.py
- Delete: backend/tests/quick/
- Delete: backend/tests/brainstorm/
- Delete: backend/tests/goals/
- Delete: backend/tests/orchestration/
- Delete: backend/tests/tasks/
- Delete: backend/tests/workspace/
- Delete: backend/tests/artifacts/
- Delete: backend/tests/reporting/
- Delete: backend/tests/selection/test_analysis.py
- Delete: backend/tests/selection/test_contract.py
- Delete: backend/tests/selection/test_detail_snapshots.py
- Delete: backend/tests/selection/test_detail_views.py
- Delete: backend/tests/selection/test_exporter.py
- Delete: backend/tests/selection/test_kol_detail_normalization.py
- Delete: backend/tests/selection/test_kol_selection_endpoints.py
- Delete: backend/tests/selection/test_selection_sets.py
- Delete: backend/tests/selection/test_scoring.py
- Delete: backend/tests/selection/test_service.py
- Delete: backend/tests/selection/test_top10_enrichment.py
- Create: backend/tests/favorites/test_api.py
- Test: backend/tests/agent_runtime/test_legacy_routes_removed.py

- [ ] **Step 1: 写旧路由不可达测试**

以下执行入口必须返回 404：

- /api/v1/quick/*
- /api/v1/sessions/{id}/brainstorm
- /api/v1/sessions/{id}/tasks
- 旧 Task cancel/retry/events
- 手动 /kol-analysis 与旧 selection detail query

身份、钱包、管理员、收藏 API 必须继续可用。收藏继续使用 /api/v1/favorites 契约和 user_kol_favorites 旧表，但实现从旧 reporting.router/service 拆到独立 favorites 包。

- [ ] **Step 2: 运行测试确认失败**

Run: cd backend && .venv/bin/pytest tests/agent_runtime/test_legacy_routes_removed.py -q

Expected: FAIL，旧路由仍注册。

- [ ] **Step 3: 先拆出收藏 Router 与 Service**

把 reporting.router 中 list/create/delete favorites 和 platform+kol_uid 幂等逻辑移到 app/favorites；保留旧 UserKolFavorite、Kol 模型和 snapshot_json。更新前后端收藏契约测试，确认历史收藏仍可列出、收藏/取消、展示 snapshot。

- [ ] **Step 4: 固化 Legacy ORM 边界后再删除执行源码**

db/models.py 只从 quick.models、goals.models、tasks.models、artifacts.models、reporting.models、selection.models、workspace.models 注册旧表；这些 models.py 明确标记 legacy/read-only，不再导出执行服务。admin/service.py 对 QuickMcpCall 的积分历史查询继续引用保留的 quick.models。

先运行：

Run: rg -n "app\.(quick|brainstorm|goals|orchestration|tasks|workspace|artifacts|reporting|selection)" backend/app --glob "*.py"

逐条确认所有非 models.py 引用已经迁到 agent_runtime、agent_artifacts 或 favorites，再删除清单中的旧执行文件。不得直接删除整个 quick/goals/tasks/reporting/selection/workspace 包。

- [ ] **Step 5: 取消旧 Router 注册并迁移测试**

api_router 最终只注册 auth、users、wallet、admin、favorites、agent_runtime、agent_artifacts。删除只验证旧执行行为的测试；评分、归一化、钱包、身份、管理员、收藏和新运行时测试必须保留。

- [ ] **Step 6: 运行后端全量测试**

Run: cd backend && .venv/bin/ruff check app tests

Expected: PASS。

Run: cd backend && .venv/bin/pytest -q

Expected: PASS。

- [ ] **Step 7: 提交**

~~~bash
git add -A backend/app backend/tests
git commit -m "refactor: remove legacy model execution paths"
~~~

## 阶段六：跨端验证、真实服务 UAT 与切换

### Task 25: 更新 E2E 为新 Run 与 Artifact 契约

**Files:**
- Create: e2e/agent-runtime.spec.ts
- Create: e2e/artifact-workspace.spec.ts
- Delete: e2e/analysis-report.spec.ts
- Delete: e2e/auth-session-recovery.spec.ts
- Delete: e2e/brand-report-export.spec.ts
- Modify: playwright.config.ts

- [ ] **Step 1: 写三视口 E2E**

覆盖新建会话、澄清、独立 Run 卡、thinking 有/无、工具状态、Draft/Review/Published、三个 BI Tab、版本/子分析、未读圆点、达人详情、暂停恢复、四个 Quick 入口消失。

- [ ] **Step 2: 用 page.route 注入新 Agent API/SSE fixture**

fixture 事件必须有真实 sequence、run_id、artifact_id 和 parent_artifact_id；不得继续使用旧 task.* 事件。

把现有三份 E2E 中仍有价值的登录恢复、Session 软删除、品牌 Excel 下载断言迁入两份新规格后再删除旧文件；不得保留任何 Brainstorm、/sessions/{id}/tasks、旧 analysis-reports 或旧 brand export route fixture。

- [ ] **Step 3: 运行 E2E**

Run: npm run test:e2e

Expected: desktop-1440、tablet-1024、mobile-390 全部 PASS。

- [ ] **Step 4: 提交**

~~~bash
git add e2e playwright.config.ts
git commit -m "test: cover unified agent runtime end to end"
~~~

### Task 26: 真实模型 + DataTap MCP UAT

**Files:**
- Create: backend/tests/integration/test_agent_runtime_real.py
- Create: backend/scripts/run_real_agent_uat.sh
- Create: docs/qa/2026-08-02-agent-runtime-uat.md
- Modify: backend/pyproject.toml

- [ ] **Step 1: 添加显式 real_services marker**

默认 pytest 跳过；只有显式 RUN_REAL_SERVICES=1 和 real_services marker 才使用 backend/.env 真实供应商密钥。运行脚本先加载 .env，再强制覆盖 APP_ENV=test、MYSQL_DATABASE=kol_insight_test、MYSQL_USER=kol_test、MYSQL_PASSWORD=test-only-password、AUTH_MODE=mock，禁止 pytest 写开发库。日志不得输出 token、DSN 或完整原始 Prompt 中的敏感字段。

- [ ] **Step 2: 运行规格中的真实场景**

至少记录：

1. 信息不足时主动澄清；
2. “最近一个月瑞幸咖啡的品牌声量和情感分析”；
3. 活动分析；
4. Top20 达人圈选与 KOL 分析；
5. 基于已发布 Artifact 的情感/峰值/平台钻取；
6. 达人详情缓存、主页和 5 条热帖；
7. 趋势 504 后继续其他工具；
8. 钱包不足后的 restricted 交付；
9. Reviewer revise 后补查或修订。

Run: cd backend && chmod +x scripts/run_real_agent_uat.sh && ./scripts/run_real_agent_uat.sh

Expected: real_services 用例全部 PASS；docs/qa/2026-08-02-agent-runtime-uat.md 记录实际 run_id、MCP call id/状态、积分前后值、Artifact Version 和限制，不记录密钥或完整原始 payload。

- [ ] **Step 3: 核对账本与 Evidence**

每个 settled MCP 精确 10 积分；failed_confirmed 释放；unknown 经自动/人工核对后有 reconciliation 审计，无法核对仍保持预留；所有正式 numeric 字段均有有效 lineage。没有真实 UAT 记录时本 Task 不得提交。

- [ ] **Step 4: 提交 UAT 测试与记录**

~~~bash
git add backend/tests/integration/test_agent_runtime_real.py backend/scripts/run_real_agent_uat.sh backend/pyproject.toml docs/qa/2026-08-02-agent-runtime-uat.md
git commit -m "test: validate agent runtime with real providers"
~~~

### Task 27: 更新运行手册、Changelog 与发布回滚清单

**Files:**
- Modify: README.md
- Modify: AGENTS.md
- Modify: docs/runbooks/phase-2-runtime.md
- Create: docs/runbooks/agent-runtime-v3-cutover.md
- Modify/Create: changelog/YYYY-MM-DD.md

- [ ] **Step 1: 记录新架构和旧入口状态**

AGENTS.md 项目概述、目录、API 契约和测试策略切换为 Agent Runtime v3；不要继续描述 GoalPlanner、TaskGoal 或 Quick 为当前路径。

- [ ] **Step 2: 写切换清单**

包含测试库迁移、生产备份、新前后端同批部署、路由冒烟、积分抽查、回滚应用版本。明确首次切换不 drop 旧表。

- [ ] **Step 3: 运行最终验证矩阵**

Run: cd backend && .venv/bin/ruff check app tests

Expected: PASS。

Run: cd backend && .venv/bin/pytest -q

Expected: PASS。

Run: npm run test

Expected: PASS。

Run: npm run lint

Expected: PASS。

Run: npm run build

Expected: PASS。

Run: npm run test:e2e

Expected: PASS。

Run: cd backend && ./scripts/run_real_agent_uat.sh

Expected: PASS，且本次执行结果已追加到 UAT 记录。默认 pytest 的 skipped 结果不能替代此命令。

- [ ] **Step 4: 检查迁移和工作区**

Run: cd backend && .venv/bin/alembic heads

Expected: 只有 0027_agent_runtime_v3。

Run: git status --short

Expected: 仅计划内待提交文档；outputs/ 仍不提交。

- [ ] **Step 5: 提交**

~~~bash
git add README.md AGENTS.md docs/runbooks docs/qa changelog
git commit -m "docs: document unified agent runtime cutover"
~~~

## 最终发布 Gate

以下任一项不满足，不得合并或切换：

- unknown MCP 被自动重放、重复扣分或错误释放；
- 任一强类型正式数值无法递归追溯到当前 Session Evidence；
- Reviewer 可被主 Agent 绕过，或多 Artifact 发生部分发布；
- Run resume 重放已完成 Step、Attempt 计数未重置，或新消息复用旧执行卡；
- 跨用户/跨 Session 可读取 Message、Run、Evidence、Artifact 或达人缓存；
- 三个 BI Tab/两个达人子 Tab 契约不稳定，或声明支持的 Excel 无法打开；
- 四个 Quick 入口/API/缓存仍可从新系统触达；
- 新前端和新后端无法在同一发布批次切换。

## 后续但不属于本计划

新系统稳定运行并单独备份、单独获得用户批准后，才能编写下一份清理计划物理删除旧 Session、Task、Goal、Report、Artifact、旧 MCP/Quick 表。本计划不得包含 drop 旧表的迁移。
