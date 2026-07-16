# MCP 任务恢复与安全结构诊断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变全量审核工具传给模型的策略下，为 MCP Schema 输出失败提供安全诊断，并使任务能保留成功数据、受限恢复、一次重新规划和部分成功交付。

**Architecture:** 网关只把已验证输出持久化；验证失败时改为生成不含原始值的字段结构诊断。任务执行器在失败批次后保留已结算步骤，最多执行一次可重试调用，再把安全失败摘要和剩余预算传给重规划模型；重规划结果单独持久化并仅执行补充步骤。只要存在可标准化的成功证据，报告服务生成覆盖范围明确的 BI 并以“完成但有警告”结束任务。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、SQLAlchemy Async、Alembic、jsonschema Draft 2020-12、pytest。

## Global Constraints

- Planner 每轮接收当前用户有权限的全部已审核启用 MCP 工具及完整输入 Schema；本计划不实现动态工具检索。
- 每个明确成功的工具调用结算 10 积分；失败不扣费；所有重试、替代和补充调用合计不超过任务 `max_calls`（默认 10）。
- 不确定是否已提交至上游的 `unknown` 调用绝不重放。
- 诊断、事件、模型重规划上下文不得出现原始达人字段值、密钥、认证信息、URL、域名、主机、端口、请求头或 MCP 地址。
- 自动恢复最多一次，模型重新规划最多一次；成功步骤不重复发起外部调用。
- 必须先运行每个新增测试并确认其因功能缺失而失败，再写相应生产代码。

---

## 文件结构

- 修改 `backend/app/mcp_gateway/validation.py`：构造有大小上限、无字段值的 Schema 校验诊断。
- 修改 `backend/app/mcp_gateway/transport.py`：让调用结果携带可选安全诊断。
- 修改 `backend/app/mcp_gateway/service.py`、`backend/app/mcp_gateway/accounting.py`：将安全诊断写入失败调用的 `evidence_json`，不写入原始输出。
- 修改 `backend/app/orchestration/schemas.py`、`backend/app/orchestration/planner.py`、`backend/app/model/prompts.py`：定义并调用一次补充计划的结构化重规划协议。
- 修改 `backend/app/tasks/models.py`、`backend/app/tasks/repository.py`、`backend/app/tasks/dependencies.py`、`backend/app/tasks/executor.py`、`backend/app/tasks/state.py`：持久化一次重规划、运行补充计划并支持部分成功完成。
- 修改 `backend/app/reporting/service.py` 与 `backend/app/tasks/dependencies.py`：在报告和会话总结中提供安全的覆盖范围提示。
- 新增 Alembic 修订：为 `analysis_tasks` 增加 `replan_json` 和 `replan_count`。
- 修改 `backend/tests/mcp_gateway/test_validation.py`、新增网关诊断测试、修改 `backend/tests/orchestration/test_planner.py`、`backend/tests/tasks/test_executor.py`、`backend/tests/tasks/fakes.py`、`backend/tests/tasks/test_task_state.py`、`backend/tests/test_phase2_migrations.py`。

### Task 1: 安全 Schema 输出诊断

**Files:**
- Modify: `backend/app/mcp_gateway/validation.py`
- Modify: `backend/app/mcp_gateway/transport.py`
- Modify: `backend/app/mcp_gateway/service.py`
- Modify: `backend/app/mcp_gateway/accounting.py`
- Modify: `backend/tests/mcp_gateway/test_validation.py`
- Create: `backend/tests/mcp_gateway/test_safe_output_diagnostics.py`

**Interfaces:**
- Produces `McpValidationError.diagnostic: dict[str, JsonValue] | None`.
- Produces `safe_output_diagnostic(value, validation_error) -> dict[str, JsonValue]`.
- Extends `ToolInvocationOutcome` with `safe_diagnostic: dict[str, JsonValue] | None`.

- [ ] **Step 1: 写出 Schema 失败只包含结构元数据的测试**

