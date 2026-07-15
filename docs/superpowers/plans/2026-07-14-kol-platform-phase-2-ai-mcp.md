# KOL 智能选人系统第二阶段实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在第一阶段 FastAPI、MySQL、模拟认证和会话持久化之上，完成腾讯 Token Plan 规划与总结、五服务 DataTap MCP、成功调用实时扣费、可恢复任务、候选清单、收藏和版本化 BI 的完整纵向闭环。

**Architecture:** 后端控制 `plan -> execute -> summarize`，模型只负责规划和解释，Python 负责白名单、参数、任务、计费、归一化和恢复。所有执行状态、事件、调用证据、候选和报告持久化到 MySQL，并通过带认证的 fetch SSE 增量呈现；首期在 FastAPI 进程内异步执行，模块接口保持可迁移到 Worker。

**Tech Stack:** Python 3.11/3.12、FastAPI、Pydantic 2、SQLAlchemy 2 Async、Alembic、MySQL 8、OpenAI Python SDK 1.x、MCP Python SDK 1.x、React 19、TypeScript 5、Vite 6、Tailwind CSS 4、Lucide React、Recharts、pytest、Vitest、Playwright。

## Global Constraints

- API 前缀固定为 `/api/v1`；预期注册用户约 100 人，同时在线和同时执行任务不超过 10。
- 第二阶段运行单个 FastAPI/Uvicorn worker；在引入跨进程事件总线前不能横向启动多个 API worker。
- 模型协议固定为 `openai-completions`，Base URL 固定为 `https://api.lkeap.cloud.tencent.com/plan/v3`，模型固定为 `deepseek-v4-pro-202606`。
- 模型和 DataTap 凭据只能由未提交的运行环境注入，禁止进入源码、数据库、事件、日志、测试夹具和前端构建产物。
- DataTap 只允许 `insight-cube-mcp`、`social-grow-mcp`、`social-grow-content-mcp`、`aktools-mcp`、`bilibili-mcp`。
- `zhihu-mcp`、`toutiao-mcp`、`baidu-index-mcp`、`google-trends-mcp` 在配置、发现、注册表、路由和测试中都必须拒绝。
- 单任务最多规划 10 次 MCP 工具调用；每个明确成功并持久化的工具响应扣 10 积分，失败不扣费。
- 请求可能已发送但结果不确定时进入 `unknown`，不自动重放；观察期内保持预留，最终无法确认则释放。
- 客户端断线不取消后端任务；取消只停止未开始步骤，已发出的调用按真实结果处理。
- Planner 结构无效最多再生成一次；第二次仍无效以 `MODEL_PLAN_INVALID` 失败，且不调用 MCP。
- 模型只看到本地审核工具目录，不看到供应商密钥、端点、接入链接、原始工具提示或未经审核的新工具。
- 评分由后端确定性计算，总分 100：受众 25、内容 20、互动 20、预算 15、增长稳定性 10、品牌安全 10。
- UI 保持原型三栏、Indigo/Slate、Lucide、紧凑按钮和 Recharts 风格；中间增加“智能会话 / 候选清单 / 已收藏”。
- 自动化测试使用 fake model 和 fake MCP；没有单独授权时不得执行真实 DataTap 调用。
- 现有短信、微信和充值继续模拟或不可用，本计划不实现真实供应商。

---

## 文件结构

```text
backend/app/
├── core/
│   ├── config.py                         # 模型、MCP、任务与安全配置
│   └── redaction.py                      # 结构化日志脱敏
├── model/
│   ├── contracts.py                      # 模型请求、结果、流事件和错误
│   ├── prompts.py                        # planner_v1 / analyst_v1 / summary_v1
│   ├── adapter.py                        # ModelAdapter Protocol
│   ├── tencent_plan.py                   # OpenAI 兼容腾讯适配器
│   ├── fake.py                           # 确定性测试模型
│   └── dependencies.py                   # 进程级适配器实例
├── mcp_gateway/
│   ├── contracts.py                      # 服务、工具、调用结果和状态
│   ├── registry.py                       # 五服务本地审核目录
│   ├── validation.py                     # 输入输出和大小限制
│   ├── transport.py                      # Streamable HTTP 适配接口
│   ├── datatap.py                        # 固定端点、会话和调用
│   ├── fake.py                           # 确定性 MCP
│   └── service.py                        # logical_call_id 与调用审计
├── tasks/
│   ├── models.py                         # analysis_tasks / task_events
│   ├── schemas.py                        # API DTO
│   ├── repository.py                    # 所有权、租约和事件查询
│   ├── state.py                          # 状态迁移矩阵
│   ├── events.py                         # 提交后广播和 replay/live 合并
│   ├── service.py                        # 创建、查询和取消
│   ├── executor.py                       # plan -> execute -> summarize
│   ├── recovery.py                       # 过期租约恢复和 unknown 协调
│   ├── dependencies.py                   # 后台执行器依赖
│   └── router.py                         # tasks REST/SSE
├── orchestration/
│   ├── schemas.py                        # 严格计划 Schema
│   ├── context.py                        # 会话上下文构建
│   ├── planner.py                        # JSON Schema 与一次再生成
│   └── batching.py                       # DAG 校验和确定性批次
├── reporting/
│   ├── models.py                         # KOL、快照、候选、BI、收藏
│   ├── schemas.py                        # 候选、报告和收藏 DTO
│   ├── normalizers.py                    # 外部响应标准化
│   ├── scoring.py                        # 版本化确定性评分
│   ├── service.py                        # 候选/报告版本与收藏
│   └── router.py                         # candidates/reports/favorites
├── billing/
│   └── service.py                        # 批次预留与单调用原子终态
└── db/models.py                          # Alembic 完整模型导入

backend/migrations/versions/
├── 0002_analysis_runtime.py              # 任务、事件、模型运行、工具和调用
└── 0003_kol_reporting.py                 # KOL、候选、报告和收藏

src/
├── api/
│   ├── contracts.ts                      # 第二阶段 API DTO
│   ├── tasks.ts                          # 任务、候选、报告和取消
│   ├── favorites.ts                      # 收藏 API
│   └── taskStream.ts                     # 带认证 fetch SSE
├── state/taskEvents.ts                   # 事件幂等 reducer
├── hooks/
│   ├── useTaskStream.ts                  # 重连与事件恢复
│   └── useWorkspace.ts                   # 会话、任务、候选、报告聚合
├── components/
│   ├── ChatArea.tsx                      # 执行事件和流式结论
│   ├── WorkspaceTabs.tsx                 # 会话/候选/收藏标签
│   ├── CandidateList.tsx                 # 排序、筛选、选择、收藏
│   ├── CandidateCompare.tsx              # 多候选对比
│   ├── FavoritesPanel.tsx                # 跨会话收藏
│   └── BiReport.tsx                      # 版本化右侧 BI
├── types.ts                              # 前端领域类型
└── App.tsx                               # 三栏工作区编排
```

## 执行顺序与代理边界

1. Task 1-3 先冻结共享配置、数据库和任务事件契约，只能串行合入。
2. Task 4（模型）与 Task 5（MCP）可由两个代理并行实现，均只依赖 Task 1-3。
3. Task 6（计费）在 Task 5 后完成；Task 7（规划）在 Task 4、5 后完成。
4. Task 8（执行恢复）整合 Task 3、6、7，是后端闭环检查点。
5. Task 9-10 完成候选、BI 和 API；Task 11-13 再接前端。
6. Task 14 只做全量验证、安全和运行文档，不借机扩大产品范围。

每个代理领取任务前必须确认前置提交存在；只修改自己任务列出的文件。共享枚举、事件载荷和迁移如需调整，先回到主代理修改契约，不在下游任务中私自分叉。

### 2026-07-15 执行加速规则（适用于未完成 Task 6-14）

- 删除“确认模块不存在 / 路由返回 404”的独立 RED 步骤；实现者直接为关键业务行为写测试并进入最小实现。
- 每个任务只运行改动范围内的 focused tests。Task 6 保留钱包并发与原子性测试，Task 8 保留恢复与不重放测试；其余跨模块全库验证统一移至 Task 14 只运行一次。
- 任务完成后只做一次针对性代码审查；只有发现 Critical 或 Important 问题才再次审查，不再为每个任务固定重复审计。
- 删除真实腾讯 / DataTap 冒烟、Playwright 并发 / 断线 / 安全 E2E、视觉像素回归、连续 10 次稳定性跑测和仅为发布验收新增的指标测试。真实供应商调用仍需用户另行授权，且不属于本阶段自动化交付。
- 不删除涉及资金、访问隔离、调用不重放、迁移可逆和密钥脱敏的测试；这些是最小上线安全边界，而不是重复审计。

### Task 1: 冻结第二阶段配置、依赖与共享枚举

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/core/config.py`
- Modify: `.env.example`
- Create: `backend/app/tasks/__init__.py`
- Create: `backend/app/tasks/state.py`
- Create: `backend/app/mcp_gateway/__init__.py`
- Create: `backend/app/mcp_gateway/contracts.py`
- Test: `backend/tests/core/test_phase2_config.py`
- Test: `backend/tests/tasks/test_task_state.py`
- Test: `backend/tests/mcp_gateway/test_service_allowlist.py`

**Interfaces:**
- Produces: `TaskStatus`、`McpCallStatus`、`DataTapService`、`Settings.datatap_endpoint(service)`。
- Consumes: 现有 `Settings`、Pydantic 2 和环境配置。

- [ ] **Step 1: 写配置、白名单和状态迁移失败测试**

```python
# backend/tests/core/test_phase2_config.py
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.mcp_gateway.contracts import DataTapService


def settings(**changes) -> Settings:
    values = {
        "mysql_password": SecretStr("test-only-password"),
        "jwt_secret": SecretStr("test-only-jwt-secret-at-least-32-characters"),
        "model_provider": "fake",
        "mcp_provider": "fake",
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def test_datatap_endpoints_are_derived_only_from_five_enum_values() -> None:
    config = settings()
    assert {item.value for item in DataTapService} == {
        "insight-cube-mcp",
        "social-grow-mcp",
        "social-grow-content-mcp",
        "aktools-mcp",
        "bilibili-mcp",
    }
    assert config.datatap_endpoint(DataTapService.BILIBILI).unicode_string() == (
        "https://datatap.deepminer.com.cn/api/gateway/bilibili-mcp/mcp"
    )


def test_secret_values_are_not_exposed_by_settings_repr() -> None:
    config = settings(
        tencent_plan_api_key=SecretStr("unit-test-model-key"),
        datatap_mcp_token=SecretStr("unit-test-mcp-key"),
    )
    rendered = repr(config)
    assert "unit-test-model-key" not in rendered
    assert "unit-test-mcp-key" not in rendered


@pytest.mark.parametrize(
    "changes",
    [
        {"mcp_call_points": 9},
        {"mcp_max_calls_per_task": 11},
        {"tencent_plan_model": "another-model"},
        {"tencent_plan_base_url": "https://untrusted.example/v1"},
    ],
)
def test_confirmed_provider_and_billing_constants_cannot_drift(changes) -> None:
    with pytest.raises(ValidationError):
        settings(**changes)
```

```python
# backend/tests/tasks/test_task_state.py
import pytest

from app.tasks.state import InvalidTaskTransition, TaskStatus, ensure_transition


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (TaskStatus.PENDING, TaskStatus.PLANNING),
        (TaskStatus.PLANNING, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.COMPLETED),
        (TaskStatus.RUNNING, TaskStatus.INTERRUPTED),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
    ],
)
def test_allowed_task_transitions(source: TaskStatus, target: TaskStatus) -> None:
    ensure_transition(source, target)


