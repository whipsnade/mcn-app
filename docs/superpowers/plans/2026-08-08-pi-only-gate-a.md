# Pi-only Gate A Implementation Plan

> SUPERSEDED 说明（2026-08-13）：本文是 2026-08-08 的历史 Gate A 实施计划，正文不改写。
> 文中 `required_artifact_type`/「Evidence lineage」等表述按当时 POC/Gate 设计理解；
> 现行 Pi production path（audited Direct MCP baseline `c01ec1ba…`）无 required
> artifact 门禁、不写数据库 Evidence。历史 Gate A 结论保持原样。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除方案 A 验收 Harness 中的 Current Runtime 对照链路，修复 MySQL 长会话中的 Run 内序号旧快照冲突，并以一次 Pi-only 六场景真实轮次产出可审计的绝对 Gate A 结论。

**Architecture:** 保留生产 Current Runtime 与未来方案 B 回滚能力，只把 POC 的 Task 9 改为 Pi 单路径。后端先通过锁定 `AgentRun` 与 `SELECT ... FOR UPDATE` 当前读统一 Attempt、Step、Event 序号，再由六场景隔离执行器生成不可变 `execution.json`；技术执行完整后，独立的零模型、零 MCP finalizer 读取人工评分并一次性生成 `summary.json`。

**Tech Stack:** Python 3.11/3.12、FastAPI、SQLAlchemy Async、MySQL 8、Pydantic、openpyxl、pytest、Node.js 20+、TypeScript、Pi RPC、Vitest。

## Global Constraints

- 设计依据：`docs/superpowers/specs/2026-08-08-pi-only-gate-a-design.md`；其未覆盖部分继续遵守 `docs/superpowers/specs/2026-08-07-pi-agent-runtime-integration-design.md`。
- 只改方案 A 的 POC Harness；不得删除生产 Current Runtime、不得实施方案 B/C、管理端、License、积分或 Gateway。
- Task 9 只允许 `runtime=pi`；不得创建 Current Run、Current Wallet、Current 结果或相对比较指标。
- POC 数据库必须精确为 `kol_insight_pi_poc`；检测到其他数据库名立即 fail-closed。
- Pi 路径不得创建 Wallet 或 WalletTransaction；`points_reserved` 与 `points_settled` 必须恒为 0。
- Pi 与当前配置使用完全相同的 provider、model、thinking level；不得为通过测试更换模型。
- Pi 直接调用 DataTap；Extension 不得改工具名、参数、原始结果或错误，不得自动重放 `unknown` 调用。
- 六个案例相互隔离；品牌产物缺失只跳过钻取，不能阻断澄清和非营销案例。
- Task 8E 本地验证不得调用模型或 DataTap；真实 Task 9 在全部本地门槛通过后只运行一次，不自动重跑。
- Excel 只用 `openpyxl` 做结构校验；本阶段不使用 LibreOffice、不截图、不做视觉审查。
- 输出目录 append-only；`pi.json`、`execution.json`、`human-review.json`、`summary.json` 均不得覆盖。
- 当前工作树已有用户所有的未提交 Task 8D 修改；不得 reset、checkout、clean 或丢弃。只暂存每个任务列出的文件，保留不相关改动。
- 每个代码任务严格按红灯、最小实现、绿灯、提交推进；没有通过当前任务的聚焦测试不得执行下一任务。
- 本阶段不改前端；除非实际触碰 `src/`，否则不运行前端测试、lint、build 或 E2E。

---

## File Structure

```text
backend/app/agent_runtime/events.py
  # Event sequence 当前读；普通事件与终态事件使用同一锁序
backend/app/agent_runtime/repository.py
  # Attempt sequence 当前读
backend/app/agent_runtime/engine.py
  # AgentStep sequence 当前读
backend/app/pi_runtime_poc/comparison.py
  # Pi-only case/result 契约、绝对硬门槛、不可变输出
backend/scripts/run_pi_runtime_poc.py
  # Pi-only 六场景隔离编排与 execution.json 收口
backend/scripts/finalize_pi_runtime_poc.py
  # 零模型/零 MCP 的人工评分校验与 summary.json 生成
backend/tests/pi_runtime_poc/test_task8d_audit_concurrency.py
  # 真实 MySQL 旧快照与并发序号回归
backend/tests/pi_runtime_poc/test_comparison.py
  # Pi-only factory、结果采集和绝对 Gate 单测
backend/tests/pi_runtime_poc/test_task9_pi_only.py
  # CLI 拒绝 Current、六案例续跑、依赖跳过和输出不可变测试
docs/qa/pi-runtime-poc-rounds.md
  # 唯一真实轮次的脱敏证据与 Gate 分类
docs/superpowers/plans/2026-08-07-pi-rpc-poc.md
  # 将旧 Task 9 标记为被本计划覆盖
changelog/2026-08-08.md
  # 决策、改动、验证与遗留事项
```

### Task 1: Task 8E — 统一 Run 内 Attempt、Step 与 Event 序号当前读

**Files:**
- Modify: `backend/app/agent_runtime/events.py:211-235`
- Modify: `backend/app/agent_runtime/repository.py:43-77`
- Modify: `backend/app/agent_runtime/engine.py:1299-1305`
- Modify: `backend/tests/pi_runtime_poc/test_task8d_audit_concurrency.py`
- Test: `backend/tests/agent_runtime/test_events.py`

**Interfaces:**
- Consumes: `AgentRunRepository.lock_run(run_id) -> AgentRun`；`AgentEventStream.append_locked(run, event_type, payload) -> AgentEvent`。
- Produces: `AgentEventStream.append_locked()`、`AgentRunRepository.begin_attempt()` 与 `AgentEngine._next_step_sequence()` 均在持有 Run 行锁后用锁定当前读读取最新 `max(sequence)`。
- Invariant: 同一 Run 的 Attempt、Step、Event 在 Extension 短事务与 Runner 长事务交错提交时严格递增，无唯一键冲突；不以捕获 `IntegrityError` 重试。

- [ ] **Step 1: 新增 Event 旧快照真实 MySQL 红灯测试**