```python
def test_output_schema_failure_exposes_paths_types_and_lengths_without_values() -> None:
    schema = strict_object_schema({"result": {"type": "string"}}, "result")
    raw = {"result": {"nickname": "不应持久化", "token": "secret"}}

    with pytest.raises(McpValidationError) as raised:
        validate_output(raw, schema)

    diagnostic = raised.value.diagnostic
    assert diagnostic is not None
    assert diagnostic["error_code"] == "schema_validation_error"
    assert diagnostic["instance_path"] == "/result"
    assert diagnostic["schema_path"]
    assert diagnostic["shape"]["type"] == "object"
    assert "不应持久化" not in json.dumps(diagnostic, ensure_ascii=False)
    assert "secret" not in json.dumps(diagnostic, ensure_ascii=False)
    assert "token" not in json.dumps(diagnostic, ensure_ascii=False)
```

- [ ] **Step 2: 运行测试并确认失败原因是诊断接口尚不存在**

Run: `cd backend && pytest tests/mcp_gateway/test_validation.py::test_output_schema_failure_exposes_paths_types_and_lengths_without_values -q`  
Expected: FAIL，提示 `McpValidationError` 没有 `diagnostic`。

- [ ] **Step 3: 实现最小安全诊断构造器**

```python
class McpValidationError(ValueError):
    def __init__(self, message: str, *, diagnostic: dict[str, JsonValue] | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


def _schema_failure_diagnostic(value: Any, error: ValidationError) -> dict[str, JsonValue]:
    return {
        "version": 1,
        "error_code": "schema_validation_error",
        "instance_path": _json_pointer(error.absolute_path),
        "schema_path": _json_pointer(error.absolute_schema_path),
        "validator": str(error.validator),
        "shape": _safe_shape_at_path(value, error.absolute_path),
    }
```

`_safe_shape_at_path` 只返回 `type`、`string_length`、`array_length`、`object_field_count` 和最多 20 个非敏感字段名及类型；遇到敏感字段名或其子树时返回 `{"type": "redacted"}`。修改 `_validate_against_schema`，在捕获 `ValidationError` 时附带此诊断。

- [ ] **Step 4: 将诊断传到持久化失败调用**

```python
return ToolInvocationOutcome(
    "failed", None, None, result.upstream_request_id,
    "output_validation_error", error.diagnostic,
)

# McpAccounting.finalize 的失败分支
row.evidence_json = {
    "outcome": "failed",
    "output_validation_diagnostic": outcome.safe_diagnostic,
}
```

非输出校验失败保持 `safe_diagnostic=None`，且不能把 `str(exc)`、原始响应或 URL 写进 `error_message`。

- [ ] **Step 5: 写出网关持久化安全性测试**

```python
async def test_invalid_output_persists_only_safe_diagnostic(db_session, gateway) -> None:
    row = await gateway.execute(command_for_output({"result": {"name": "达人A", "url": "https://x"}}))
    assert row.status == "released"
    payload = json.dumps(row.evidence_json, ensure_ascii=False)
    assert "output_validation_diagnostic" in payload
    assert "达人A" not in payload
    assert "https://x" not in payload
```

- [ ] **Step 6: 运行网关与校验测试**

Run: `cd backend && pytest tests/mcp_gateway/test_validation.py tests/mcp_gateway/test_safe_output_diagnostics.py -q`  
Expected: PASS。

- [ ] **Step 7: 提交该独立改动**

```bash
git add backend/app/mcp_gateway/validation.py backend/app/mcp_gateway/transport.py backend/app/mcp_gateway/service.py backend/app/mcp_gateway/accounting.py backend/tests/mcp_gateway/test_validation.py backend/tests/mcp_gateway/test_safe_output_diagnostics.py
git commit -m "feat: add safe MCP output diagnostics"
```

### Task 2: 重规划数据模型与模型协议

**Files:**
- Modify: `backend/app/orchestration/schemas.py`
- Modify: `backend/app/orchestration/planner.py`
- Modify: `backend/app/model/prompts.py`
- Modify: `backend/tests/orchestration/test_planner.py`