@pytest.mark.parametrize("terminal", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_terminal_task_cannot_return_to_running(terminal: TaskStatus) -> None:
    with pytest.raises(InvalidTaskTransition):
        ensure_transition(terminal, TaskStatus.RUNNING)
```

- [ ] **Step 2: 运行测试并确认缺少第二阶段类型**

Run: `cd backend && .venv/bin/pytest tests/core/test_phase2_config.py tests/tasks/test_task_state.py -q`

Expected: 因 `app.tasks.state` 或第二阶段配置字段不存在而失败，且现有测试未被修改成跳过。

- [ ] **Step 3: 添加依赖、配置和严格枚举**

```toml
# backend/pyproject.toml 的 dependencies 新增
"httpx>=0.28,<1",
"mcp>=1.27,<2",
"openai>=1,<2",
"prometheus-client>=0.21,<1",
```

```python
# backend/app/mcp_gateway/contracts.py
from enum import StrEnum


class DataTapService(StrEnum):
    INSIGHT_CUBE = "insight-cube-mcp"
    SOCIAL_GROW = "social-grow-mcp"
    SOCIAL_GROW_CONTENT = "social-grow-content-mcp"
    AKTOOLS = "aktools-mcp"
    BILIBILI = "bilibili-mcp"


class McpCallStatus(StrEnum):
    PLANNED = "planned"
    RESERVED = "reserved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SETTLED = "settled"
    RELEASED = "released"
```

```python
# backend/app/tasks/state.py
from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class TaskEventType(StrEnum):
    TASK_PENDING = "task.pending"
    PLAN_READY = "plan.ready"
    TOOL_STARTED = "tool.started"
    TOOL_SUCCEEDED = "tool.succeeded"
    TOOL_FAILED = "tool.failed"
    TOOL_UNKNOWN = "tool.unknown"
    POINTS_RESERVED = "points.reserved"
    POINTS_SETTLED = "points.settled"
    POINTS_RELEASED = "points.released"
    CANDIDATES_UPDATED = "candidates.updated"
    BI_UPDATED = "bi.updated"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.INSUFFICIENT_BALANCE,
    TaskStatus.CANCELLED,
}

ALLOWED_TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.PLANNING, TaskStatus.CANCELLED},
    TaskStatus.PLANNING: {
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.INSUFFICIENT_BALANCE,
        TaskStatus.INTERRUPTED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.INSUFFICIENT_BALANCE,
        TaskStatus.INTERRUPTED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.INTERRUPTED: {
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
}


class InvalidTaskTransition(ValueError):
    pass


def ensure_transition(source: TaskStatus, target: TaskStatus) -> None:
    if target not in ALLOWED_TRANSITIONS.get(source, set()):
        raise InvalidTaskTransition(f"{source.value}->{target.value}")
```

`Settings` 增加以下同名字段，并在 `datatap_endpoint()` 中只接受 `DataTapService`，不接受任意字符串：

```python
model_provider: Literal["tencent_plan", "fake"] = "fake"
tencent_plan_base_url: AnyHttpUrl = AnyHttpUrl("https://api.lkeap.cloud.tencent.com/plan/v3")
tencent_plan_api_key: SecretStr | None = None
tencent_plan_model: str = "deepseek-v4-pro-202606"
model_timeout_seconds: float = 60.0
mcp_provider: Literal["datatap", "fake"] = "fake"
datatap_mcp_token: SecretStr | None = None
mcp_call_points: int = 10
mcp_max_calls_per_task: int = 10
mcp_unknown_reconcile_seconds: int = 300
task_lease_seconds: int = 60

def datatap_endpoint(self, service: DataTapService) -> AnyHttpUrl:
    return AnyHttpUrl(
        f"https://datatap.deepminer.com.cn/api/gateway/{service.value}/mcp"
    )
```

生产环境除禁止 mock 认证外，还必须禁止 `model_provider=fake`、`mcp_provider=fake`，并要求两个 `SecretStr` 均存在。`.env.example` 只列环境变量名和非敏感默认配置，不填写密钥示例值。

同一 `model_validator` 对所有环境固定校验 Base URL、模型名、单次 10 积分和单任务最多 10 次调用；这些值不能被环境变量悄悄改写。超时、租约和 unknown 观察期只允许正整数范围。

```dotenv
# .env.example 第二阶段配置
MODEL_PROVIDER=fake
TENCENT_PLAN_BASE_URL=https://api.lkeap.cloud.tencent.com/plan/v3
TENCENT_PLAN_MODEL=deepseek-v4-pro-202606
TENCENT_PLAN_API_KEY=
MODEL_TIMEOUT_SECONDS=60
MCP_PROVIDER=fake
DATATAP_MCP_TOKEN=
MCP_CALL_POINTS=10
MCP_MAX_CALLS_PER_TASK=10
MCP_UNKNOWN_RECONCILE_SECONDS=300
TASK_LEASE_SECONDS=60
```

- [ ] **Step 4: 安装依赖并运行配置测试**

Run: `cd backend && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest tests/core/test_phase2_config.py tests/tasks/test_task_state.py -q`

Expected: 新测试全部通过；依赖解析到 MCP 1.x 与 OpenAI 1.x。

- [ ] **Step 5: 提交共享契约**

```bash
git add .env.example backend/pyproject.toml backend/app/core/config.py backend/app/tasks backend/app/mcp_gateway backend/tests/core backend/tests/tasks/test_task_state.py
git commit -m "feat: define phase two runtime contracts"
```

### Task 2: 建立任务、调用、KOL 与报告持久化模型

**Files:**
- Create: `backend/app/tasks/models.py`
- Create: `backend/app/model/__init__.py`
- Create: `backend/app/model/models.py`
- Create: `backend/app/mcp_gateway/models.py`
- Create: `backend/app/reporting/__init__.py`
- Create: `backend/app/reporting/models.py`
- Modify: `backend/app/db/models.py`
- Create: `backend/migrations/versions/0002_analysis_runtime.py`
- Create: `backend/migrations/versions/0003_kol_reporting.py`
- Modify: `backend/tests/test_schema.py`
- Create: `backend/tests/test_phase2_migrations.py`

**Interfaces:**
- Produces: `AnalysisTask`、`TaskEvent`、`ModelRun`、`McpToolCatalog`、`McpCall`、`Kol`、`KolSnapshot`、`TaskCandidate`、`BiReport`、`UserKolFavorite`。
- Consumes: Task/MCP 枚举、现有 `users`、`sessions`、`messages`、`wallet_transactions`。

- [ ] **Step 1: 把旧的精确表集合测试改成阶段子集并先写迁移失败测试**

```python
# backend/tests/test_schema.py
from app.db.base import Base
import app.db.models  # noqa: F401


def test_phase_one_tables_remain_registered() -> None:
    assert {
        "users", "auth_identities", "user_sessions", "user_channel_permissions",
        "wallets", "wallet_transactions", "sessions", "messages",
    }.issubset(Base.metadata.tables)


def test_phase_two_tables_are_registered() -> None:
    assert {
        "analysis_tasks", "task_events", "model_runs", "mcp_tool_catalog",
        "mcp_calls", "kols", "kol_snapshots", "task_candidates",
        "bi_reports", "user_kol_favorites",
    }.issubset(Base.metadata.tables)
```

```python
# backend/tests/test_phase2_migrations.py
from sqlalchemy import inspect

from app.db.session import engine


async def test_phase_two_unique_constraints() -> None:
    async with engine.connect() as connection:
        constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("mcp_calls")
        )
    names = {item["name"] for item in constraints}
    assert "uq_mcp_calls_logical_call_id" in names
    assert "uq_mcp_calls_task_step_attempt" in names