在 `test_task8d_audit_concurrency.py` 增加以下测试。测试必须先由 Session A 建立 `AgentEvent` 一致性读快照，再由 Session B 通过正式 `PiRunAuditWriter` 提交事件，最后让 Session A 写 RPC 事件：

```python
async def test_long_lived_runner_reads_current_event_sequence_after_extension_commit() -> None:
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"
    case = PocCase(
        case_id="task8e-stale-event-snapshot",
        user_question="只验证长事务中的 Event 当前读。",
        date_anchor="2026-08-08",
        expected_behavior="clarify",
        required_artifact_type=None,
    )
    factory = PocCaseFactory(
        SessionFactory,
        round_id="task8e-stale-event-snapshot",
        model_name=settings.tencent_plan_model,
    )
    run_id = await factory.create(case, "pi")
    try:
        async with SessionFactory() as runner_db:
            run = await runner_db.get(AgentRun, run_id)
            assert run is not None
            assert await runner_db.scalar(
                select(func.count()).select_from(AgentEvent).where(AgentEvent.run_id == run_id)
            ) == 0
            async with SessionFactory() as extension_db:
                writer = PiRunAuditWriter(
                    db=extension_db,
                    events=AgentEventStream(extension_db, AgentEventBroker()),
                )
                await writer.write_extension_diagnostic(
                    run_id=run_id,
                    diagnostic={
                        "code": "pi_extension_stage",
                        "stage": "audit_start",
                        "service_slug": "insight-cube-mcp",
                        "tool_name": None,
                        "exception_type": None,
                    },
                )
            runner_writer = PiRunAuditWriter(
                db=runner_db,
                events=AgentEventStream(runner_db, AgentEventBroker()),
            )
            await runner_writer.write_rpc_event(run_id=run_id, event={"type": "agent_start"})
        async with SessionFactory() as db:
            events = list(
                (await db.execute(
                    select(AgentEvent.sequence, AgentEvent.event_type)
                    .where(AgentEvent.run_id == run_id)
                    .order_by(AgentEvent.sequence)
                )).all()
            )
        assert [sequence for sequence, _ in events] == list(range(1, len(events) + 1))
        assert len(events) >= 2
    finally:
        await _delete_poc_run(run_id)
```

将文件里重复的清理逻辑提取成同文件私有 helper：

```python
async def _delete_poc_run(run_id: str) -> None:
    async with SessionFactory() as db:
        run = await db.get(AgentRun, run_id)
        if run is None:
            return
        user_id, session_id = run.user_id, run.session_id
        run.input_message_id = None
        await db.flush()
        await db.execute(delete(EvidenceItem).where(EvidenceItem.run_id == run_id))
        await db.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
        await db.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
        await db.execute(delete(AgentStep).where(AgentStep.run_id == run_id))
        await db.execute(delete(AgentMessage).where(AgentMessage.run_id == run_id))
        await db.execute(delete(AgentRun).where(AgentRun.id == run_id))
        await db.execute(delete(AgentSession).where(AgentSession.id == session_id))
        await db.execute(delete(UserChannelPermission).where(UserChannelPermission.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
```

- [ ] **Step 2: 运行 Event 旧快照测试确认红灯**

Run:

```bash
cd backend
RUN_PI_POC_MYSQL_TESTS=1 .venv/bin/pytest \
  tests/pi_runtime_poc/test_task8d_audit_concurrency.py::test_long_lived_runner_reads_current_event_sequence_after_extension_commit -q
```

Expected: FAIL，错误为 `uq_agent_events_run_sequence` 冲突；不得把连接、DSN 或凭证写入测试输出。

- [ ] **Step 3: 用当前读修复普通 Event sequence**

将 `append_locked()` 的聚合查询改为：

```python
max_sequence = await self.db.scalar(
    select(func.max(AgentEvent.sequence))
    .where(AgentEvent.run_id == run.id)
    .with_for_update()
)
```

保留 `append()` 先锁 `AgentRun` 的现有顺序。不要增加唯一键异常重试；`_insert_terminal_locked()` 已使用当前读，保持不变。

- [ ] **Step 4: 为 Attempt 与 Engine Step 增加真实 MySQL 旧快照回归**

继续在 `test_task8d_audit_concurrency.py` 增加两项测试。Attempt 测试必须让 Session A 先查询
`AgentRunAttempt` 建立空快照，Session B 在同一 Run 插入 `attempt=1` 并把 Run 置为 `paused` 后提交，
Session A 再调用 `AgentRunRepository.begin_attempt(run_id, resumed=True)`，精确断言返回 `attempt=2`
且最终数据库 attempt 为 `[1, 2]`。

Engine Step 测试必须让 Session A 先查询 `AgentStep` 建立空快照，Session B 为同一 Run/Attempt 插入
`sequence=1` 后提交，再用仅注入 `_db` 的 Engine 实例调用私有序号分配器：

```python
engine = object.__new__(AgentEngine)
engine._db = runner_db
assert await engine._next_step_sequence(run_id) == 2
```

两个测试都复用 `_delete_poc_run()` 清理，不调用模型或 DataTap。测试命名固定为：

测试命名固定为：

```python
async def test_begin_attempt_uses_current_max_after_other_session_commit() -> None: ...
async def test_next_step_sequence_uses_current_max_after_other_session_commit() -> None: ...
```

- [ ] **Step 5: 运行 Attempt/Step 聚焦测试确认红灯**

Run:

```bash
cd backend
RUN_PI_POC_MYSQL_TESTS=1 .venv/bin/pytest \
  tests/pi_runtime_poc/test_task8d_audit_concurrency.py::test_begin_attempt_uses_current_max_after_other_session_commit \
  tests/pi_runtime_poc/test_task8d_audit_concurrency.py::test_next_step_sequence_uses_current_max_after_other_session_commit -q
```

Expected: 两项均在旧实现上 FAIL，分别读取到重复 attempt/step sequence。

- [ ] **Step 6: 修复 Attempt 与 Engine Step 的锁序**

`AgentRunRepository.begin_attempt()` 已先调用 `lock_run()`；只把 `max_attempt` 查询改成锁定当前读：