**Interfaces:**
- Produces `ReplanFailure`, `ReplanContext` and `Planner.replan(context, recovery) -> ToolPlan`.
- `ReplanContext` includes `completed_step_ids`, `failed_steps`, `remaining_calls`, `remaining_points`; it contains no raw MCP output.

- [ ] **Step 1: 写出补充计划不能重复已完成步骤的测试**

```python
@pytest.mark.asyncio
async def test_replan_rejects_completed_steps_and_uses_remaining_call_budget() -> None:
    planner = Planner(model=FakeModel(plan_with("step_1", "creator.search.v1")))
    recovery = ReplanContext(
        completed_step_ids=("step_1",),
        failed_steps=(ReplanFailure(step_id="step_2", error_code="output_validation_error"),),
        remaining_calls=1,
        remaining_points=10,
    )
    with pytest.raises(PlanValidationError, match="REPLAN_REUSES_COMPLETED_STEP"):
        await planner.replan(context_with_tools(), recovery)
```

- [ ] **Step 2: 运行测试并确认因缺少重规划接口失败**

Run: `cd backend && pytest tests/orchestration/test_planner.py::test_replan_rejects_completed_steps_and_uses_remaining_call_budget -q`  
Expected: FAIL，提示 `Planner` 没有 `replan`。

- [ ] **Step 3: 实现受控 ReplanContext 与 Planner.replan**

```python
class ReplanFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str = Field(pattern=r"^step_[0-9]+$")
    internal_tool_name: str = Field(min_length=1, max_length=128)
    error_code: str = Field(min_length=1, max_length=64)
    diagnostic: dict[str, JsonValue] | None = None

class ReplanContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed_step_ids: tuple[str, ...]
    failed_steps: tuple[ReplanFailure, ...]
    remaining_calls: int = Field(ge=0, le=10)
    remaining_points: int = Field(ge=0)
```

新增 `REPLANNER_PROMPT`：要求模型只输出补充步骤、不得复用已完成步骤、不得增加渠道或预算。`Planner.replan` 复用模型结构化输出和 `_validate`，并额外校验步骤数不超过 `remaining_calls`、每个 ID 不在 `completed_step_ids`、工具来自现有全量审核工具上下文。

- [ ] **Step 4: 写出模型上下文不泄露失败原始值的测试**

```python
@pytest.mark.asyncio
async def test_replan_request_contains_safe_diagnostic_but_no_raw_tool_output() -> None:
    await planner.replan(context, recovery_with_diagnostic())
    serialized = fake_model.requests[-1].messages[-1].content
    assert "schema_validation_error" in serialized
    assert "达人原始昵称" not in serialized
    assert "https://datatap" not in serialized
```

- [ ] **Step 5: 运行 Planner 测试**

Run: `cd backend && pytest tests/orchestration/test_planner.py -q`  
Expected: PASS。

- [ ] **Step 6: 提交该独立改动**

```bash
git add backend/app/orchestration/schemas.py backend/app/orchestration/planner.py backend/app/model/prompts.py backend/tests/orchestration/test_planner.py
git commit -m "feat: add safe MCP replanning protocol"
```

### Task 3: 持久化一次重规划与任务状态

**Files:**
- Create: `backend/migrations/versions/0007_task_replan_state.py`
- Modify: `backend/app/tasks/models.py`
- Modify: `backend/app/tasks/state.py`
- Modify: `backend/app/tasks/repository.py`
- Modify: `backend/app/tasks/dependencies.py`
- Modify: `backend/tests/tasks/fakes.py`
- Modify: `backend/tests/tasks/test_task_state.py`
- Modify: `backend/tests/test_phase2_migrations.py`

**Interfaces:**
- `AnalysisTask.replan_json: dict | None`, `AnalysisTask.replan_count: int`.
- `TaskStore.save_replan(task_id, worker_id, replan_json) -> bool`.
- `TaskStore.mark_completed_with_warnings(task_id, worker_id, warning_code) -> None`.

- [ ] **Step 1: 写出状态机和迁移契约测试**