```

- [ ] **Step 2: 运行测试并确认十张表尚未注册**

Run: `cd backend && .venv/bin/pytest tests/test_schema.py tests/test_phase2_migrations.py -q`

Expected: `test_phase_two_tables_are_registered` 失败并列出缺失表。

- [ ] **Step 3: 定义模型、外键和恢复索引**

模型字段必须与设计文档逐项一致，关键 SQLAlchemy 约束固定为：

```python
# backend/app/tasks/models.py（关键约束）
class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (
        Index("ix_analysis_tasks_user_session_created", "user_id", "session_id", "created_at"),
        Index("ix_analysis_tasks_status_lease", "status", "lease_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    trigger_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_version: Mapped[str | None] = mapped_column(String(32))
    max_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    estimated_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_task_id_id", "task_id", "id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("analysis_tasks.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
```

```python
# backend/app/mcp_gateway/models.py（关键约束）
class McpCall(Base):
    __tablename__ = "mcp_calls"
    __table_args__ = (
        UniqueConstraint("logical_call_id", name="uq_mcp_calls_logical_call_id"),
        UniqueConstraint("task_id", "plan_step_id", "attempt", name="uq_mcp_calls_task_step_attempt"),
        UniqueConstraint("settlement_transaction_id", name="uq_mcp_calls_settlement_transaction"),
        Index("ix_mcp_calls_status_updated", "status", "updated_at"),
    )
```

`ModelRun` 保存用途、供应商、模型、模板与版本、状态、五个可空 token 用量、耗时、错误类型和请求 ID。`McpToolCatalog` 保存服务枚举、内部工具名、审核 Schema、发现摘要、审核/启用状态。`McpCall` 保存计划步骤、参数摘要、调用状态、预留/结算交易、响应哈希、证据 JSON、错误与时间。

`McpToolCatalog.service_slug` 增加只含五个枚举值的 `CheckConstraint`；`McpCall.status` 增加已确认状态集合的 `CheckConstraint`，预留/结算交易字段外键指向 `wallet_transactions.id`。四个禁用服务即使绕过应用层也无法写入目录。

`Kol` 对 `(platform, platform_account_id)` 建唯一键；`KolSnapshot` 保存标准化 JSON 和采集时间；`TaskCandidate` 对 `(task_id, candidate_version, kol_id)` 唯一；`BiReport` 对 `(task_id, report_version)` 唯一并保存 `candidate_version`；`UserKolFavorite` 对 `(user_id, kol_id)` 唯一。

- [ ] **Step 4: 创建两个可逆迁移并验证 test 数据库**

`0002` 只创建运行时五表，`0003` 只创建报告五表；`downgrade()` 按外键逆序删除。迁移中显式写出模型同名约束，不依赖运行时自动建表。

Run: `cd backend && .venv/bin/alembic upgrade head && .venv/bin/pytest tests/test_schema.py tests/test_phase2_migrations.py -q`

Expected: 数据库版本为 `0003 (head)`；两组模型/约束测试全部通过。

- [ ] **Step 5: 提交持久化模型**

```bash
git add backend/app/db/models.py backend/app/tasks/models.py backend/app/model backend/app/mcp_gateway/models.py backend/app/reporting backend/migrations/versions backend/tests/test_schema.py backend/tests/test_phase2_migrations.py
git commit -m "feat: persist analysis runtime and reporting data"
```

### Task 3: 实现任务状态、持久化事件和带认证 SSE

**Files:**
- Create: `backend/app/tasks/schemas.py`
- Create: `backend/app/tasks/repository.py`
- Create: `backend/app/tasks/events.py`
- Create: `backend/app/tasks/service.py`
- Create: `backend/app/tasks/router.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/app/workspace/models.py`
- Create: `backend/tests/tasks/fakes.py`
- Test: `backend/tests/tasks/test_task_service.py`
- Test: `backend/tests/tasks/test_task_sse.py`

**Interfaces:**
- Produces: `TaskService.create()`、`TaskService.cancel()`、`TaskEventStream.stream()`、任务 REST/SSE API。
- Consumes: `WorkspaceService.get_owned_session()`、`AnalysisTask`、`TaskEvent`、`CurrentUser`。

- [ ] **Step 1: 写任务所有权、事件 ID 和 replay/live 失败测试**

```python
# backend/tests/tasks/test_task_service.py
async def test_create_task_persists_message_task_and_pending_event(
    db_session, user_factory, workspace_factory
) -> None:
    user = await user_factory()
    workspace = await workspace_factory(user.id)
    service = TaskService(db_session)

    task = await service.create(
        user.id,
        workspace.id,
        TaskCreate(content="寻找预算内的 B 站科技达人", scoring_profile="balanced"),
    )
    events = await TaskRepository(db_session).list_events_after(task.id, 0)

    assert task.status == TaskStatus.PENDING
    assert task.user_id == user.id
    assert [event.event_type for event in events] == ["task.pending"]
```

```python
# backend/tests/tasks/test_task_sse.py
async def test_stream_subscribes_before_replay_and_deduplicates(
    task_event_stream, persisted_task, monkeypatch
) -> None:
    first = await task_event_stream.append(persisted_task, "plan.ready", {"calls": 1})
    replay_started = asyncio.Event()
    continue_replay = asyncio.Event()
    original = task_event_stream.repository.list_events_after

    async def gated(*args, **kwargs):
        rows = await original(*args, **kwargs)
        replay_started.set()
        await continue_replay.wait()
        return rows

    monkeypatch.setattr(task_event_stream.repository, "list_events_after", gated)
    collection = asyncio.create_task(
        collect_event_ids(
            task_event_stream.stream(persisted_task.id, persisted_task.user_id, 0),
            count=2,
        )
    )
    await replay_started.wait()
    second = await task_event_stream.append(persisted_task, "tool.started", {"call_id": "c1"})
    continue_replay.set()

    assert await collection == [first.id, second.id]
```

- [ ] **Step 2: 运行测试并确认任务服务和事件流不存在**

Run: `cd backend && .venv/bin/pytest tests/tasks/test_task_service.py tests/tasks/test_task_sse.py -q`

Expected: 导入失败或 API 返回 404。

- [ ] **Step 3: 实现状态服务、提交后广播和 fetch SSE**

```python
# backend/app/tasks/schemas.py
class TaskCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    scoring_profile: Literal[
        "balanced", "audience_first", "performance_first", "budget_first", "risk_first"
    ] = "balanced"


class TaskRead(BaseModel):
    id: str
    session_id: str
    status: TaskStatus
    estimated_points: int
    error_code: str | None
    latest_report_id: str | None = None


class TaskEventRead(BaseModel):
    id: int
    task_id: str
    type: TaskEventType
    payload: dict[str, Any]
    created_at: datetime
```

```python
# backend/app/tasks/events.py
class TaskEventBroker:
    async def subscribe(self, task_id: str) -> asyncio.Queue[TaskEvent]: ...
    async def unsubscribe(self, task_id: str, queue: asyncio.Queue[TaskEvent]) -> None: ...
    async def publish(self, event: TaskEvent) -> None: ...


class TaskEventStream:
    def __init__(self, session_factory, repository_factory, broker: TaskEventBroker) -> None: ...

    async def append(
        self, task: AnalysisTask, event_type: str, payload: dict[str, Any]
    ) -> TaskEvent:
        async with self.session_factory.begin() as db:
            event = await self.repository_factory(db).append_event(
                task.id, task.user_id, event_type, payload
            )
        await self.broker.publish(event)
        return event

    async def stream(
        self, task_id: str, user_id: str, last_event_id: int
    ) -> AsyncIterator[TaskEvent]:
        queue = await self.broker.subscribe(task_id)
        seen = last_event_id
        try:
            async with self.session_factory() as db:
                rows = await self.repository_factory(db).list_owned_events_after(
                    task_id, user_id, seen
                )
            for row in rows:
                if row.id > seen:
                    seen = row.id
                    yield row
            while True:
                row = await queue.get()
                if row.user_id == user_id and row.id > seen:
                    seen = row.id
                    yield row
        finally:
            await self.broker.unsubscribe(task_id, queue)
```

SSE 路由使用 `StreamingResponse(media_type="text/event-stream")`，事件格式固定为 `id:`、`event:`、`data:`，每 15 秒输出注释心跳。`Last-Event-ID` 请求头优先，`last_event_id` 查询参数作为测试和兼容入口。路由先用 `TaskRepository.get_owned()` 校验用户，再开始流式响应。

`POST /sessions/{session_id}/tasks` 在同一请求事务中追加用户消息、创建 `pending` 任务和首事件；成功返回 `202`。`GET /tasks/{id}` 与 `POST /tasks/{id}/cancel` 都做所有权校验。取消只设置 `cancel_requested_at`，由执行器安全收敛。

`backend/tests/tasks/fakes.py` 在本任务提供 `workspace_factory`、`persisted_task`、`collect_event_ids()` 和内存 `TaskEventBroker` 夹具；Task 8 在同一文件继续加入脚本化模型/MCP 和崩溃注入，不另造同名 helper。

- [ ] **Step 4: 验证 API、回放和跨用户隔离**

Run: `cd backend && .venv/bin/pytest tests/tasks/test_task_service.py tests/tasks/test_task_sse.py tests/workspace/test_sessions.py -q`

Expected: 任务、SSE 和旧会话测试全部通过；其他用户查询任务或事件得到 404；关闭 SSE 生成器不改变任务状态。

- [ ] **Step 5: 提交任务事件纵向切片**

```bash
git add backend/app/tasks backend/app/api/router.py backend/app/workspace/models.py backend/tests/tasks backend/tests/workspace/test_sessions.py
git commit -m "feat: add durable task events and authenticated SSE"
```

### Task 4: 接入腾讯 Token Plan 结构化与流式适配器

**Files:**
- Create: `backend/app/model/contracts.py`
- Create: `backend/app/model/adapter.py`
- Create: `backend/app/model/prompts.py`
- Create: `backend/app/model/tencent_plan.py`
- Create: `backend/app/model/fake.py`
- Create: `backend/app/model/dependencies.py`
- Create: `backend/app/model/runs.py`
- Create: `backend/tests/model/fakes.py`
- Test: `backend/tests/model/test_tencent_plan.py`
- Test: `backend/tests/model/test_tencent_plan_stream.py`
- Test: `backend/tests/model/test_prompts.py`

**Interfaces:**
- Produces: `ModelAdapter.complete_json()`、`ModelAdapter.stream_text()`、`ModelRunService`、进程级 `get_model_adapter()`。
- Consumes: Task 1 的腾讯配置、OpenAI 1.x、Task 2 的 `ModelRun`。

- [ ] **Step 1: 写结构化能力、重试和流式边界失败测试**

```python
# backend/tests/model/test_tencent_plan.py
async def test_json_schema_unsupported_falls_back_to_json_object() -> None:
    client = FakeChatCompletions(
        [unsupported_schema_error(), completion('{"objective":"选人","steps":[]}')]
    )
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock())

    result = await adapter.complete_json(
        StructuredModelRequest(
            purpose="planner",
            template_name="planner_v1",
            messages=(ChatMessage(role="user", content="选人"),),
            output_model=MinimalPlan,
        )
    )

    assert result.value.objective == "选人"
    assert [call["response_format"]["type"] for call in client.calls] == [
        "json_schema", "json_object"
    ]
```

```python
async def test_invalid_structure_is_regenerated_only_once() -> None:
    client = FakeChatCompletions([
        completion("not-json"),
        completion('{"objective":"选人","steps":"invalid"}'),
    ])
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock())

    with pytest.raises(ModelPlanInvalidError) as caught:
        await adapter.complete_json(planner_request(MinimalPlan))

    assert caught.value.code == "MODEL_PLAN_INVALID"
    assert len(client.calls) == 2
```

```python
# backend/tests/model/test_tencent_plan_stream.py
async def test_stream_skips_empty_delta_and_emits_usage_then_done() -> None:
    adapter = adapter_with_stream(
        chunk(content="", finish_reason=None),
        chunk(content="推荐", finish_reason=None),
        chunk(content=None, finish_reason="stop"),
        usage_chunk(prompt=8, completion=2, total=10),
    )
    events = [event async for event in adapter.stream_text(summary_request())]

    assert [event.type for event in events] == ["text.delta", "usage.updated", "stream.completed"]
    assert events[0].text == "推荐"
    assert events[1].usage.total_tokens == 10
    assert events[2].finish_reason == "stop"


async def test_partial_stream_interruption_is_not_retried() -> None:
    adapter = adapter_with_broken_stream(first_text="部分结论")
    received = []
    with pytest.raises(ModelStreamInterrupted):
        async for event in adapter.stream_text(summary_request()):
            received.append(event)
    assert received[0].text == "部分结论"
    assert adapter.create_count == 1
```

- [ ] **Step 2: 运行测试并确认模型模块尚未实现**

Run: `cd backend && .venv/bin/pytest tests/model -q`

Expected: `app.model.contracts` 或 `TencentPlanAdapter` 导入失败。

- [ ] **Step 3: 实现窄接口、错误映射和版本化 Prompt**

```python
# backend/app/model/contracts.py
T = TypeVar("T", bound=BaseModel)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class TokenUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class StructuredModelRequest(Generic[T]):
    purpose: Literal["planner", "analyst"]
    template_name: str
    messages: tuple[ChatMessage, ...]
    output_model: type[T]
    max_tokens: int = 4096


class StreamingModelRequest(BaseModel):
    purpose: Literal["summary"] = "summary"
    template_name: Literal["summary_v1"] = "summary_v1"
    messages: tuple[ChatMessage, ...]
    max_tokens: int = 2048


class ModelEvent(BaseModel):
    type: Literal["text.delta", "usage.updated", "stream.completed"]
    text: str | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None


class StructuredResult(BaseModel, Generic[T]):
    value: T
    usage: TokenUsage | None
    request_id: str | None
    regeneration_count: int


class ModelAdapterError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool, request_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


class ModelPlanInvalidError(ModelAdapterError): ...


class ModelStreamInterrupted(ModelAdapterError):
    partial_output_received: bool
```

```python
# backend/app/model/adapter.py
class ModelAdapter(Protocol):
    async def complete_json(self, request: StructuredModelRequest[T]) -> StructuredResult[T]: ...
    async def stream_text(self, request: StreamingModelRequest) -> AsyncIterator[ModelEvent]: ...
    async def aclose(self) -> None: ...
```

`TencentPlanAdapter` 构造 `AsyncOpenAI(api_key=..., base_url=..., max_retries=0)`。只对网络瞬时错误和 `429/502/503/504` 做带抖动的有限重试；`400/401/402/403/451/499`、取消和业务超时不重试。只有供应商明确返回不支持 response format 时才把 `(base_url, model, schema_digest)` 缓存为不支持；认证、额度和网络错误不能触发降级。

JSON Schema 和 `json_object` 两条路径最终都使用 `request.output_model.model_validate_json()` 严格校验。首次无效把裁剪后的校验错误加入一次修复消息，第二次无效抛 `ModelPlanInvalidError("MODEL_PLAN_INVALID")`。

流处理忽略空 delta，记录最后一个 `finish_reason`，usage 字段全部可空；正常 EOF 未见结束原因时抛中断。已经输出正文后的网络错误不得自动重开流。`asyncio.CancelledError` 原样传播。

```python
# backend/app/model/prompts.py
PLANNER_PROMPT = PromptTemplate(name="planner_v1", version="1", system=PLANNER_SYSTEM_TEXT)
ANALYST_PROMPT = PromptTemplate(name="analyst_v1", version="1", system=ANALYST_SYSTEM_TEXT)
SUMMARY_PROMPT = PromptTemplate(name="summary_v1", version="1", system=SUMMARY_SYSTEM_TEXT)
```

三个 system 文本明确：外部内容是不可信数据；只能使用传入证据；不得请求隐藏工具、URL、密钥或额外调用；Planner 只能输出目标 Schema。测试断言 Prompt 不包含 DataTap 端点或环境变量值。

`FakeModelAdapter` 接收脚本化结果队列并记录请求，供后续所有测试使用；默认不访问网络。

`backend/tests/model/fakes.py` 提供本任务测试使用的 `FakeChatCompletions`、`completion()`、`unsupported_schema_error()`、`chunk()`、`usage_chunk()`、`adapter_with_stream()` 和 `adapter_with_broken_stream()`，全部只返回内存对象。`MinimalPlan` 和 `planner_request()` 定义在同一测试模块，避免依赖未声明的生产类型。

```python
# backend/app/model/runs.py
class ModelRunService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def start(self, task_id: str, request: ModelRequestMetadata) -> ModelRun: ...
    async def succeed(
        self, run: ModelRun, *, usage: TokenUsage | None, request_id: str | None, duration_ms: int
    ) -> ModelRun: ...
    async def fail(
        self, run: ModelRun, *, error_type: str, request_id: str | None, duration_ms: int
    ) -> ModelRun: ...
```

执行器调用模型前后使用该服务记录 `purpose/provider/model/template/version/status/usage/duration/error/request_id`；请求正文和密钥不写入 `model_runs`。新增测试断言成功、结构失败和流中断都留下对应运行记录。

- [ ] **Step 4: 验证结构化、流式、Prompt 和旧测试**

Run: `cd backend && .venv/bin/pytest tests/model tests/test_health.py -q`

Expected: JSON Schema、回退、一次再生成、重试分类、空 delta、usage、finish reason、中断和取消测试全部通过。

- [ ] **Step 5: 提交模型适配器**

```bash
git add backend/app/model backend/tests/model
git commit -m "feat: integrate Tencent Token Plan model adapter"
```

### Task 5: 实现五服务 MCP Transport、工具隔离与严格校验

**Files:**
- Create: `backend/app/mcp_gateway/transport.py`
- Create: `backend/app/mcp_gateway/datatap.py`
- Create: `backend/app/mcp_gateway/registry.py`
- Create: `backend/app/mcp_gateway/approved_tools.json`
- Create: `backend/app/mcp_gateway/validation.py`
- Create: `backend/app/mcp_gateway/fake.py`
- Create: `backend/app/mcp_gateway/service.py`
- Test: `backend/tests/mcp_gateway/fakes.py`
- Test: `backend/tests/mcp_gateway/test_transport_policy.py`
- Test: `backend/tests/mcp_gateway/test_registry.py`
- Test: `backend/tests/mcp_gateway/test_validation.py`
- Test: `backend/tests/mcp_gateway/test_unknown_outcome.py`
- Test: `backend/tests/mcp_gateway/test_service_isolation.py`

**Interfaces:**
- Produces: `McpTransport`、`ToolRegistryService`、`McpCallService.prepare()`、`McpCallService.invoke()`。
- Consumes: Task 1 的服务枚举、Task 2 的工具/调用模型、官方 MCP SDK。

- [ ] **Step 1: 写禁用服务网络前拒绝、Schema 漂移和 unknown 失败测试**

```python
# backend/tests/mcp_gateway/test_transport_policy.py
@pytest.mark.parametrize(
    "slug", ["zhihu-mcp", "toutiao-mcp", "baidu-index-mcp", "google-trends-mcp"]
)
async def test_disabled_service_is_rejected_before_network(slug: str) -> None:
    opened = AsyncMock(side_effect=AssertionError("network must not open"))
    transport = DataTapTransport(token=SecretStr("unit-test-token"), session_opener=opened)

    with pytest.raises(ServiceNotAllowedError):
        await transport.list_tools(slug)

    opened.assert_not_awaited()
```

```python
# backend/tests/mcp_gateway/test_registry.py
async def test_schema_drift_quarantines_without_overwriting_approved_schema(
    db_session, approved_tool_factory
) -> None:
    row = await approved_tool_factory(service="social-grow-mcp", is_enabled=True)
    original = row.approved_input_schema
    transport = FakeMcpTransport.with_discovered_tool(
        service="social-grow-mcp",
        remote_name=row.remote_name,
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
    )

    report = await ToolRegistryService(db_session, transport).refresh_service(
        DataTapService.SOCIAL_GROW
    )
    await db_session.refresh(row)

    assert report.quarantined_remote_names == (row.remote_name,)
    assert row.review_status == "quarantined"
    assert row.is_enabled is False
    assert row.approved_input_schema == original
```

```python
# backend/tests/mcp_gateway/test_unknown_outcome.py
async def test_possible_sent_timeout_becomes_unknown_and_is_not_replayed(
    db_session, reserved_call, approved_tool
) -> None:
    transport = FakeMcpTransport(call_error=PossiblySentTimeout("fake read timeout"))
    service = McpCallService(db_session, transport)

    first = await service.invoke(reserved_call.logical_call_id)
    second = await service.invoke(reserved_call.logical_call_id)

    assert first.status == second.status == McpCallStatus.UNKNOWN
    assert transport.call_count == 1
```

```python
# backend/tests/mcp_gateway/test_service_isolation.py
async def test_open_circuit_for_one_service_does_not_block_another() -> None:
    transport = isolated_transport(failing_service=DataTapService.AKTOOLS)
    for _ in range(transport.failure_threshold):
        with pytest.raises(McpUpstreamError):
            await transport.list_tools(DataTapService.AKTOOLS)

    tools = await transport.list_tools(DataTapService.BILIBILI)
    assert tools
    assert transport.open_count(DataTapService.BILIBILI) == 1
```

- [ ] **Step 2: 运行测试并确认 Transport、注册表和调用服务不存在**

Run: `cd backend && .venv/bin/pytest tests/mcp_gateway/test_transport_policy.py tests/mcp_gateway/test_registry.py tests/mcp_gateway/test_validation.py tests/mcp_gateway/test_unknown_outcome.py tests/mcp_gateway/test_service_isolation.py -q`

Expected: 新模块导入失败；不得把测试改成真实网络冒烟。

- [ ] **Step 3: 实现固定 HTTPS Transport 和半动态注册表**

```python
# backend/app/mcp_gateway/transport.py
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class DiscoveredTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


@dataclass(frozen=True)
class RemoteToolResult:
    structured_content: JsonValue
    is_error: bool
    upstream_request_id: str | None


class McpTransport(Protocol):
    async def list_tools(self, service: DataTapService) -> tuple[DiscoveredTool, ...]: ...
    async def call_tool(
        self, service: DataTapService, remote_name: str, arguments: Mapping[str, JsonValue]
    ) -> RemoteToolResult: ...


@dataclass(frozen=True)
class ToolInvocationOutcome:
    status: Literal["succeeded", "failed", "unknown"]
    validated_output: JsonValue | None
    response_hash: str | None
    upstream_request_id: str | None
    error_type: str | None


class ServiceNotAllowedError(ValueError): ...
class PossiblySentTimeout(TimeoutError): ...
class McpUpstreamError(RuntimeError): ...
class LogicalCallConflictError(ValueError): ...
```

`DataTapTransport` 使用 `httpx.AsyncClient`，固定 Authorization、`follow_redirects=False`、`trust_env=False`、TLS 验证和分项超时。每次协议会话按 `(gateway_session_id, service, credential_version)` 隔离；通过 `streamable_http_client`、`ClientSession.initialize()` 和 `call_tool()` 调用。进入 `call_tool()` 后发生读超时包装为 `PossiblySentTimeout`，Transport 内不重试。

每个服务拥有独立 `asyncio.Semaphore`、失败计数、熔断开启时间和半开探测；一个服务的连接、排队和熔断状态不能被另一个服务共享。测试配置小阈值和假时钟，不用真实 sleep。

```python
# backend/app/mcp_gateway/registry.py
@dataclass(frozen=True)
class ApprovedTool:
    catalog_id: str
    internal_name: str
    service: DataTapService
    remote_name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class ToolRegistryService:
    async def refresh_service(self, service: DataTapService) -> DiscoveryReport: ...
    async def require_enabled(self, internal_name: str) -> ApprovedTool: ...
    async def list_enabled(self) -> tuple[ApprovedTool, ...]: ...
```

发现的新工具和 Schema 摘要变化工具只写 `quarantined`，不能分配或继续暴露内部名。审核 Schema 使用 `additionalProperties: false`，并由校验器限制总体字节、嵌套深度、字符串、数组和数值。输入不能包含 `url`、`host`、`authorization`、`service_slug`、`device_id` 等路由字段。

`approved_tools.json` 首次提交为拒绝优先的确定内容：

```json
{
  "manifest_version": 1,
  "tools": []
}
```

测试通过依赖注入内存审核 manifest。真实环境只在用户另行授权后才执行五服务 `initialize/tools/list`，把发现项写入 quarantine；人工核对名称、描述和 Schema 后再生成新的受版本控制 manifest。未进入 manifest 的工具即使上游存在也不能被 Planner 看见或调用。

```python
# backend/app/mcp_gateway/service.py
class McpCallService:
    async def prepare(
        self,
        *,
        logical_call_id: str,
        user_id: str,
        task_id: str,
        plan_step_id: str,
        internal_tool_name: str,
        arguments: dict[str, JsonValue],
    ) -> McpCall: ...

    async def invoke(self, logical_call_id: str) -> McpCall: ...
```

`prepare()` 保存规范化参数摘要；相同 ID 不同摘要抛 `LogicalCallConflictError`。`invoke()` 只有在 `reserved` 原子 claim 为 `running` 后才访问 Transport；成功需满足 JSON-RPC 成功、`is_error=False`、大小和输出 Schema 通过、证据/哈希可持久化。明确错误返回 `failed`，可能已发送超时返回 `unknown`。`unknown/succeeded/failed/settled/released` 再次调用直接返回，不访问 Transport。

- [ ] **Step 4: 验证固定端点、无重定向、隔离和无真实网络**

Run: `cd backend && .venv/bin/pytest tests/mcp_gateway/test_transport_policy.py tests/mcp_gateway/test_registry.py tests/mcp_gateway/test_validation.py tests/mcp_gateway/test_unknown_outcome.py tests/mcp_gateway/test_service_isolation.py -q`

Expected: 四个禁用服务网络计数为 0；五个固定端点全部为 HTTPS；302 不跟随；漂移工具被隔离；unknown 调用计数始终为 1。

- [ ] **Step 5: 提交 MCP 网关核心**

```bash
git add backend/app/mcp_gateway backend/tests/mcp_gateway
git commit -m "feat: add allowlisted DataTap MCP gateway"
```

### Task 6: 把批次预留和 MCP 终态纳入原子计费

**Files:**
- Modify: `backend/app/billing/service.py`
- Create: `backend/app/mcp_gateway/accounting.py`
- Modify: `backend/app/mcp_gateway/service.py`
- Test: `backend/tests/billing/test_wallet_batch.py`
- Modify: `backend/tests/billing/test_wallet_concurrency.py`
- Test: `backend/tests/mcp_gateway/test_billing_lifecycle.py`
- Test: `backend/tests/mcp_gateway/test_billing_atomicity.py`

**Interfaces:**
- Produces: `WalletService.reserve_batch()`、`McpAccounting.finalize()`、成功/失败/unknown 的精确钱包结果。
- Consumes: 现有 `reserve/settle/release`、Task 5 的调用状态。

```python
@dataclass(frozen=True)
class ExecuteMcpCall:
    logical_call_id: str
    user_id: str
    task_id: str
    plan_step_id: str
    internal_tool_name: str
    arguments: dict[str, JsonValue]


class McpAccounting:
    async def reserve_batch(
        self, user_id: str, calls: Sequence[McpCall]
    ) -> None: ...

    async def finalize(
        self, call: McpCall, outcome: ToolInvocationOutcome
    ) -> McpCall: ...


class McpGatewayService:
    async def execute(self, command: ExecuteMcpCall) -> McpCall: ...

    async def execute_batch(
        self, commands: Sequence[ExecuteMcpCall]
    ) -> tuple[McpCall, ...]: ...
```

- [ ] **Step 1: 写批次全有或全无、10 方争用和终态测试**

```python
# backend/tests/billing/test_wallet_batch.py
async def test_batch_reservation_is_all_or_nothing(wallet_user_with_balance) -> None:
    user_id = await wallet_user_with_balance(20)
    requests = tuple(
        ReservationRequest(reference_id=f"call-{index}", idempotency_key=f"batch:{index}")
        for index in range(3)
    )
    async with SessionFactory() as db:
        with pytest.raises(InsufficientPointsError):
            async with db.begin():
                await WalletService(db).reserve_batch(user_id, requests)

    wallet, transactions = await read_wallet_and_transactions(user_id)
    assert (wallet.balance, wallet.reserved) == (20, 0)
    assert transactions == []
```

同一测试再通过 `McpGatewayService.execute_batch()` 传入三条命令，注入余额 20 的钱包并断言 `fake_transport.call_count == 0`，证明不是只回滚账本而仍然调用上游。

```python
# backend/tests/mcp_gateway/test_billing_lifecycle.py
async def test_success_charges_ten_exactly_once(gateway, funded_user, fake_transport) -> None:
    command = make_call_command(funded_user.id)
    first = await gateway.execute(command)
    second = await gateway.execute(command)
    wallet = await read_wallet(funded_user.id)

    assert first.status == second.status == McpCallStatus.SETTLED
    assert (wallet.balance, wallet.reserved) == (990, 0)
    assert fake_transport.call_count == 1
    assert await terminal_ledger_kinds(first.id) == ["reserve", "settle"]


async def test_failure_releases_and_unknown_retains(gateway_factory, funded_user) -> None:
    failed = await gateway_factory(result=remote_error()).execute(make_call_command(funded_user.id))
    assert failed.status == McpCallStatus.RELEASED
    assert await wallet_tuple(funded_user.id) == (1000, 0)

    unknown = await gateway_factory(error=PossiblySentTimeout("fake")).execute(
        make_call_command(funded_user.id, logical_call_id="unknown-call")
    )
    assert unknown.status == McpCallStatus.UNKNOWN
    assert await wallet_tuple(funded_user.id) == (990, 10)
```

- [ ] **Step 3: 实现批次预留和三事务调用边界**

```python
# backend/app/billing/service.py
@dataclass(frozen=True)
class ReservationRequest:
    reference_id: str
    idempotency_key: str
    amount: int = 10


async def reserve_batch(
    self, user_id: str, requests: Sequence[ReservationRequest]
) -> Wallet:
    if not requests or any(item.amount != 10 for item in requests):
        raise ValueError("invalid_mcp_reservation_batch")
    wallet = await self.get_wallet(user_id, for_update=True)
    unapplied = [item for item in requests if not await self._already_applied(item.idempotency_key)]
    required = sum(item.amount for item in unapplied)
    if wallet.balance < required:
        raise InsufficientPointsError()
    for item in unapplied:
        await self._record(
            wallet,
            kind="reserve",
            balance_delta=-item.amount,
            reserved_delta=item.amount,
            idempotency_key=item.idempotency_key,
            reference_type="mcp_call",
            reference_id=item.reference_id,
        )
    return wallet
```

`execute_batch()` 固定为三段：事务 A 创建该批所有调用并一次性全量预留；预留失败则整批不启动。事务 B 分别 claim 为 running 后提交；事务外只对成功 claim 的调用并行访问 Transport 一次；事务 C 按调用把成功证据、`settle` 和事件同事务提交，或把明确失败、`release` 和失败事件同事务提交。`unknown` 只写状态和错误，不结算/释放。`execute()` 是单元素批次的便利包装，二者共享同一实现。

幂等键固定为 `mcp:{logical_call_id}:reserve|settle|release`。数据库唯一约束保证一个调用至多一个成功消费。结算与释放竞争时，调用行锁下只有一个终态转换成功。

- [ ] **Step 4: 运行真实 MySQL 并发与原子性测试**

Run: `cd backend && .venv/bin/pytest tests/billing tests/mcp_gateway/test_billing_lifecycle.py tests/mcp_gateway/test_billing_atomicity.py -q`

Expected: 最后 10 分的 10 个独立 SessionFactory 竞争恰好 1 成功；批次余额不足写入 0 条；结算/释放只产生一个终态；异常回滚后可补结算但不重调 MCP。

- [ ] **Step 5: 提交实时计费**

```bash
git add backend/app/billing/service.py backend/app/mcp_gateway backend/tests/billing backend/tests/mcp_gateway
git commit -m "feat: settle MCP calls through atomic point ledger"
```

### Task 7: 实现 Context Builder、严格 Planner 和确定性批次

**Files:**
- Create: `backend/app/orchestration/__init__.py`
- Create: `backend/app/orchestration/schemas.py`
- Create: `backend/app/orchestration/context.py`
- Create: `backend/app/orchestration/planner.py`
- Create: `backend/app/orchestration/batching.py`
- Test: `backend/tests/orchestration/test_context.py`
- Test: `backend/tests/orchestration/test_planner.py`
- Test: `backend/tests/orchestration/test_batching.py`

**Interfaces:**
- Produces: `ContextBuilder.build()`、`Planner.plan()`、`build_execution_batches()`。
- Consumes: Task 4 模型接口、Task 5 本地工具目录、会话/消息、用户渠道权限。

- [ ] **Step 1: 写 Prompt 隔离、调用上限、禁用工具和 DAG 失败测试**

```python
# backend/tests/orchestration/test_planner.py
async def test_plan_rejects_disabled_service_before_mcp(
    planner, fake_model, fake_mcp_transport
) -> None:
    fake_model.structured_result = plan_with_tool("google-trends-mcp.search")

    with pytest.raises(PlanValidationError) as caught:
        await planner.plan(context_fixture())

    assert caught.value.code == "TOOL_NOT_ALLOWED"
    assert fake_mcp_transport.call_count == 0


async def test_plan_rejects_more_than_ten_calls(planner, fake_model) -> None:
    fake_model.structured_result = plan_with_repeated_calls(count=11)
    with pytest.raises(PlanValidationError) as caught:
        await planner.plan(context_fixture())
    assert caught.value.code == "TOO_MANY_TOOL_CALLS"
```

```python
# backend/tests/orchestration/test_context.py
async def test_model_context_contains_reviewed_tools_but_no_supplier_details(builder) -> None:
    context = await builder.build(user_id="user-1", session_id="session-1")
    serialized = context.model_dump_json()
    assert "达人搜索" in serialized
    assert "datatap.deepminer.com.cn" not in serialized
    assert "authorization" not in serialized.lower()
    assert "google-trends-mcp" not in serialized
```

- [ ] **Step 3: 实现严格计划 Schema 与批次构建**

```python
# backend/app/orchestration/schemas.py
class ToolPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^step_[0-9]+$")
    internal_tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, JsonValue]
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    evidence_goal: str = Field(min_length=1, max_length=300)


class ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=1000)
    steps: list[ToolPlanStep] = Field(min_length=1, max_length=10)
    stop_conditions: list[str] = Field(default_factory=list, max_length=10)
```

```python
# backend/app/orchestration/context.py
class ContextBuilder:
    async def build(self, user_id: str, session_id: str) -> PlannerContext:
        workspace = await self.workspace.get_owned_session(user_id, session_id)
        messages = await self.workspace.list_messages(user_id, session_id)
        tools = await self.registry.list_enabled_for_user(user_id)
        return PlannerContext(
            brief=SessionBrief.from_workspace(workspace),
            recent_messages=compress_messages(messages, max_chars=24_000),
            existing_results=await self.reporting.context_summary(session_id),
            tools=tuple(PlannerTool.from_approved(item) for item in tools),
        )
```

`Planner.plan()` 使用 `planner_v1` 和 `ToolPlan`；模型适配层负责一次结构再生成，业务校验不再要求模型重试。后端校验用户渠道、工具存在、严格参数、调用数、依赖引用和无环图。`build_execution_batches()` 用稳定拓扑排序，批次内按 step ID 排序，输出 `tuple[ExecutionBatch, ...]`；循环依赖抛 `PLAN_DEPENDENCY_CYCLE`。

- [ ] **Step 4: 验证计划和批次排序**

Run: `cd backend && .venv/bin/pytest tests/orchestration -q`

Expected: 调用上限、白名单、参数、DAG、稳定批次和 Prompt 隔离测试全部通过；不产生网络请求。

- [ ] **Step 5: 提交编排规划**

```bash
git add backend/app/orchestration backend/tests/orchestration
git commit -m "feat: add constrained KOL analysis planner"
```

### Task 8: 实现异步执行器、租约、取消与重启恢复

**Files:**
- Create: `backend/app/tasks/executor.py`
- Create: `backend/app/tasks/recovery.py`
- Create: `backend/app/tasks/dependencies.py`
- Modify: `backend/app/tasks/service.py`
- Modify: `backend/app/tasks/router.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/tasks/fakes.py`
- Test: `backend/tests/tasks/test_executor.py`
- Test: `backend/tests/tasks/test_cancellation.py`
- Test: `backend/tests/tasks/test_recovery.py`

**Interfaces:**
- Produces: `TaskExecutor.run()`、`TaskRecovery.recover_expired()`、应用生命周期恢复扫描。
- Consumes: Task 3 事件、Task 6 MCP/计费、Task 7 Planner；Task 9 将注入报告阶段实现。

- [ ] **Step 1: 写断线不取消、unknown 不重放和崩溃检查点失败测试**

```python
# backend/tests/tasks/test_cancellation.py
async def test_closing_event_stream_does_not_cancel_running_task(
    running_scenario, event_stream
) -> None:
    execution = asyncio.create_task(running_scenario.executor.run(running_scenario.task.id))
    stream = event_stream.stream(running_scenario.task.id, running_scenario.user.id, 0)
    await anext(stream)
    await stream.aclose()
    running_scenario.fake_mcp.release_success()
    await execution

    assert (await running_scenario.reload_task()).status == TaskStatus.COMPLETED
    assert running_scenario.fake_mcp.cancel_count == 0