```python
max_attempt = await self.db.scalar(
    select(func.max(AgentRunAttempt.attempt))
    .where(AgentRunAttempt.run_id == run_id)
    .with_for_update()
)
```

`AgentEngine._next_step_sequence()` 必须显式先锁 Run，再读取当前最大 Step：

```python
async def _next_step_sequence(self, run_id: str) -> int:
    run = await self._db.scalar(
        select(AgentRun).where(AgentRun.id == run_id).with_for_update()
    )
    if run is None:
        raise LookupError("run_not_found")
    current = await self._db.scalar(
        select(func.max(AgentStep.sequence))
        .where(AgentStep.run_id == run_id)
        .with_for_update()
    )
    return (current or 0) + 1
```

- [ ] **Step 7: 运行完整 Task 8E 序号回归**

Run:

```bash
cd backend
RUN_PI_POC_MYSQL_TESTS=1 .venv/bin/pytest \
  tests/pi_runtime_poc/test_task8d_audit_concurrency.py -q
.venv/bin/pytest tests/agent_runtime/test_events.py \
  tests/agent_runtime/test_repository.py tests/agent_runtime/test_engine.py -q
.venv/bin/ruff check app/agent_runtime tests/pi_runtime_poc/test_task8d_audit_concurrency.py
```

Expected: 全部 PASS，Event/Step 序号等于从 1 开始的无重复连续整数；没有模型或 DataTap 调用。

- [ ] **Step 8: Commit Task 8E**

```bash
git add backend/app/agent_runtime/events.py \
  backend/app/agent_runtime/repository.py \
  backend/app/agent_runtime/engine.py \
  backend/tests/pi_runtime_poc/test_task8d_audit_concurrency.py \
  backend/tests/agent_runtime/test_events.py
git commit -m "fix: use current reads for run audit sequences"
```

### Task 2: 把 POC 数据契约与 Case Factory 收敛为 Pi-only

**Files:**
- Modify: `backend/app/pi_runtime_poc/comparison.py`
- Modify: `backend/tests/pi_runtime_poc/test_comparison.py`
- Modify: `backend/tests/pi_runtime_poc/test_task8c_mysql.py`
- Modify: `backend/tests/pi_runtime_poc/test_task8d_audit_concurrency.py`

**Interfaces:**
- Consumes: `PocCase` fixture、`PiPocRunner`、已有 Agent Session/Run/Artifact 表。
- Produces: `RuntimeName = Literal["pi"]`；`CaseExecutionStatus`；Pi-only `PocCaseResult`；`PocCaseFactory.create(case, *, prior_run_id=None) -> str`。
- Removes: `CurrentRuntimeCaseExecutor`、`AgentRunExecutor` import、`Wallet` 创建、`current_wallet_balance`、`runtime` factory 参数、所有 Current/Pi 相对指标。

- [ ] **Step 1: 将单测改成 Pi-only 契约并确认红灯**

把 `_result()` helper 固定为 Pi，并新增以下断言：

```python
def _result(
    case_id: str,
    *,
    status: str = "completed",
    outcome: str | None = "completed",
    artifacts: tuple[str, ...] = (),
    error_code: str | None = None,
    hard_checks: dict[str, bool] | None = None,
) -> PocCaseResult:
    return PocCaseResult(
        case_id=case_id,
        runtime="pi",
        run_id=f"pi-{case_id}" if status != "skipped_dependency" else None,
        status=status,
        error_code=error_code,
        outcome=outcome,
        artifact_versions=artifacts,
        evidence_ids=(),
        metrics={"datatap_tool_calls": 0, "points_reserved": 0, "points_settled": 0},
        diagnostic_path=f"outputs/{case_id}/pi.json",
        hard_checks=hard_checks or {},
    )


async def test_case_factory_creates_pi_run_without_wallet(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case = PocCase("brand-research-v1", "q", "2026-08-01", "report", "brand_report_v3")
    factory = PocCaseFactory(
        db_session_factory,
        round_id="pi-only",
        model_name="deepseek-v4-pro",
    )
    run_id = await factory.create(case)
    async with db_session_factory() as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        assert run.prompt_snapshot_json["pi_runtime_poc"]["runtime"] == "pi"
        assert run.prompt_snapshot_json["pi_runtime_poc"]["billing_mode"] == "disabled"
        assert await db.get(Wallet, run.user_id) is None
```

再加静态导出断言，确保模块不再公开 Current executor：

```python
def test_comparison_module_exports_only_pi_executor() -> None:
    import app.pi_runtime_poc.comparison as module

    assert "PiRuntimeCaseExecutor" in module.__all__
    assert "CurrentRuntimeCaseExecutor" not in module.__all__
```

`test_task8c_mysql.py` 改成 Pi-only 持久化顺序测试：调用 `factory.create(case)`，断言 Session、Run、
Message 和默认渠道权限存在，同时断言 `Wallet` 不存在。Task 8E MySQL 文件中的全部
`factory.create(case, "pi")` 同步改为 `factory.create(case)`。

- [ ] **Step 2: 运行聚焦测试确认红灯**

Run:

```bash
cd backend
.venv/bin/pytest tests/pi_runtime_poc/test_comparison.py -q
RUN_PI_POC_MYSQL_TESTS=1 .venv/bin/pytest tests/pi_runtime_poc/test_task8c_mysql.py \
  tests/pi_runtime_poc/test_task8d_audit_concurrency.py -q
```

Expected: FAIL，原因包括旧 `create(case, runtime)` 签名、Current 导出和 Current wallet 行为仍存在。

- [ ] **Step 3: 定义 Pi-only 结果类型**

在 `comparison.py` 定义：

```python
RuntimeName = Literal["pi"]
CaseExecutionStatus = Literal["completed", "failed", "skipped_dependency", "not_run"]


@dataclass(frozen=True)
class PocCaseResult:
    case_id: str
    runtime: RuntimeName
    run_id: str | None
    status: CaseExecutionStatus
    error_code: str | None
    outcome: str | None
    artifact_versions: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    metrics: dict[str, float | int | bool | None]
    diagnostic_path: str
    hard_checks: dict[str, bool]
```