```python
def test_running_task_may_complete_with_warnings() -> None:
    ensure_transition(TaskStatus.RUNNING, TaskStatus.COMPLETED_WITH_WARNINGS)
    assert TaskStatus.COMPLETED_WITH_WARNINGS in TERMINAL_TASK_STATUSES

async def test_replan_migration_adds_state_columns(async_engine) -> None:
    columns = await async_engine.run_sync(lambda sync: inspect(sync).get_columns("analysis_tasks"))
    assert {"replan_json", "replan_count"} <= {column["name"] for column in columns}
```

- [ ] **Step 2: 运行测试并确认新状态和字段尚不存在**

Run: `cd backend && pytest tests/tasks/test_task_state.py::test_running_task_may_complete_with_warnings tests/test_phase2_migrations.py -q`  
Expected: FAIL，提示状态或列不存在。

- [ ] **Step 3: 实现模型、迁移和仓储操作**

```python
class TaskStatus(StrEnum):
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"

class TaskEventType(StrEnum):
    REPLAN_READY = "replan.ready"
    TASK_COMPLETED_WITH_WARNINGS = "task.completed_with_warnings"

async def save_replan(self, task_id: str, worker_id: str, replan_json: dict[str, Any]) -> bool:
    task = await self._locked(task_id)
    if not self._owns_active_lease(task, worker_id) or task.replan_count >= 1:
        return False
    task.replan_json = replan_json
    task.replan_count += 1
    await self.append_event(task.id, task.user_id, TaskEventType.REPLAN_READY, {"attempt": task.replan_count})
    await self.db.flush()
    return True
```

迁移给 `analysis_tasks` 添加可空 JSON `replan_json` 和非空 `replan_count`（默认 `0`），并提供完整 downgrade。`mark_completed_with_warnings` 写稳定 `warning_code`，事件 payload 不含上游正文。

- [ ] **Step 4: 运行状态、仓储与迁移测试**

Run: `cd backend && pytest tests/tasks/test_task_state.py tests/test_phase2_migrations.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交该独立改动**

```bash
git add backend/migrations/versions/0007_task_replan_state.py backend/app/tasks/models.py backend/app/tasks/state.py backend/app/tasks/repository.py backend/app/tasks/dependencies.py backend/tests/tasks/fakes.py backend/tests/tasks/test_task_state.py backend/tests/test_phase2_migrations.py
git commit -m "feat: persist task replan state"
```

### Task 4: 执行器恢复与部分成功 BI

**Files:**
- Modify: `backend/app/tasks/executor.py`
- Modify: `backend/app/tasks/dependencies.py`
- Modify: `backend/app/reporting/service.py`
- Modify: `backend/tests/tasks/fakes.py`
- Modify: `backend/tests/tasks/test_executor.py`
- Modify: `backend/tests/reporting/test_reporting_service.py`

**Interfaces:**
- `TaskExecutor` runs original batches once, retries only a retryable failure once, persists/executes at most one supplement plan, then builds artifacts from settled evidence.
- `ReportingService` emits a safe coverage summary derived from MCP call statuses and internal tool names.

- [ ] **Step 1: 写出小红书成功、抖音失败仍生成报告的测试**

```python
@pytest.mark.asyncio
async def test_one_failed_parallel_channel_keeps_success_and_completes_with_warning() -> None:
    scenario = FakeExecutionScenario.with_parallel_steps()
    scenario.gateway.statuses = ("settled", "released")

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.COMPLETED_WITH_WARNINGS
    assert scenario.artifacts.calls == ["candidates", "bi", "summary"]
    assert scenario.gateway.successful_logical_calls == 1
```

- [ ] **Step 2: 运行测试并确认现有执行器会直接失败**

Run: `cd backend && pytest tests/tasks/test_executor.py::test_one_failed_parallel_channel_keeps_success_and_completes_with_warning -q`  
Expected: FAIL，当前状态为 `failed`。

- [ ] **Step 3: 实现有限恢复循环**

```python
async def _recover_failed_batch(self, task: Any, plan: ToolPlan, rows: tuple[Any, ...]) -> ToolPlan | None:
    retryable = tuple(row for row in rows if row.status == "released" and row.error_type in RETRYABLE_MCP_ERRORS)
    if retryable:
        retry_rows = await self.gateway.execute_batch(self._retry_commands(task, retryable))
        if all(row.status == "settled" for row in retry_rows):
            return None
    return await self._load_or_create_replan(task, plan, rows)