```

```python
# backend/tests/tasks/test_recovery.py
@pytest.mark.parametrize(
    "crash_at",
    ["after_reserve", "after_mcp_result", "after_settle", "after_candidates", "after_bi"],
)
async def test_recovery_reuses_persisted_success(crash_at: str, crash_scenario) -> None:
    scenario = await crash_scenario(crash_at)
    with pytest.raises(InjectedProcessCrash):
        await scenario.executor.run(scenario.task.id)

    await scenario.new_recovery().recover_expired()

    assert (await scenario.reload_task()).status == TaskStatus.COMPLETED
    assert scenario.fake_mcp.successful_logical_calls == 1
    assert await scenario.wallet_tuple() == (990, 0)
```

- [ ] **Step 3: 实现租约驱动的进程内 Runner**

```python
# backend/app/tasks/executor.py
class TaskExecutor:
    async def run(self, task_id: str) -> None:
        task = await self.repository.claim_lease(task_id, self.worker_id, self.lease_seconds)
        if task is None:
            return
        try:
            plan = await self._load_or_create_plan(task)
            for batch in build_execution_batches(plan):
                if await self.repository.cancel_requested(task.id):
                    await self._cancel_unstarted(task, batch)
                    return
                await self._reserve_and_execute_batch(task, batch)
                await self.repository.renew_lease(task.id, self.worker_id, self.lease_seconds)
            await self._build_results_and_summary(task)
        except asyncio.CancelledError:
            await self.repository.mark_interrupted(task.id, self.worker_id)
            raise
        finally:
            await self.repository.release_lease(task.id, self.worker_id)
```

任务路由完成消息、任务和首事件写入后显式 `await db.commit()`，再调用进程级 `TaskRunner.submit(task.id)`；提交失败或进程在两步之间退出时，任务保持 `pending` 并由恢复扫描接管，绝不在事务提交前访问模型或 MCP。Runner 使用 `asyncio.create_task()` 并持有强引用集合，在应用 shutdown 时等待短暂收敛，未完成任务标记 `interrupted`。每个阶段先检查已有持久化产物：已有 plan 不重跑 Planner；`settled` 调用复用证据；`unknown` 交给协调器；候选/BI 已存在则跳过；仅总结失败只重跑 Analyst/Summary。

`TaskRecovery.recover_expired()` 扫描租约过期的 planning/running/interrupted 任务，使用行锁重新 claim。unknown 超过观察期且无成功证据时同事务释放 10 分并发 `points.released`；不得重调 Transport。

应用 lifespan 启动恢复扫描和固定间隔协调器，关闭时停止接收新任务并标记未完成租约。测试通过依赖注入 fake model/MCP，不读取真实凭据。

- [ ] **Step 4: 验证恢复、取消与租约**

Run: `cd backend && .venv/bin/pytest tests/tasks/test_executor.py tests/tasks/test_cancellation.py tests/tasks/test_recovery.py -q`

Expected: 所有崩溃检查点恢复后 MCP 成功逻辑调用为 1、钱包无冻结；unknown 不重放；用户取消停止未开始调用；SSE 断线不取消任务。

- [ ] **Step 5: 提交可恢复执行器**

```bash
git add backend/app/tasks backend/app/main.py backend/tests/tasks
git commit -m "feat: execute and recover analysis tasks in process"
```

### Task 9: 实现 KOL 标准化、去重和确定性评分

**Files:**
- Create: `backend/app/reporting/schemas.py`
- Create: `backend/app/reporting/normalizers.py`
- Create: `backend/app/reporting/scoring.py`
- Create: `backend/app/reporting/service.py`
- Create: `backend/tests/reporting/fakes.py`
- Test: `backend/tests/reporting/test_normalizers.py`
- Test: `backend/tests/reporting/test_scoring.py`
- Test: `backend/tests/reporting/test_candidate_versions.py`

**Interfaces:**
- Produces: `normalize_tool_evidence()`、`score_candidate()`、`ReportingService.build_candidate_version()`。
- Consumes: Task 5 成功证据、Task 2 报告模型、Task 8 执行检查点。

- [ ] **Step 1: 写身份去重、缺失数据、权重和稳定排序失败测试**

```python
# backend/tests/reporting/test_normalizers.py
def test_same_nickname_on_different_platform_accounts_is_not_merged() -> None:
    rows = normalize_tool_evidence([
        evidence(platform="bilibili", account_id="100", nickname="同名达人"),
        evidence(platform="bilibili", account_id="200", nickname="同名达人"),
    ])
    assert [(row.platform, row.platform_account_id) for row in rows] == [
        ("bilibili", "100"), ("bilibili", "200")
    ]