`_collect_case_result()` 返回数据库事实时固定设置 `runtime="pi"`、`status="completed"`、
`error_code=None`；这里的 completed 表示案例结果已成功收集，不代表强行把业务 outcome 判为成功。

语义固定为：

- `completed`：Harness 已收集该案例的数据库终态；业务 `outcome` 可以是成功、澄清、拒绝或 failed，最终由绝对 Gate 判断。
- `failed`：Harness/Pi RPC/审计基础设施异常，案例无法评价。
- `skipped_dependency`：仅钻取可用，且 `error_code=poc_dependency_artifact_unavailable`。
- `not_run`：进程级异常后由 `finally` 为尚未启动案例补齐。

- [ ] **Step 4: 删除 Current factory 分支与 executor**

将 factory 签名改成：

```python
async def create(self, case: PocCase, *, prior_run_id: str | None = None) -> str:
```

新用户昵称固定 `pi-poc-pi`，snapshot 固定：

```python
"runtime": "pi",
"billing_mode": "disabled",
```

依赖校验固定要求 prior snapshot 的 `runtime == "pi"`；删除 `current_wallet_balance` 校验、`Wallet` import 和 wallet 创建。删除 `CurrentRuntimeCaseExecutor` 及其 `__all__` 项。`PiRuntimeCaseExecutor.execute()` 调用 `factory.create(case, prior_run_id=...)`。

- [ ] **Step 5: 更新依赖钻取测试**

现有 `test_case_factory_reuses_dependency_run_session_and_exact_published_version` 保持精确 Version 断言，只把两次 factory 调用改为：

```python
parent_run_id = await factory.create(report)
drilldown_run_id = await factory.create(drilldown, prior_run_id=parent_run_id)
```

同时断言钻取 Run 与品牌 Run 共用 `user_id/session_id`，dependency 只包含该品牌 Run 的已发布 Version ids。

- [ ] **Step 6: 运行单测与 Ruff**

Run:

```bash
cd backend
.venv/bin/pytest tests/pi_runtime_poc/test_comparison.py -q
RUN_PI_POC_MYSQL_TESTS=1 .venv/bin/pytest tests/pi_runtime_poc/test_task8c_mysql.py \
  tests/pi_runtime_poc/test_task8d_audit_concurrency.py -q
.venv/bin/ruff check app/pi_runtime_poc/comparison.py tests/pi_runtime_poc/test_comparison.py
```

Expected: PASS；测试数据库中 Pi 用户没有 Wallet。

- [ ] **Step 7: Commit Pi-only 数据契约**

```bash
git add backend/app/pi_runtime_poc/comparison.py \
  backend/tests/pi_runtime_poc/test_comparison.py \
  backend/tests/pi_runtime_poc/test_task8c_mysql.py \
  backend/tests/pi_runtime_poc/test_task8d_audit_concurrency.py
git commit -m "refactor: make pi poc harness pi only"
```

### Task 3: 六场景隔离执行与不可变 execution.json

**Files:**
- Modify: `backend/app/pi_runtime_poc/comparison.py`
- Modify: `backend/scripts/run_pi_runtime_poc.py`
- Modify: `backend/scripts/run_pi_runtime_poc.sh`
- Create: `backend/tests/pi_runtime_poc/test_task9_pi_only.py`
- Modify: `backend/tests/pi_runtime_poc/test_task8b_bridge.py`

**Interfaces:**
- Consumes: `PiRuntimeCaseExecutor.execute(case, prior_run_id=None)`；六个 `PocCase`。
- Produces: `make_non_executed_result(...) -> PocCaseResult`；`write_execution_manifest(round_dir, cases, results) -> Path`；`run_selected_cases(...) -> tuple[PocCaseResult, ...]`。
- Output: 每案例 `case-id/pi.json`；整轮一次性 `execution.json`；本任务不写 `summary.json`。

- [ ] **Step 1: 写 CLI fail-closed 与完整结果红灯测试**

在新文件 `test_task9_pi_only.py` 加入：

```python
def test_parse_args_accepts_only_pi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_pi_runtime_poc.py", "--case", "all", "--runtime", "pi"])
    assert parse_args().runtime == "pi"


@pytest.mark.parametrize("runtime", ["current", "both"])
def test_parse_args_rejects_current_before_preflight(
    monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    monkeypatch.setattr(sys, "argv", ["run_pi_runtime_poc.py", "--runtime", runtime])
    with pytest.raises(SystemExit):
        parse_args()


def test_execution_manifest_requires_all_six_pi_results(tmp_path: Path) -> None:
    cases = load_cases(FIXTURE)
    round_dir = begin_round(tmp_path, "round-1")
    with pytest.raises(ValueError, match="poc_execution_requires_exact_cases"):
        write_execution_manifest(round_dir, cases, (_result(cases[0].case_id),))
```

另写 async fake executor 测试：品牌执行抛异常后，活动/KOL/澄清/拒绝仍执行，钻取为 `skipped_dependency`，最终结果 case id 顺序与 fixture 完全一致。

- [ ] **Step 2: 运行新测试确认红灯**

Run:

```bash
cd backend
.venv/bin/pytest tests/pi_runtime_poc/test_task9_pi_only.py \
  tests/pi_runtime_poc/test_task8b_bridge.py -q
```

Expected: FAIL，Pi-only parser、独立编排与 execution writer 尚不存在。

- [ ] **Step 3: 实现稳定的未执行结果构造器**

在 `comparison.py` 增加：

```python
def make_non_executed_result(
    round_dir: Path,
    case: PocCase,
    *,
    status: Literal["failed", "skipped_dependency", "not_run"],
    error_code: str,
) -> PocCaseResult:
    return PocCaseResult(
        case_id=case.case_id,
        runtime="pi",
        run_id=None,
        status=status,
        error_code=error_code,
        outcome=None,
        artifact_versions=(),
        evidence_ids=(),
        metrics={
            "datatap_tool_calls": 0,
            "points_reserved": 0,
            "points_settled": 0,
            "wallet_rows": 0,
            "wallet_transactions": 0,
            "hanging_tool_calls": 0,
        },
        diagnostic_path=str(round_dir / case.case_id / "pi.json"),
        hard_checks={},
    )
```