```

重试命令使用不同逻辑调用 ID 和新的补充步骤 ID，避免触及已结算调用；`unknown` 直接中断，不重试。重规划存在时从 `task.replan_json` 恢复，否则构造只含安全诊断的 `ReplanContext`，调用 `Planner.replan` 并通过 `save_replan` 原子持久化。补充计划调用数不得超过数据库已创建调用数与 `task.max_calls` 的剩余差额。

原计划失败后不再执行其后续批次；执行已保存的补充计划。若至少有一个调用已结算，即使补充计划为空或失败，也调用 artifacts 并通过 `mark_completed_with_warnings` 结束；若没有结算调用才 `mark_failed("mcp_call_failed")`。

- [ ] **Step 4: 写出重试、重规划、上限和未知状态测试**

```python
@pytest.mark.asyncio
async def test_retryable_failure_retries_once_without_replaying_settled_step() -> None:
    scenario = FakeExecutionScenario.with_parallel_steps()
    scenario.gateway.batch_statuses = [("settled", "released"), ("settled",)]
    scenario.gateway.batch_error_types = [(None, "upstream_error"), (None,)]

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.COMPLETED
    assert scenario.gateway.commands_by_batch == [2, 1]
    assert scenario.gateway.replayed_settled_logical_calls == 0
    assert scenario.gateway.wallet == [980, 0]


@pytest.mark.asyncio
async def test_non_retryable_schema_failure_uses_single_replan_with_safe_context() -> None:
    scenario = FakeExecutionScenario.with_parallel_steps()
    scenario.gateway.batch_statuses = [("settled", "released"), ("settled",)]
    scenario.gateway.batch_error_types = [(None, "output_validation_error"), (None,)]
    scenario.replan = scenario.plan_with_step("step_3")
    scenario.failed_diagnostic = {"error_code": "schema_validation_error", "shape": {"type": "object"}}

    await scenario.executor.run(scenario.task.id)

    assert scenario.replan_requests == 1
    assert scenario.task.status == TaskStatus.COMPLETED
    assert "达人原始昵称" not in scenario.replan_payload
    assert "https://datatap" not in scenario.replan_payload


@pytest.mark.asyncio
async def test_replan_is_not_called_twice_after_supplement_fails() -> None:
    scenario = FakeExecutionScenario.with_parallel_steps()
    scenario.gateway.batch_statuses = [("settled", "released"), ("released",)]
    scenario.gateway.batch_error_types = [(None, "output_validation_error"), ("output_validation_error",)]
    scenario.replan = scenario.plan_with_step("step_3")

    await scenario.executor.run(scenario.task.id)

    assert scenario.replan_requests == 1
    assert scenario.task.status == TaskStatus.COMPLETED_WITH_WARNINGS


@pytest.mark.asyncio
async def test_unknown_outcome_never_retries_or_replans() -> None:
    scenario = FakeExecutionScenario.with_parallel_steps()
    scenario.gateway.batch_statuses = [("settled", "unknown")]

    await scenario.executor.run(scenario.task.id)

    assert scenario.gateway.commands_by_batch == [2]
    assert scenario.replan_requests == 0
    assert scenario.task.status == TaskStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_recovery_stops_when_task_max_calls_is_exhausted() -> None:
    scenario = FakeExecutionScenario.with_parallel_steps()
    scenario.task.max_calls = 2
    scenario.gateway.batch_statuses = [("settled", "released")]
    scenario.gateway.batch_error_types = [(None, "output_validation_error")]

    await scenario.executor.run(scenario.task.id)

    assert scenario.replan_requests == 0
    assert scenario.gateway.commands_by_batch == [2]
    assert scenario.task.status == TaskStatus.COMPLETED_WITH_WARNINGS