def test_missing_metrics_stay_none_and_are_reported() -> None:
    row = normalize_tool_evidence([evidence(platform="bilibili", account_id="100")])[0]
    assert row.engagement_rate is None
    assert "engagement_rate" in row.missing_fields
```

```python
# backend/tests/reporting/test_scoring.py
def test_balanced_profile_uses_confirmed_weights() -> None:
    result = score_candidate(all_dimensions(80), profile="balanced")
    assert result.total == 80
    assert result.weights == {
        "audience": 25, "content": 20, "engagement": 20,
        "budget": 15, "growth": 10, "brand_safety": 10,
    }


def test_missing_dimension_is_not_fabricated_as_observed_zero() -> None:
    result = score_candidate(all_dimensions(80, engagement=None), profile="balanced")
    assert result.dimensions["engagement"].raw_score is None
    assert result.dimensions["engagement"].weighted_score == 0
    assert result.data_completeness == 80
```

- [ ] **Step 3: 实现统一结构、评分版本和候选快照**

```python
# backend/app/reporting/scoring.py
SCORE_VERSION = "kol_score_v1"
WEIGHT_PROFILES: dict[str, dict[str, int]] = {
    "balanced": {
        "audience": 25, "content": 20, "engagement": 20,
        "budget": 15, "growth": 10, "brand_safety": 10,
    },
    "audience_first": {
        "audience": 35, "content": 20, "engagement": 15,
        "budget": 10, "growth": 10, "brand_safety": 10,
    },
    "performance_first": {
        "audience": 20, "content": 25, "engagement": 30,
        "budget": 10, "growth": 10, "brand_safety": 5,
    },
    "budget_first": {
        "audience": 20, "content": 15, "engagement": 15,
        "budget": 30, "growth": 10, "brand_safety": 10,
    },
    "risk_first": {
        "audience": 20, "content": 15, "engagement": 15,
        "budget": 10, "growth": 15, "brand_safety": 25,
    },
}


def score_candidate(dimensions: DimensionInputs, profile: str) -> CandidateScore:
    weights = WEIGHT_PROFILES[profile]
    details = {
        name: DimensionScore(
            raw_score=value,
            weight=weights[name],
            weighted_score=0 if value is None else round(value * weights[name] / 100, 2),
        )
        for name, value in dimensions.as_mapping().items()
    }
    observed = sum(item.raw_score is not None for item in details.values())
    return CandidateScore(
        version=SCORE_VERSION,
        total=round(sum(item.weighted_score for item in details.values()), 2),
        weights=weights,
        dimensions=details,
        data_completeness=round(observed / len(details) * 100),
    )
```

`normalizers.py` 按内部工具名注册适配器，输出统一 `NormalizedKolEvidence`：平台身份、粉丝、互动、内容、受众、报价、增长、风险、采集时间、证据引用和缺失字段。原始缺失保持 `None`；百分比、货币和单位入库前规范化。外部工具名未知时拒绝，不用通用猜测解析器。

`ReportingService.build_candidate_version(task_id, profile)` 在同一事务中：按 `(platform, platform_account_id)` upsert `Kol`；写不可变 `KolSnapshot`；计算下一候选版本；按总分、受众分、互动分、平台账号 ID 稳定排序；写 `TaskCandidate`。重复恢复时发现同任务同证据摘要的版本直接复用。

`backend/tests/reporting/fakes.py` 定义 `evidence()`、`all_dimensions()`、`candidate_fixture()`、`report_fixture()` 和对应 factories；Task 10 复用相同对象建立已完成任务，避免测试各自发明候选字段。

- [ ] **Step 4: 验证标准化、评分和候选版本幂等**

Run: `cd backend && .venv/bin/pytest tests/reporting -q`

Expected: 五种权重均合计 100；缺失值不伪造；同名不同账号不合并；相同证据恢复不新增版本；稳定排序结果固定。

- [ ] **Step 5: 提交候选计算**

```bash
git add backend/app/reporting backend/tests/reporting
git commit -m "feat: normalize and score KOL candidates"
```

### Task 10: 生成版本化 BI 并提供候选、报告和收藏 API

**Files:**
- Modify: `backend/app/reporting/schemas.py`
- Modify: `backend/app/reporting/service.py`
- Create: `backend/app/reporting/router.py`
- Modify: `backend/app/tasks/executor.py`
- Modify: `backend/app/workspace/schemas.py`
- Modify: `backend/app/workspace/router.py`
- Modify: `backend/app/api/router.py`
- Test: `backend/tests/reporting/test_reporting_api.py`
- Test: `backend/tests/reporting/test_favorites_api.py`
- Test: `backend/tests/workspace/test_session_analysis_restore.py`

**Interfaces:**
- Produces: candidates/report/favorites API、`ReportingService.build_bi_report()`、会话最新分析引用。
- Consumes: Task 9 候选版本、Task 4 Analyst/Summary、Task 3 所有权。

- [ ] **Step 1: 写候选排序、报告版本、收藏隔离和历史恢复失败测试**

```python
# backend/tests/reporting/test_reporting_api.py
async def test_candidates_are_sortable_and_report_matches_candidate_version(
    auth_client, completed_task
) -> None:
    candidates = await auth_client.get(
        f"/api/v1/tasks/{completed_task.id}/candidates",
        params={"sort": "engagement", "direction": "desc"},
    )
    report = await auth_client.get(f"/api/v1/reports/{completed_task.report_id}")

    assert candidates.status_code == 200
    body = candidates.json()
    assert body["items"][0]["scores"]["engagement"] >= body["items"][1]["scores"]["engagement"]
    assert report.json()["candidate_version"] == body["version"]
```

```python
# backend/tests/reporting/test_favorites_api.py
async def test_favorite_is_user_owned_and_idempotent(auth_client_factory, candidate) -> None:
    alice = await auth_client_factory("13800000101")
    bob = await auth_client_factory("13800000102")

    first = await alice.post("/api/v1/favorites", json={"kol_id": candidate.kol_id})
    second = await alice.post("/api/v1/favorites", json={"kol_id": candidate.kol_id})

    assert first.status_code in {200, 201}
    assert second.json()["kol_id"] == first.json()["kol_id"]
    assert (await bob.get("/api/v1/favorites")).json() == []
```

- [ ] **Step 3: 实现报告结构、API 和执行器最后阶段**

```python
# backend/app/reporting/schemas.py
class CandidatePage(BaseModel):
    task_id: str
    version: int
    total: int
    items: list[CandidateRead]


class BiReportRead(BaseModel):
    id: str
    task_id: str
    report_version: int
    candidate_version: int
    overview: OverviewSection
    score_composition: list[ScoreSeries]
    audience_content_fit: AudienceContentSection
    platform_distribution: list[ChartDatum]
    budget_analysis: BudgetSection
    comparison: list[CandidateComparison]
    risks: list[RiskItem]
    conclusion: str
    sources: list[EvidenceSource]
    generated_at: datetime


class FavoriteCreate(BaseModel):
    kol_id: str
    note: str | None = Field(default=None, max_length=500)
    source_task_id: str | None = None
```

`ReportingService.build_bi_report()` 固定引用最新已完成候选版本，结构化生成概览、分数组成、受众/内容、平台、预算、对比、风险和证据。Analyst 只生成严格 `AnalystConclusion`，服务端把它与确定性图表合并；模型不能改变候选分数或版本。报告写入成功后发 `bi.updated`，然后 Summary 流每个 delta 先追加消息草稿并提交，再发 `message.delta`，完成后发 `message.completed` 和 `task.completed`。

路由固定为：

```python
@router.get("/tasks/{task_id}/candidates", response_model=CandidatePage)
async def list_candidates(...): ...

@router.get("/reports/{report_id}", response_model=BiReportRead)
async def get_report(...): ...

@router.get("/favorites", response_model=list[FavoriteRead])
async def list_favorites(...): ...

@router.post("/favorites", response_model=FavoriteRead)
async def create_favorite(...): ...

@router.delete("/favorites/{kol_id}", status_code=204)
async def delete_favorite(...): ...
```

会话读取 DTO 增加 `latest_task`、`latest_candidates` 和 `latest_report` 可空字段；只返回当前用户拥有的对象。列表会话不展开大候选数据，单会话恢复才返回最新版本摘要。

- [ ] **Step 4: 验证 API、版本一致和会话恢复**

Run: `cd backend && .venv/bin/pytest tests/reporting/test_reporting_api.py tests/reporting/test_favorites_api.py tests/workspace/test_session_analysis_restore.py -q`

Expected: 排序/筛选、报告候选版本、收藏幂等、跨用户 404 和会话历史恢复全部通过。

- [ ] **Step 5: 提交报告和 API**

```bash
git add backend/app/reporting backend/app/tasks/executor.py backend/app/workspace backend/app/api/router.py backend/tests/reporting backend/tests/workspace
git commit -m "feat: expose versioned candidates reports and favorites"
```

### Task 11: 实现前端任务 API、fetch SSE 和幂等事件状态

**Files:**
- Modify: `src/api/client.ts`
- Modify: `src/api/contracts.ts`
- Create: `src/api/tasks.ts`
- Create: `src/api/favorites.ts`
- Create: `src/api/taskStream.ts`
- Create: `src/state/taskEvents.ts`
- Create: `src/state/taskEvents.test.ts`
- Create: `src/hooks/useTaskStream.ts`
- Create: `src/hooks/useTaskStream.test.tsx`
- Create: `src/test/fakeSse.ts`
- Modify: `src/hooks/useWorkspace.ts`
- Modify: `src/hooks/useWorkspace.test.tsx`
- Modify: `src/types.ts`

**Interfaces:**
- Produces: `createTask()`、`streamTaskEvents()`、`reduceTaskEvent()`、`useTaskStream()`、扩展后的 `useWorkspace()`。
- Consumes: Task 3/10 API，现有 access token 刷新逻辑。

- [ ] **Step 1: 写重复事件、重连和旧任务晚到失败测试**

```typescript
// src/state/taskEvents.test.ts
it('applies a duplicate event id exactly once', () => {
  const event: TaskEvent = {
    id: 41,
    taskId: 'task-1',
    type: 'message.delta',
    payload: { text: '推荐' },
  };
  const once = reduceTaskEvent(initialTaskRuntime('task-1'), event);
  const twice = reduceTaskEvent(once, event);
  expect(twice.assistantDraft).toBe('推荐');
  expect(twice.lastEventId).toBe(41);
});