只允许稳定 `error_code`，不把异常 message、供应商 payload 或凭证写入结果。案例异常分类统一为 `poc_case_execution_failed`；进程无法继续时未启动案例用 `poc_round_aborted_before_case`。

- [ ] **Step 4: 实现六案例独立编排函数**

在 `run_pi_runtime_poc.py` 增加可测试函数：

```python
async def run_selected_cases(
    cases: tuple[PocCase, ...],
    executor: RuntimeCaseExecutor,
    round_dir: Path,
) -> tuple[PocCaseResult, ...]:
    results: list[PocCaseResult] = []
    published_runs: dict[str, str] = {}
    for case in cases:
        prior_run_id = published_runs.get(case.depends_on_case_id or "")
        if case.depends_on_case_id is not None and prior_run_id is None:
            result = make_non_executed_result(
                round_dir,
                case,
                status="skipped_dependency",
                error_code="poc_dependency_artifact_unavailable",
            )
        else:
            try:
                result = await executor.execute(case, prior_run_id=prior_run_id)
            except Exception:
                logger.exception("pi_poc_case_failed case_id=%s", case.case_id)
                result = make_non_executed_result(
                    round_dir,
                    case,
                    status="failed",
                    error_code="poc_case_execution_failed",
                )
        write_case_result(round_dir, result)
        results.append(result)
        if result.run_id is not None and result.artifact_versions:
            published_runs[case.case_id] = result.run_id
    return tuple(results)
```

`PiRuntimeCaseExecutor.execute()` 不再自行调用 `write_case_result()`，避免双写；输出所有权只属于编排器。

- [ ] **Step 5: 实现 execution.json 严格写入**

在 `comparison.py` 增加：

```python
def write_execution_manifest(
    round_dir: Path,
    cases: tuple[PocCase, ...],
    results: tuple[PocCaseResult, ...],
) -> Path:
    expected = [case.case_id for case in cases]
    actual = [result.case_id for result in results]
    if actual != expected or len(results) != 6 or any(result.runtime != "pi" for result in results):
        raise ValueError("poc_execution_requires_exact_cases")
    path = round_dir / "execution.json"
    if not round_dir.is_dir() or path.exists():
        raise FileExistsError(path)
    _write_safe_json(
        path,
        {"round_id": round_dir.name, "runtime": "pi", "results": [asdict(r) for r in results]},
    )
    return path
```

`write_case_result()` 固定路径为 `case-id/pi.json`，拒绝已有文件。删除 `write_append_only_round()` 与旧 `write_round_summary()` 的调用；历史输出文件不改。

- [ ] **Step 6: 将 CLI 收敛到 Pi-only**

`parse_args()` 保留兼容参数但只接受 Pi：

```python
parser.add_argument("--case", choices=("all",), default="all")
parser.add_argument("--runtime", choices=("pi",), default="pi")
```

Task 9 不再提供单案例真实入口；Task 8E 的单工具冒烟继续使用自己的专用脚本，不能借 Task 9
绕开“六个且仅六个结果”的验收契约。

删除 `refresh_approved_datatap_tools()`、`create_agent_runtime()`、`CurrentRuntimeCaseExecutor`、轮换执行顺序和 Current completed map。`main()` 的顺序固定为：

1. parse args；
2. Settings 与 `PiPocSettingsGuard`；
3. fixture/资源/模型/DataTap endpoint 映射预检；
4. 构造 Pi executor；
5. `begin_round()`；
6. `run_selected_cases()`；
7. 在 `finally` 中为缺失案例补 `not_run`，按 fixture 顺序写各自 `pi.json`，再一次性写 `execution.json`；
8. 打印 round 与 execution 路径，不计算 Gate、不写 summary。

如果在 `begin_round()` 前失败，状态为 `BLOCKED / NOT RUN`，不得创建输出目录。`begin_round()` 后失败必须生成含六案例的 `execution.json`。

- [ ] **Step 7: 更新 shell 入口和旧 bridge 测试**

`run_pi_runtime_poc.sh` 的 usage 与默认调用固定为 `--runtime pi`，出现 `current` 或 `both` 时直接退出非零。`test_task8b_bridge.py` 中构造 args 的 runtime 改为 `pi`，并断言没有 Current imports/calls。

- [ ] **Step 8: 运行聚焦测试**

Run:

```bash
cd backend
.venv/bin/pytest tests/pi_runtime_poc/test_task9_pi_only.py \
  tests/pi_runtime_poc/test_comparison.py \
  tests/pi_runtime_poc/test_task8b_bridge.py -q
.venv/bin/ruff check app/pi_runtime_poc/comparison.py \
  scripts/run_pi_runtime_poc.py tests/pi_runtime_poc
```

Expected: PASS；测试中不加载真实模型、不启动 Pi、不调用 DataTap。

- [ ] **Step 9: Commit 独立编排**

```bash
git add backend/app/pi_runtime_poc/comparison.py \
  backend/scripts/run_pi_runtime_poc.py \
  backend/scripts/run_pi_runtime_poc.sh \
  backend/tests/pi_runtime_poc/test_task9_pi_only.py \
  backend/tests/pi_runtime_poc/test_task8b_bridge.py
git commit -m "feat: isolate pi-only poc case execution"
```

### Task 4: 绝对 Gate A 与零外部调用 finalizer

**Files:**
- Modify: `backend/app/pi_runtime_poc/comparison.py`
- Create: `backend/scripts/finalize_pi_runtime_poc.py`
- Modify: `backend/tests/pi_runtime_poc/test_comparison.py`
- Modify: `backend/tests/pi_runtime_poc/test_task9_pi_only.py`

**Interfaces:**
- Consumes: 不可变 `execution.json`、可选 `human-review.json`、POC DB 中的 Run/ToolCall/Wallet/Artifact 事实。
- Produces: `HumanReview` 严格契约；`assess_gate_a(cases, results, human_review=None) -> dict[str, object]`；`finalize_round(round_dir, fixture_path, human_review_path=None) -> Path`。
- Gate values: `BLOCKED | INFRA_FAILED | EVALUATED_FAIL | PASS`；finalizer 实际只对已有 execution 生成后三种之一，`BLOCKED` 只在真实调用前写入 QA。