```

每个测试都断言：原成功 `logical_call_id` 数量不变、失败调用不减少钱包、补充成功调用才减少 10 积分、重规划上下文不含原始值。

- [ ] **Step 5: 在报告中构造安全覆盖范围**

```python
coverage = {
    "completed_tools": sorted(settled_internal_tool_names),
    "failed_tools": sorted(released_internal_tool_names),
    "partial": bool(released_internal_tool_names),
}
evidence["coverage"] = coverage
if coverage["partial"]:
    conclusion = f"{conclusion}\n\n说明：本报告基于已成功获取的数据生成，部分渠道数据暂不可用。"
```

覆盖信息只使用内部工具名和状态，不使用失败载荷、端点或上游异常消息；`_TaskArtifacts` 把该覆盖摘要带入 Analyst 和 Summary 输入。

- [ ] **Step 6: 运行任务与报告测试**

Run: `cd backend && pytest tests/tasks/test_executor.py tests/reporting/test_reporting_service.py -q`  
Expected: PASS。

- [ ] **Step 7: 提交该独立改动**

```bash
git add backend/app/tasks/executor.py backend/app/tasks/dependencies.py backend/app/reporting/service.py backend/tests/tasks/fakes.py backend/tests/tasks/test_executor.py backend/tests/reporting/test_reporting_service.py
git commit -m "feat: recover MCP failures with partial reports"
```

### Task 5: 全链路回归与文档收尾

**Files:**
- Modify: `docs/runbooks/phase-2-runtime.md`
- Modify: `docs/superpowers/plans/2026-07-16-mcp-resilience-and-safe-diagnostics.md`

- [ ] **Step 1: 更新运行手册中的可观测项**

```markdown
- `output_validation_error`：只检查 `output_validation_diagnostic` 的版本、路径、类型和长度；不得导出 `evidence_json` 的原始失败载荷。
- `replan.ready`：确认每任务最多一次，检查剩余调用次数和积分预算。
- `task.completed_with_warnings`：确认报告覆盖范围明确且钱包仅扣除 settled 调用。
```

- [ ] **Step 2: 运行后端全套测试**

Run: `cd backend && pytest -q`  
Expected: PASS。

- [ ] **Step 3: 运行静态检查和迁移升级验证**

Run: `cd backend && ruff check app tests && alembic upgrade head`  
Expected: `All checks passed!`，且迁移成功。

- [ ] **Step 4: 在本地服务使用双渠道案例做人工验证**

使用“科颜氏、20～30 女性、小红书+抖音、近 30 天、浙江/湖州、粉丝大于 2 万”的会话，确认：

1. 两渠道工具进入同一计划；
2. 正常时两端均返回候选并扣除成功调用积分；
3. 人为注入单端 Schema 错误时，日志仅含安全诊断，另一端候选和 BI 仍完成；
4. 页面显示中文覆盖提示和正确积分余额。

- [ ] **Step 5: 更新复选框并提交收尾改动**

```bash
git add docs/runbooks/phase-2-runtime.md docs/superpowers/plans/2026-07-16-mcp-resilience-and-safe-diagnostics.md
git commit -m "docs: document MCP recovery operations"
```

## 自检结果

- 规格覆盖：Task 1 覆盖安全诊断；Task 2 覆盖全量审核工具下的重规划协议；Task 3 覆盖一次重规划的持久化与状态；Task 4 覆盖失败恢复、积分边界、部分成功 BI；Task 5 覆盖运行与回归验证。
- 占位符扫描：所有任务均给出文件、接口、测试入口和验证命令；Task 4 的五项测试会在实施时写成独立、具名测试，不以注释替代断言。
- 类型一致性：`McpValidationError.diagnostic` 由网关转为 `ToolInvocationOutcome.safe_diagnostic`，由账务持久化；`ReplanContext` 由执行器创建并由 `Planner.replan` 消费；`replan_json` 由仓储保存并在恢复时由执行器读取。