it('shows BI only after its candidate version is present', () => {
  const early = reduceTaskEvent(initialTaskRuntime('task-1'), {
    id: 1, taskId: 'task-1', type: 'bi.updated',
    payload: { reportId: 'report-2', candidateVersion: 2 },
  });
  expect(early.visibleReportId).toBeUndefined();
  const ready = reduceTaskEvent(early, {
    id: 2, taskId: 'task-1', type: 'candidates.updated', payload: { version: 2 },
  });
  expect(ready.visibleReportId).toBe('report-2');
});
```

```typescript
// src/hooks/useTaskStream.test.tsx
it('reconnects from the last id without cancelling the task', async () => {
  const fake = installFetchSse();
  const cancel = vi.spyOn(tasksApi, 'cancelTask');
  renderHook(() => useTaskStream('task-1'));
  fake.emit({ id: 17, taskId: 'task-1', type: 'tool.started', payload: {} });
  fake.disconnect();
  await fake.waitForReconnect();
  expect(fake.lastRequestHeaders().get('Last-Event-ID')).toBe('17');
  expect(cancel).not.toHaveBeenCalled();
});
```

- [ ] **Step 3: 实现共享 DTO、SSE 解析和任务聚合**

```typescript
// src/api/client.ts
export async function authorizedFetch(
  path: string,
  init: RequestInit = {},
  retry = true,
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
  const response = await fetch(path, { ...init, headers, credentials: 'include' });
  if (response.status === 401 && retry && await refreshAccessToken()) {
    return authorizedFetch(path, init, false);
  }
  return response;
}
```

```typescript
// src/api/taskStream.ts
export async function streamTaskEvents(
  taskId: string,
  lastEventId: number,
  signal: AbortSignal,
  onEvent: (event: TaskEvent) => void,
): Promise<void> {
  const headers = new Headers({ Accept: 'text/event-stream' });
  if (lastEventId > 0) headers.set('Last-Event-ID', String(lastEventId));
  const response = await authorizedFetch(`/api/v1/tasks/${taskId}/events`, { headers, signal });
  if (!response.ok || !response.body) throw new Error(`SSE_${response.status}`);
  await parseSseStream(response.body, raw => onEvent(toTaskEvent(taskId, raw)));
}
```

```typescript
// src/state/taskEvents.ts
export function reduceTaskEvent(state: TaskRuntimeState, event: TaskEvent): TaskRuntimeState {
  if (event.taskId !== state.taskId || event.id <= state.lastEventId) return state;
  const next = { ...state, lastEventId: event.id };
  switch (event.type) {
    case 'message.delta':
      return { ...next, assistantDraft: next.assistantDraft + String(event.payload.text ?? '') };
    case 'candidates.updated':
      return exposeMatchingReport({ ...next, candidateVersion: Number(event.payload.version) });
    case 'bi.updated':
      return exposeMatchingReport({
        ...next,
        pendingReport: {
          id: String(event.payload.reportId),
          candidateVersion: Number(event.payload.candidateVersion),
        },
      });
    default:
      return applyStatusAndPointEvent(next, event);
  }
}
```

同文件导出 `initialTaskRuntime(taskId: string): TaskRuntimeState`，初值包含 `lastEventId=0`、空草稿、无候选/报告版本和 `connection='idle'`。`src/test/fakeSse.ts` 实现 `installFetchSse()`，可发事件、断开、等待重连并读取最后请求头；它不创建 HTTP 服务器。

`useTaskStream` 在 task ID 变化和卸载时只 abort 当前 fetch；非终态断线用带上限抖动退避重连，传最后事件 ID；终态停止重连。用户切换会话或退出后，旧 task ID 的事件由 reducer 拒绝。`useWorkspace.appendMessage()` 改为 `createTask()`，立即合并用户消息和 pending 任务，再由流更新；选择历史会话时加载最新候选与匹配报告。

- [ ] **Step 4: 验证事件和认证恢复**

Run: `npm run test -- src/state/taskEvents.test.ts src/hooks/useTaskStream.test.tsx src/hooks/useWorkspace.test.tsx`

Expected: 任务事件不重复；重连携带最后 ID；abort 不调用取消；旧用户/旧会话事件被忽略。

- [ ] **Step 5: 提交前端数据层**

```bash
git add src/api src/state src/hooks src/types.ts
git commit -m "feat: stream durable analysis state to the client"
```

### Task 12: 增加会话、候选清单、对比和收藏工作区

**Files:**
- Create: `src/components/WorkspaceTabs.tsx`
- Create: `src/components/WorkspaceTabs.test.tsx`
- Create: `src/components/CandidateList.tsx`
- Create: `src/components/CandidateList.test.tsx`
- Create: `src/components/CandidateCompare.tsx`
- Create: `src/components/FavoritesPanel.tsx`
- Create: `src/components/FavoritesPanel.test.tsx`
- Create: `src/test/fixtures.ts`
- Modify: `src/components/ChatArea.tsx`
- Modify: `src/components/ChatArea.test.tsx`
- Modify: `src/App.tsx`
- Modify: `src/components/MobileWorkspaceNav.tsx`
- Modify: `src/components/MobileWorkspaceNav.test.tsx`

**Interfaces:**
- Produces: 中间三标签、候选排序/筛选/多选、收藏和执行进度。
- Consumes: Task 11 workspace 状态与 Task 10 DTO。

- [ ] **Step 1: 写标签、稳定排序、对比和收藏失败测试**

```tsx
// src/components/CandidateList.test.tsx
it('sorts candidates and keeps selected ids for comparison', async () => {
  const user = userEvent.setup();
  render(<CandidateList page={candidatePage} onFavorite={vi.fn()} />);
  await user.click(screen.getByRole('button', { name: '互动率' }));
  expect(screen.getAllByTestId('candidate-name').map(node => node.textContent)).toEqual([
    '达人乙', '达人甲',
  ]);
  await user.click(screen.getByRole('checkbox', { name: '选择达人乙' }));
  await user.click(screen.getByRole('checkbox', { name: '选择达人甲' }));
  expect(screen.getByRole('button', { name: '对比 2 位达人' })).toBeEnabled();
});
```

```tsx
// src/components/WorkspaceTabs.test.tsx
it('exposes chat candidates and favorites with prototype styling', async () => {
  render(<WorkspaceTabs active="chat" onChange={vi.fn()} candidateCount={6} favoriteCount={2} />);
  expect(screen.getByRole('tab', { name: '智能会话' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByRole('tab', { name: '候选清单 6' })).toBeVisible();
  expect(screen.getByRole('tab', { name: '已收藏 2' })).toBeVisible();
});
```

- [ ] **Step 3: 组合中间工作区并保持原型视觉**

```tsx
// src/components/WorkspaceTabs.tsx
export function WorkspaceTabs({ active, onChange, candidateCount, favoriteCount }: Props) {
  const tabs = [
    { id: 'chat', label: '智能会话', icon: MessageSquare },
    { id: 'candidates', label: `候选清单 ${candidateCount}`, icon: Users },
    { id: 'favorites', label: `已收藏 ${favoriteCount}`, icon: Star },
  ] as const;
  return (
    <div role="tablist" className="flex h-11 border-b border-slate-200 bg-white px-4">
      {tabs.map(({ id, label, icon: Icon }) => (
        <button
          key={id}
          role="tab"
          aria-selected={active === id}
          onClick={() => onChange(id)}
          className={active === id
            ? 'flex items-center gap-1.5 border-b-2 border-indigo-600 px-3 text-[11px] font-semibold text-indigo-600'
            : 'flex items-center gap-1.5 px-3 text-[11px] font-medium text-slate-500 hover:text-slate-800'}
        >
          <Icon className="h-3.5 w-3.5" />{label}
        </button>
      ))}
    </div>
  );
}
```

`CandidateList` 使用语义表头按钮控制总分、分项、平台、粉丝、互动率、价格和风险排序；展示数据新鲜度、完整度、证据入口和收藏图标。对比最多选择 4 人，`CandidateCompare` 横向展示六分项、关键指标和风险。`FavoritesPanel` 调用真实收藏 API，跨会话展示，删除使用同一星形按钮样式。

`src/test/fixtures.ts` 提供与 `ApiCandidatePage`、`ApiBiReport` 完全同形的 `candidatePage`、`candidate`、`reportFixture()` 和缺失数据报告，Task 13 继续复用。

`ChatArea` 在消息流中展示 `plan.ready`、`tool.started/succeeded/failed/unknown`、积分预留/结算/释放、候选和 BI 更新；工具名称使用审核后的中文标签，绝不显示端点或原始响应。输入 busy 只阻止重复提交，SSE 重连时仍可查看历史。

`App` 保持左会话、中工作区、右 BI。桌面中间由 `WorkspaceTabs` 切换；窄屏 `MobileWorkspaceNav` 的“分析对话”进入中栏，中栏内部再显示三个标签，不增加第四个全局导航图标。

- [ ] **Step 4: 验证候选与收藏交互**

Run: `npm run test -- src/components/WorkspaceTabs.test.tsx src/components/CandidateList.test.tsx src/components/CandidateCompare.test.tsx src/components/FavoritesPanel.test.tsx`

Expected: 标签、排序、对比和收藏交互通过。

- [ ] **Step 5: 提交候选工作区**

```bash
git add src/components src/App.tsx
git commit -m "feat: add candidate comparison and favorites workspace"
```

### Task 13: 将右侧 BI 改为版本化 KOL 决策报告

**Files:**
- Modify: `src/components/BiReport.tsx`
- Create: `src/components/BiReport.test.tsx`
- Modify: `src/types.ts`
- Modify: `src/App.tsx`
- Test: `e2e/bi-report.spec.ts`

**Interfaces:**
- Produces: 与 `BiReportRead` 对齐的九段 BI、候选版本门控和空数据提示。
- Consumes: Task 11 可见报告、Task 12 选中候选。

- [ ] **Step 1: 写报告版本、空数据和原型控件失败测试**

```tsx
// src/components/BiReport.test.tsx
it('renders the nine KOL decision sections from a matching report', () => {
  render(<BiReport report={reportFixture({ candidateVersion: 3 })} candidateVersion={3} />);
  for (const title of [
    '任务概览', '评分构成', '受众与内容匹配', '平台分布', '预算与性价比',
    '候选对比', '风险与数据质量', 'AI 结论', '数据来源',
  ]) {
    expect(screen.getByText(title)).toBeVisible();
  }
});


it('does not render a report for another candidate version', () => {
  render(<BiReport report={reportFixture({ candidateVersion: 2 })} candidateVersion={3} />);
  expect(screen.getByText('正在同步最新候选与 BI 报告')).toBeVisible();
  expect(screen.queryByText('AI 结论')).not.toBeInTheDocument();
});


it('labels missing data instead of showing zero', () => {
  render(<BiReport report={reportWithMissingAudience} candidateVersion={1} />);
  expect(screen.getByText('受众数据不足')).toBeVisible();
  expect(screen.queryByText('0%')).not.toBeInTheDocument();
});
```

- [ ] **Step 3: 保留外壳并替换为 KOL 决策图表**

```tsx
// src/components/BiReport.tsx
export default function BiReport({ report, candidateVersion, selectedCandidates = [] }: Props) {
  if (!report) return <BiEmptyState />;
  if (report.candidateVersion !== candidateVersion) return <BiSyncingState />;
  return (
    <aside className="flex h-full w-full shrink-0 flex-col overflow-hidden border-l border-slate-200 bg-white shadow-sm xl:w-[420px]">
      <BiHeader reportVersion={report.reportVersion} generatedAt={report.generatedAt} />
      <div className="flex-1 space-y-3 overflow-y-auto bg-slate-50/40 p-3">
        <OverviewCard data={report.overview} />
        <ScoreCompositionChart data={report.scoreComposition} />
        <AudienceContentCard data={report.audienceContentFit} />
        <PlatformDistributionChart data={report.platformDistribution} />
        <BudgetCard data={report.budgetAnalysis} />
        <ComparisonCard data={report.comparison} selected={selectedCandidates} />
        <RiskCard items={report.risks} />
        <ConclusionCard text={report.conclusion} />
        <EvidenceCard sources={report.sources} />
      </div>
    </aside>
  );
}
```

复用原有卡片圆角、边框、阴影、Indigo 高亮、Lucide 图标、Recharts tooltip 和 `MetricHelper`，移除 MCN 履约率、旧情感占位和跨会话伪比较。所有图表对 `null` 显示“数据不足”，来源显示工具中文名、采集时间和证据编号，不显示内部端点。

- [ ] **Step 4: 验证 BI 组件和版本门控**

Run: `npm run test -- src/components/BiReport.test.tsx`

Expected: 九段报告、版本门控、空数据和图表测试通过。

- [ ] **Step 5: 提交 BI 报告**

```bash
git add src/components/BiReport.tsx src/components/BiReport.test.tsx src/types.ts src/App.tsx e2e/bi-report.spec.ts
git commit -m "feat: render versioned KOL decision BI"
```

### Task 14: 最小发布检查与运行手册

**Files:**
- Create: `backend/app/core/redaction.py`
- Create: `backend/tests/security/test_log_redaction.py`
- Create: `docs/runbooks/phase-2-runtime.md`
- Modify: `README.md`

**Interfaces:**
- Produces: 凭据脱敏、单体部署与恢复说明、一次性发布冒烟结果。
- Consumes: Task 1-13 已有 fake-only 功能和访问隔离测试。

- [ ] **Step 1: 实现递归脱敏与最小运行手册**

`redact_for_log()` 必须递归处理字典和列表，并遮蔽 authorization、cookie、手机号、腾讯模型密钥、DataTap token、JWT 密钥和 MySQL 密码。测试只断言敏感原值不会出现在序列化结果中；不新增指标、真实供应商或 E2E 基础设施。

运行手册只说明单个 Uvicorn worker、迁移、fake / production 环境变量、关闭新任务、租约恢复、unknown 协调、积分对账和回滚。真实 DataTap 或腾讯验证保留为用户另行授权后的手工操作，不写自动化测试。

- [ ] **Step 2: 执行一次最终发布冒烟**

Run: `cd backend && .venv/bin/pytest tests/security/test_log_redaction.py -q && .venv/bin/pytest -q`

Run: `npm run test && npm run lint && npm run build`

Run: `rg -n "(api[_-]?key|authorization|token)[[:space:]]*[:=][[:space:]]*['\"][^'\"]{16,}" backend src .env.example`

Expected: 后端和前端各执行一次完整回归，密钥扫描无硬编码凭据命中；不运行 Playwright、视觉基线、连续稳定性跑测或真实供应商测试。

- [ ] **Step 3: 提交最小发布材料**

```bash
git add backend/app/core/redaction.py backend/tests/security/test_log_redaction.py docs/runbooks/phase-2-runtime.md README.md
git commit -m "docs: add phase two runtime runbook"
```

<!-- 2026-07-15 已删除：下列原 Task 14 的实时供应商冒烟、指标、10 并发验收、Playwright、视觉回归及连续十次跑测。保留历史文本仅供追溯，不属于后续执行范围。

### Task 14: 完成并发、恢复、安全、视觉和运行验收

**Files:**
- Create: `backend/app/core/redaction.py`
- Create: `backend/app/core/metrics.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/security/test_log_redaction.py`
- Create: `backend/tests/security/test_metrics.py`
- Create: `backend/tests/integration/test_ten_concurrent_tasks.py`
- Create: `backend/tests/integration/test_crash_windows.py`
- Create: `backend/tests/live/test_tencent_plan_smoke.py`
- Create: `backend/tests/live/test_datatap_mcp_smoke.py`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/pyproject.toml`
- Modify: `playwright.config.ts`
- Create: `e2e/fixtures/phaseTwo.ts`
- Create: `e2e/task-recovery.spec.ts`
- Create: `e2e/phase2-concurrency.spec.ts`
- Create: `e2e/visual-regression.spec.ts`
- Create: `e2e/security.spec.ts`
- Modify: `package.json`
- Delete: `server.ts`
- Modify: `README.md`
- Create: `docs/runbooks/phase-2-runtime.md`

**Interfaces:**
- Produces: 第二阶段发布门槛、fake-only E2E、密钥脱敏和运行/恢复手册。
- Consumes: Task 1-13 全部功能。

- [ ] **Step 1: 写 10 并发、日志脱敏、断线恢复和跨用户失败测试**

```python
# backend/tests/integration/test_ten_concurrent_tasks.py
async def test_ten_tasks_complete_without_wallet_or_version_drift(scenario_factory) -> None:
    scenarios = [await scenario_factory(index) for index in range(10)]
    await asyncio.gather(*(item.executor.run(item.task.id) for item in scenarios))

    for item in scenarios:
        assert (await item.reload_task()).status == TaskStatus.COMPLETED
        assert await item.wallet_tuple() == (990, 0)
        assert await item.settlement_count() == 1
        assert await item.candidate_version() == await item.report_candidate_version()
```

```python
# backend/tests/security/test_log_redaction.py
def test_redaction_removes_auth_phone_and_supplier_tokens() -> None:
    rendered = redact_for_log({
        "phone": "13812345678",
        "authorization": "unit-test-authorization-value",
        "tencent_plan_api_key": "unit-test-model-value",
        "datatap_mcp_token": "unit-test-mcp-value",
    })
    serialized = json.dumps(rendered, ensure_ascii=False)
    for value in [
        "13812345678", "unit-test-authorization-value",
        "unit-test-model-value", "unit-test-mcp-value",
    ]:
        assert value not in serialized
```

```typescript
// e2e/task-recovery.spec.ts
test('disconnect does not cancel and reload restores candidates and BI', async ({ page }) => {
  await loginAndCreateAnalysis(page);
  await expect(page.getByText('正在获取达人数据')).toBeVisible();
  await page.context().setOffline(true);
  await page.context().setOffline(false);
  await expect(page.getByText('分析完成')).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('candidate-version')).toHaveText('1');
  await expect(page.getByTestId('bi-candidate-version')).toHaveText('1');
  await expect(page.getByText('990 / 5,000 点')).toBeVisible();
});
```

- [ ] **Step 2: 运行新增门槛并确认缺少 fake 场景或安全实现**

Run: `cd backend && .venv/bin/pytest tests/integration tests/security -q`

Expected: 缺少 `redact_for_log` 或完整场景夹具导致失败；不得把测试指向真实供应商。

- [ ] **Step 3: 完成测试基础设施、脱敏和遗留 Node 后端清理**

```python
# backend/app/core/redaction.py
from typing import Any


SENSITIVE_KEYS = {
    "authorization", "cookie", "set-cookie", "phone",
    "tencent_plan_api_key", "datatap_mcp_token", "jwt_secret", "mysql_password",
}


def redact_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_log(item) for item in value]
    return value
```

```python
# backend/app/core/metrics.py
from prometheus_client import Counter, Histogram


TASK_TERMINALS = Counter(
    "kol_task_terminal_total", "Analysis task terminal states", ("status", "error_type")
)
MODEL_DURATION = Histogram(
    "kol_model_duration_seconds", "Model latency", ("purpose", "status")
)
MCP_CALLS = Counter(
    "kol_mcp_calls_total", "MCP outcomes", ("service", "tool", "status")
)
POINT_TRANSITIONS = Counter(
    "kol_point_transitions_total", "Point reservation lifecycle", ("kind",)
)
SSE_CONNECTIONS = Counter(
    "kol_sse_connections_total", "SSE opens and reconnects", ("kind",)
)
```

`prometheus-client>=0.21,<1` 在 Task 1 加入运行依赖。Task 3、4、5、6、8 的服务在成功提交状态后递增对应指标，标签只允许低基数枚举，不使用 user/task/call ID。`test_metrics.py` 使用独立 registry 或指标样本差值断言成功、失败和 unknown 各记录一次，且指标文本不含密钥、手机号和完整 Prompt。

```python
# backend/app/main.py 的 create_app()
@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

指标端点只暴露低基数聚合值，测试断言不出现用户 ID、任务 ID、调用 ID、手机号、Prompt 或凭据。

Playwright 后端环境增加 `MODEL_PROVIDER=fake`、`MCP_PROVIDER=fake`，fake 响应固定为一次成功调用和两位候选；不定义任何真实供应商凭据。并发数据库夹具为每个竞争者创建独立 `SessionFactory`，不共享 `db_session`。

删除 `server.ts`、`dev:legacy`、Node `start`，并从 `package.json` 移除 `@google/genai`、Express、dotenv、tsx 和仅为旧 Node 服务存在的类型依赖。Vite 继续把 `/api` 代理到 FastAPI。

`docs/runbooks/phase-2-runtime.md` 写明以单个 Uvicorn worker 启动、迁移、fake/production 配置变量、关闭新任务、租约恢复、unknown 协调、积分对账、回滚和真实冒烟授权步骤。真实集成先在明确授权后执行五服务协议初始化与工具列表，把所有发现项置为 quarantine；人工审核并提交 manifest 后，才能调用一个白名单低风险工具。真实冒烟最多允许一次明确成功和 10 积分。

`pyproject.toml` 注册 `live` marker。两个 live 测试默认 `pytest.skip()`；只有对应 `RUN_TENCENT_LIVE_SMOKE=1` 或 `RUN_DATATAP_LIVE_SMOKE=1` 时才读取环境密钥。腾讯测试发送一个最小结构化规划和一个短流；DataTap 测试先验证 manifest 已审核，再调用一个明确选定工具一次。普通 `pytest` 通过 `-m "not live"` 或默认 skip 保证零外网。

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["live: opt-in smoke test that may call a configured external provider"]
```

- [ ] **Step 4: 执行后端、前端、E2E、视觉和敏感信息全量验证**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q`

Expected: Ruff 零错误；全量 pytest 零失败；10 并发无透支、冻结或重复结算；所有崩溃窗口恢复一致。

Run: `npm run test && npm run lint && npm run build`

Expected: Vitest 全部通过；TypeScript 零错误；Vite 构建成功。

Run: `npm run test:e2e -- e2e/auth-session-recovery.spec.ts e2e/task-recovery.spec.ts e2e/phase2-concurrency.spec.ts e2e/security.spec.ts --project=desktop-1440`

Expected: 登录、断线、10 并发和跨用户隔离全部通过；每个成功一次的用户钱包为 990 可用、0 预留。

Run: `npm run test:e2e -- e2e/visual-regression.spec.ts --project=desktop-1440 --project=mobile-390`

Expected: 人工审核过的运行中/完成态桌面和移动基线零差异，三栏比例、按钮、图标和 Indigo/Slate 风格保持一致。

Run: `rg -n "(api[_-]?key|authorization|token)[[:space:]]*[:=][[:space:]]*['\"][^'\"]{16,}" backend src .env.example`

Expected: 无硬编码凭据命中；测试值也使用明确的短期测试标识，不复制真实凭据格式。

最后把关键后端并发/恢复测试和 Playwright 断线测试连续执行 10 次，预期 10 次零失败；不得开启 pytest rerun 或 Playwright retries 掩盖竞态。

用户批准腾讯真实冒烟后运行：`cd backend && RUN_TENCENT_LIVE_SMOKE=1 .venv/bin/pytest -m live tests/live/test_tencent_plan_smoke.py -q`。Expected: 结构化结果和短流完成，日志不含密钥。

用户另行批准 DataTap 真实冒烟且 manifest 已审核后运行：`cd backend && RUN_DATATAP_LIVE_SMOKE=1 .venv/bin/pytest -m live tests/live/test_datatap_mcp_smoke.py -q`。Expected: 仅一个工具明确成功、钱包只结算 10 积分、调用记录与证据存在；失败则 0 积分。

- [ ] **Step 5: 提交第二阶段发布门槛**

```bash
git add backend/app/core backend/tests playwright.config.ts e2e package.json package-lock.json README.md docs/runbooks server.ts
git commit -m "test: add phase two reliability and release gates"
```
-->

## 设计覆盖自检

| 已批准设计内容 | 实施任务 |
|---|---|
| 腾讯 Token Plan、结构化输出、流式与错误分类 | Task 1、4 |
| 五服务白名单、四服务禁用、固定 HTTPS 与工具隔离 | Task 1、2、5 |
| 后端控制的 plan/execute/summarize 与 Prompt 版本 | Task 4、7、8、10 |
| 任务状态、持久化事件、SSE、取消、租约和恢复 | Task 2、3、8、11 |
| 成功 10 积分、批次预留、unknown 和断线计费 | Task 2、5、6、8 |
| KOL、快照、候选、报告、收藏数据模型 | Task 2、9、10 |
| 六维确定性评分和五种配置 | Task 9 |
| 候选排序、筛选、对比和跨会话收藏 | Task 10、12 |
| 右侧九段 BI、版本一致和数据证据 | Task 9、10、13 |
| API、fetch SSE 和前端历史恢复 | Task 3、10、11 |
| 安全脱敏、关键并发、崩溃恢复和原型一致性 | Task 3、6、8、10、14 |
| fake-only 自动化与运行 / 回滚手册 | Task 14 |

自检结果：设计的所有实施性要求均有对应任务；真实充值、真实短信、微信 OAuth、独立 Worker 和微服务仍保持在本阶段范围之外。

## 阶段验收检查点

### 第二阶段 A：基础契约

- Task 1-6 已提交，迁移到 `0003` 成功。
- fake model/MCP 可跑通结构化规划、工具发现、成功/失败/unknown 和积分状态。
- 四个禁用服务在任何网络动作前被拒绝。
- 任务事件可持久化、鉴权、回放和去重。

### 第二阶段 B：业务闭环

- Task 7-13 已提交，完整 fake 选人流程可用。
- 候选可排序、筛选、对比、收藏，历史会话可恢复。
- 候选版本与 BI 版本一致，AI 不能改写确定性评分。
- 前端保留原型视觉，桌面三栏和移动切换均可用。

### 第二阶段 C：最小发布检查

- Task 14 的脱敏测试、一次后端完整回归和一次前端完整构建通过，无真实供应商请求。
- 资金并发与恢复不重放由 Task 6、Task 8 的 focused MySQL 测试覆盖；跨用户访问隔离由 Task 3、Task 10 的 API 测试覆盖。
- 真实腾讯或 DataTap 调用保持人工授权的后续运维事项，不纳入本阶段自动化验收。