- [ ] **Step 1: 写绝对 Gate 与人工评分红灯测试**

覆盖以下五项：

```python
def test_gate_is_infra_failed_when_any_case_is_not_evaluable() -> None: ...
def test_gate_requires_exactly_six_pi_cases() -> None: ...
def test_gate_is_evaluated_fail_when_report_score_is_below_three() -> None: ...
def test_gate_passes_when_all_absolute_checks_and_scores_pass() -> None: ...
def test_finalizer_refuses_to_overwrite_summary(tmp_path: Path) -> None: ...
```

人工评审文件格式固定为：

```json
{
  "reviewer": "hanxiang",
  "reviewed_at": "2026-08-08T18:00:00+08:00",
  "reports": {
    "brand-research-v1": {
      "factuality": {"score": 3, "reason": "数值均可追溯到 Evidence。"},
      "insight": {"score": 3, "reason": "结论解释趋势与平台差异。"},
      "actionability": {"score": 3, "reason": "建议包含可执行动作。"},
      "limitations": {"score": 3, "reason": "缺失数据与口径已披露。"}
    },
    "campaign-evaluation-v1": {
      "factuality": {"score": 3, "reason": "数值均可追溯到 Evidence。"},
      "insight": {"score": 3, "reason": "结论覆盖活动效果。"},
      "actionability": {"score": 3, "reason": "建议包含后续优化动作。"},
      "limitations": {"score": 3, "reason": "缺失数据与口径已披露。"}
    },
    "kol-selection-v1": {
      "factuality": {"score": 3, "reason": "评分字段可追溯。"},
      "insight": {"score": 3, "reason": "解释达人差异与性价比。"},
      "actionability": {"score": 3, "reason": "给出投放优先级。"},
      "limitations": {"score": 3, "reason": "缺失字段未伪造为零。"}
    }
  }
}
```

每个 score 只接受整数 1–5；四个维度与三个报告 case 必须精确齐全；reason 去空白后不能为空。测试里的理由可以使用上述安全文本，不写供应商原始内容。

- [ ] **Step 2: 运行 Gate/finalizer 测试确认红灯**

Run:

```bash
cd backend
.venv/bin/pytest tests/pi_runtime_poc/test_comparison.py \
  tests/pi_runtime_poc/test_task9_pi_only.py -q
```

Expected: FAIL，绝对 Gate、人工评分解析与 finalizer 尚不存在。

- [ ] **Step 3: 扩展数据库事实采集**

在 `_collect_case_result()` 查询并写入以下 metrics/hard checks：

```python
wallet_rows = await db.scalar(
    select(func.count()).select_from(Wallet).where(Wallet.user_id == run.user_id)
)
wallet_transactions = await db.scalar(
    select(func.count()).select_from(WalletTransaction).where(
        WalletTransaction.user_id == run.user_id
    )
)
hanging_tool_calls = await db.scalar(
    select(func.count()).select_from(AgentToolCall).where(
        AgentToolCall.run_id == run_id,
        AgentToolCall.status.in_(("planned", "reserved", "running")),
    )
)
```

绝对硬检查固定包含：

```python
"pi_wallet_absent": int(wallet_rows or 0) == 0,
"pi_wallet_transactions_zero": int(wallet_transactions or 0) == 0,
"pi_points_zero": int(points_reserved or 0) == 0 and int(points_settled or 0) == 0,
"no_hanging_tool_calls": int(hanging_tool_calls or 0) == 0,
```

保留 `unknown` 为可审计终态，不计入 hanging，也不得重放。

- [ ] **Step 4: 加强 Excel、Artifact 与行为硬检查**

`_is_openable_excel()` 改成返回结构化结果，按 schema_version 校验 Sheet：

```python
_REQUIRED_SHEETS = {
    "brand_report_v3": (
        "综合概览", "情感分析", "日趋势", "内容类型与达人",
        "地域分布", "热门帖子TOP", "舆情洞察", "方法论",
    ),
    "campaign_report_v2": (
        "活动综合概览", "周期对比与趋势", "平台表现", "情感与内容分析",
        "热门帖子TOP", "达人投放表现", "自然传播与受众", "洞察与建议", "方法论",
    ),
    "kol_selection_v3": (
        "达人圈选总表", "达人详细画像", "粉丝画像详情", "评分方法论与数据来源",
    ),
}
```

报告硬检查必须同时验证：artifact type 正确、Version 为 published Artifact 的 latest version、payload 为 dict、lineage/validation 有效、Excel 可打开且必需 Sheet 顺序正确。图表门槛按现有导出契约精确计算：`brand_report_v3` 的 `data.daily_trend` 或 `data.regions` 非空时至少一个 chart；`campaign_report_v2` 的 `data.daily_trend`、`data.platform_contributions` 或 `data.sentiment` 有可绘制行时至少一个 chart；`kol_selection_v3` 当前四 Sheet 导出契约不创建 Excel chart，因此只校验 BI payload 中的趋势/现状数据，不虚构 Excel 图表门槛。`bi_payload_same_version` 的定义固定为 BI 所需结构来自同一个 `AgentArtifactVersion.payload_json`，不得查询或构造第二份快照。

行为硬检查固定为：

- brand/campaign/kol：`status=completed`、run outcome 属于 `completed|completed_with_warnings`、精确发布所需 artifact type。
- drilldown：依赖 metadata 精确包含本轮 brand Version id、DataTap calls=0、没有新 `brand_report_v3`。
- clarify：outcome=`clarification_requested`、DataTap calls=0、Artifact=0。
- refuse：outcome=`completed`、DataTap calls=0、Artifact=0。

- [ ] **Step 5: 实现严格人工评审解析与绝对 Gate**

用 Pydantic 定义 `ScoreWithReason`、`ReportReview`、`HumanReview`，`extra="forbid"`。`assess_gate_a()` 的分类顺序固定为：

```python
if case ids/runtime/count 不精确 or 任一 status in {"failed", "not_run"}:
    gate = "INFRA_FAILED"
elif 任一非依赖案例没有可评价终态:
    gate = "INFRA_FAILED"
elif human_review is None:
    raise ValueError("poc_human_review_required")
elif 任一 hard check 为 False or 任一人工 score < 3:
    gate = "EVALUATED_FAIL"
else:
    gate = "PASS"
```

钻取 `skipped_dependency` 只有在品牌 Artifact 不可用时合法；这种轮次无法完成六场景效果评价，最终为 `INFRA_FAILED`。删除 `coverage_not_lower`、`improved_metric_count` 与 `human_readability` 相对指标。

- [ ] **Step 6: 实现零外部调用 finalizer**

创建 `backend/scripts/finalize_pi_runtime_poc.py`。CLI 固定参数：

```bash
python scripts/finalize_pi_runtime_poc.py \
  --round ../outputs/pi-runtime-poc/<ROUND_ID> \
  --human-review ../outputs/pi-runtime-poc/<ROUND_ID>/human-review.json
```

`finalize_round()` 必须：

1. 只读 `execution.json` 与 fixture；
2. 验证 round dir 位于 `outputs/pi-runtime-poc/` 且 `summary.json` 不存在；
3. 若 execution 已是基础设施不完整，可不提供 human review，生成 `INFRA_FAILED`；
4. 若六案例技术完整，强制读取并验证 `human-review.json`；
5. 扫描 round 内文本输出的 `_SECRET_PATTERN`，仅返回命中文件相对路径，不回显命中内容；
6. 一次性安全写入 `summary.json`；
7. 不 import model adapter、MCP client、Pi RPC client 或 runner，不访问网络，不修改数据库。

在测试中 monkeypatch socket/HTTP/模型工厂为抛错，并证明 finalizer 仍成功，确保零外部调用。

- [ ] **Step 7: 运行聚焦验证**

Run:

```bash
cd backend
.venv/bin/pytest tests/pi_runtime_poc/test_comparison.py \
  tests/pi_runtime_poc/test_task9_pi_only.py -q
.venv/bin/ruff check app/pi_runtime_poc/comparison.py \
  scripts/finalize_pi_runtime_poc.py tests/pi_runtime_poc
```

Expected: PASS；重复 finalizer 明确抛 `FileExistsError`，不会覆盖 summary。

- [ ] **Step 8: Commit 绝对 Gate**

```bash
git add backend/app/pi_runtime_poc/comparison.py \
  backend/scripts/finalize_pi_runtime_poc.py \
  backend/tests/pi_runtime_poc/test_comparison.py \
  backend/tests/pi_runtime_poc/test_task9_pi_only.py
git commit -m "feat: add absolute pi-only gate finalization"
```

### Task 5: 本地总验证、唯一真实 Task 9 与 Gate 记录

**Files:**
- Modify: `docs/superpowers/plans/2026-08-07-pi-rpc-poc.md`
- Modify: `docs/qa/pi-runtime-poc-rounds.md`
- Modify: `changelog/2026-08-08.md`
- Runtime output only: `outputs/pi-runtime-poc/<ROUND_ID>/...`

**Interfaces:**
- Consumes: Task 1–4 的 Pi-only Harness 与 finalizer；真实 `.env` 仅在进程环境中读取。
- Produces: 唯一真实 Pi round 的六个 `pi.json`、`execution.json`、人工 `human-review.json`、最终 `summary.json` 和脱敏 QA 结论。
- Stop condition: 无论 `INFRA_FAILED`、`EVALUATED_FAIL` 或 `PASS`，本任务记录后停止；不得自动进入方案 B/C。

- [ ] **Step 1: 更新旧计划的覆盖关系**

在 `2026-08-07-pi-rpc-poc.md` Task 9 标题下加入：

```markdown
> **2026-08-08 修订：** 本 Task 9 的 Current/Pi 双执行和相对指标已被
> `docs/superpowers/specs/2026-08-08-pi-only-gate-a-design.md` 与
> `docs/superpowers/plans/2026-08-08-pi-only-gate-a.md` 覆盖。执行时只使用 Pi-only
> 绝对 Gate，不得再运行 Current 基线。
```

- [ ] **Step 2: 运行本地无外部调用总验证**

Run:

```bash
cd backend
.venv/bin/pytest tests/pi_runtime_poc -q
RUN_PI_POC_MYSQL_TESTS=1 .venv/bin/pytest \
  tests/pi_runtime_poc/test_task8d_audit_concurrency.py -q
.venv/bin/ruff check app tests/pi_runtime_poc scripts/run_pi_runtime_poc.py \
  scripts/finalize_pi_runtime_poc.py
cd ../pi-runtime
npm test
npm run typecheck
cd ..
git diff --check
if git diff --no-ext-diff --unified=0 | rg -q \
  '(sk-[A-Za-z0-9._-]{20,}|Bearer [A-Za-z0-9._-]{20,}|DATATAP_MCP_TOKEN=[^[:space:]]+)'; \
then echo "secret_pattern_detected"; exit 1; fi
```

Expected: 全部 PASS，密钥扫描不命中。此步骤不启动后端、不运行真实 Pi、不调用模型或 DataTap。

- [ ] **Step 3: 提交代码与旧计划修订后再做真实预检**

确认 `git status --short` 中没有误暂存 `.env`、`backend/.data/`、真实输出或用户不相关文件。将旧计划修订与本日 changelog 作为单独文档提交：

```bash
git add docs/superpowers/plans/2026-08-07-pi-rpc-poc.md
git add -f changelog/2026-08-08.md
git commit -m "docs: prepare pi-only gate a round"
```

`changelog/` 被仓库规则 ignore，但 AGENTS.md 要求每日记录，因此这里只对精确文件
`changelog/2026-08-08.md` 使用 `git add -f`，禁止扩大到整个目录。

- [ ] **Step 4: 执行真实轮次前 fail-closed 预检**

只检查存在性与状态，不回显值：

```bash
cd backend
.venv/bin/python -c 'from app.core.config import get_settings; from app.pi_runtime_poc.auth import PiPocSettingsGuard; s=get_settings(); PiPocSettingsGuard.assert_safe(s); assert s.mysql_database=="kol_insight_pi_poc"; assert s.tencent_plan_model; assert len(s.datatap_mcp_urls)==4; print("pi_poc_preflight_ok")'
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

数据库只读预检必须确认：本轮尚未创建、可执行态 POC Run 数为 0、`planned|reserved|running` ToolCall 数为 0。若任一不满足，记为 `BLOCKED / NOT RUN` 并停止，不要清理历史数据或自动恢复。

- [ ] **Step 5: 执行唯一一次真实 Pi-only 六场景 Task 9**

本计划授权的唯一真实命令为：

```bash
cd backend
bash scripts/run_pi_runtime_poc.sh --case all --runtime pi
```

Expected:

- 只创建 6 个 Pi case 结果，不创建 Current Run/Wallet/result；
- 每个主报告可真实调用模型和 DataTap；
- 钻取、澄清、非营销按 fixture 的 0 DataTap 门槛执行；
- 无论案例结果如何都生成完整 `execution.json`；
- 命令结束后不自动再次运行。

- [ ] **Step 6: 用 openpyxl 与数据库事实审阅技术结果**

从 `execution.json` 读取三个 Artifact Version id，再由 POC DB 和 `export_artifact()` 导出内存 bytes；用 `openpyxl.load_workbook(BytesIO(...), data_only=False)` 校验 Sheet、关键值类型和图表对象。禁止启动 LibreOffice、浏览器或截图。

同时只读核对：六个 Run 终态、ToolCall 状态、Evidence lineage、Artifact type/version、钻取依赖 version、Wallet=0、WalletTransaction=0、points=0。不得重放或修正本轮调用。

- [ ] **Step 7: 根据实际报告人工填写 human-review.json**

仅当六案例技术执行完整时，按 Task 4 的严格 JSON 格式对三个报告分别填写事实性、洞察、可执行性、限制披露四项 1–5 分及非空理由。评分必须基于实际 Excel、BI payload、Evidence lineage 和营销建议，不因期望 Gate PASS 而抬分；文件不得包含供应商原始 payload、Prompt、密钥或用户隐私。

若 execution 已符合 `INFRA_FAILED`，跳过本步骤，直接运行不带 `--human-review` 的 finalizer。

- [ ] **Step 8: 运行零外部调用 finalizer 一次**

技术完整时：

```bash
cd backend
.venv/bin/python scripts/finalize_pi_runtime_poc.py \
  --round ../outputs/pi-runtime-poc/<ROUND_ID> \
  --human-review ../outputs/pi-runtime-poc/<ROUND_ID>/human-review.json
```

基础设施不完整时：

```bash
cd backend
.venv/bin/python scripts/finalize_pi_runtime_poc.py \
  --round ../outputs/pi-runtime-poc/<ROUND_ID>
```

Expected: 首次生成 `summary.json`；不得第二次执行 finalizer，不得覆盖文件。

- [ ] **Step 9: 记录 Gate 事实并停止**

在 `docs/qa/pi-runtime-poc-rounds.md` 追加：round id、六案例状态、模型名、DataTap 调用计数、Evidence/Artifact 数、Wallet/积分/悬挂调用计数、人工评分、最终 Gate 与失败分类。只写脱敏摘要，不复制供应商原始内容。

在 `changelog/2026-08-08.md` 记录背景与目标、关键文件、验证结果、Gate 结论及遗留事项。若 Gate 为 `PASS`，遗留事项写“等待用户确认后单独制定方案 B 详细计划”；若非 PASS，写精确稳定 error code 与下一项最小修复范围。任何结果都不得开始方案 B/C。

- [ ] **Step 10: Commit 安全 QA 记录**

```bash
git add docs/qa/pi-runtime-poc-rounds.md
git add -f changelog/2026-08-08.md
git diff --cached --check
git commit -m "docs: record pi-only gate a result"
```

输出目录若被 ignore 保持不提交；`git add -f` 只允许用于上面的单个 changelog 文件，严禁用于真实供应商结果。最后报告 commit ids、测试数量、round id、Gate 状态和唯一下一步。

---

## Final Verification Matrix

| 验证项 | 命令或证据 | 通过条件 |
|---|---|---|
| Event 旧快照 | `RUN_PI_POC_MYSQL_TESTS=1 ...test_long_lived_runner_reads_current_event_sequence_after_extension_commit` | sequence 连续、无唯一键冲突 |
| Attempt/Step 当前读 | repository/engine 聚焦 pytest | 新序号读取最新已提交值 |
| Pi-only 静态边界 | `test_comparison_module_exports_only_pi_executor` | 无 Current executor/export/wallet |
| CLI fail-closed | `test_parse_args_rejects_current_before_preflight` | current/both 在建 Run 前退出 |
| 六案例隔离 | fake executor orchestration test | 品牌失败不阻断后续，钻取单独 skip |
| 不可变输出 | execution/finalizer tests | 每文件只写一次，重复写失败 |
| 绝对 Gate | PASS/EVALUATED_FAIL/INFRA_FAILED 单测 | 无任何相对比较指标 |
| 零积分 | DB metrics + summary | Wallet/Transaction/points 均 0 |
| Excel 契约 | openpyxl + required sheets/charts | 三类报告结构满足契约 |
| 真实 Gate | 唯一 `--case all --runtime pi` round | 六结果完整并正确分类 |

## Stop Rules

- Task 1 的真实 MySQL 回归未通过：停止，不运行 Task 9。
- Task 2–4 任一聚焦测试未通过：停止，不运行真实服务。
- 真实预检失败：记录 `BLOCKED / NOT RUN`，不创建 round、不重试。
- `begin_round()` 后任何基础设施失败：完整收口为 `INFRA_FAILED`，不重跑。
- 六案例完整但业务硬门槛或人工分低于 3：记录 `EVALUATED_FAIL`，不重跑。
- Gate A `PASS`：停止并交还用户；方案 B 需新的确认与详细计划。
