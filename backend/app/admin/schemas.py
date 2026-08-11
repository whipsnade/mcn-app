from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class AdminUserItem(BaseModel):
    id: str
    tenant_id: str | None = None
    nickname: str
    role: str
    status: str
    phone: str | None
    points: int
    reserved_points: int
    channels: list[str]
    industries: list[str]
    created_at: datetime


class AdminUserListResponse(BaseModel):
    items: list[AdminUserItem]
    total: int


class AdminUserCreate(BaseModel):
    nickname: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=1, max_length=32)
    role: str = Field(pattern="^(user|admin)$")
    points: int = Field(default=0, ge=0, le=50000)
    channels: list[str] = Field(default_factory=list)


class AdminUserUpdate(BaseModel):
    nickname: str | None = Field(default=None, min_length=1, max_length=80)
    phone: str | None = Field(default=None, min_length=1, max_length=32)
    role: str | None = Field(default=None, pattern="^(user|admin)$")
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    channels: list[str] | None = None
    industries: list[Annotated[str, Field(min_length=1, max_length=20)]] | None = Field(
        default=None, max_length=5
    )


class PointsAdjustRequest(BaseModel):
    delta: int
    reason: str = Field(min_length=1, max_length=200)


class PointsAdjustResponse(BaseModel):
    tenant_id: str | None = None
    points: int
    reserved_points: int
    transaction_id: str


class PointsHistoryEntry(BaseModel):
    id: str
    kind: str
    points: int
    session_title: str | None
    platform: str | None
    created_at: datetime


class PointsHistoryResponse(BaseModel):
    items: list[PointsHistoryEntry]
    total: int


class AgentToolCallReconcileRequest(BaseModel):
    """管理员核对 result_unknown 的 Agent 工具调用。

    - confirm_success：可确认成功。能取回 payload 时创建 Evidence 并结算，
      否则只结算并标记结果不可用（不得伪造 Evidence）；
    - confirm_failure：确认失败，释放预留；
    - keep_unknown：无法核对，保持 reserved/unknown 并追加核对审计。
    """

    decision: Literal["confirm_success", "confirm_failure", "keep_unknown"]
    note: str | None = Field(default=None, max_length=500)


class AgentToolCallReconcileResponse(BaseModel):
    call_id: str
    status: str
    error_type: str | None = None
    points_reserved: int
    points_settled: int
    evidence_id: str | None = None


# ---------------------------------------------------------------------------
# Pi Agent Gateway administration (B6A)
# ---------------------------------------------------------------------------


class AdminTenantItem(BaseModel):
    id: str
    slug: str
    name: str
    status: Literal["active", "disabled"]
    is_internal: bool
    runtime_backend: Literal["current", "pi"]
    license_status: Literal["active", "suspended"]
    active_license_id: str | None = None
    active_runtime_config_id: str | None = None
    member_count: int = 0
    active_run_count: int = 0
    created_at: datetime
    updated_at: datetime


class AdminTenantCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]{0,78}[a-z0-9]$")
    name: str = Field(min_length=1, max_length=160)
    is_internal: bool = False


class AdminTenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    status: Literal["active", "disabled"] | None = None
    runtime_backend: Literal["current", "pi"] | None = None


class AdminTenantUserItem(BaseModel):
    id: str
    nickname: str
    role: Literal["owner", "admin", "member"]
    status: Literal["active", "disabled"]
    created_at: datetime


class AdminTenantUserCreate(BaseModel):
    nickname: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=1, max_length=32)
    role: Literal["owner", "admin", "member"] = "member"
    points: int = Field(default=0, ge=0, le=50000)


class AdminWalletAdjustRequest(BaseModel):
    """租户钱包人工调整（admin_adjust 账本 + 审计）。"""

    user_id: str = Field(min_length=1, max_length=64)
    delta: int = Field(ne=0, ge=-1_000_000, le=1_000_000)
    reason: str = Field(min_length=1, max_length=200)


class AdminWalletAdjustResponse(BaseModel):
    tenant_id: str
    balance: int
    reserved: int
    transaction_id: str


class AdminWalletItem(BaseModel):
    """租户钱包只读投影。"""

    tenant_id: str
    balance: int
    reserved: int


class AdminQuotaPolicyItem(BaseModel):
    user_id: str
    period: Literal["monthly"]
    points_limit: int
    status: Literal["active", "disabled"]


class AdminQuotaPolicyUpdate(BaseModel):
    points_limit: int = Field(ge=0, le=10_000_000)


class AdminUserDisableResult(BaseModel):
    """legacy 用户软禁用的幂等回放投影（路由层始终返回 204）。"""

    id: str
    status: Literal["disabled"] = "disabled"


class AdminLicenseItem(BaseModel):
    id: str
    tenant_id: str
    version: int
    valid_from: datetime
    valid_until: datetime | None = None
    features: dict[str, bool]
    max_concurrent_runs: int
    max_user_concurrent_runs: int
    active: bool
    created_at: datetime


class AdminLicenseCreate(BaseModel):
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    features: dict[str, bool]
    max_concurrent_runs: int = Field(ge=1, le=1000)
    max_user_concurrent_runs: int = Field(ge=1, le=1000)


class AdminLicenseStatusUpdate(BaseModel):
    status: Literal["active", "suspended"]


class AdminUsageItem(BaseModel):
    tenant_id: str
    user_id: str | None = None
    run_id: str | None = None
    day: str | None = None
    record_count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_micros: int | None = None
    priced_cost_micros: int = 0
    usage_unavailable_count: int = 0
    unpriced_count: int = 0


class AdminUsageResponse(BaseModel):
    items: list[AdminUsageItem]
    limit: int
    offset: int


class AdminGatewayItem(BaseModel):
    id: str
    gateway_id: str
    status: Literal["active", "offline", "disabled"]
    mode: Literal["active", "draining"]
    desired_capacity: int
    last_seen_at: datetime | None = None
    updated_at: datetime


class AdminGatewayUpdate(BaseModel):
    desired_capacity: int | None = Field(default=None, ge=1, le=128)
    mode: Literal["active", "draining"] | None = None


class AdminRuntimeConfigItem(BaseModel):
    id: str
    scope: Literal["system", "tenant"]
    tenant_id: str | None = None
    version: int
    status: Literal["draft", "active", "retired"]
    runtime_backend: Literal["current", "pi"]
    runtime_contract_version: str
    model: dict[str, Any]
    datatap: dict[str, Any]
    limits: dict[str, Any]
    billing: dict[str, Any]
    secret_refs: list[dict[str, str]]
    created_by: str | None = None
    created_at: datetime
    activated_at: datetime | None = None


class AdminRuntimeConfigCreate(BaseModel):
    tenant_id: str
    runtime_backend: Literal["current", "pi"]
    model: dict[str, Any]
    datatap: dict[str, Any]
    limits: dict[str, int | float]
    billing: dict[str, Any]
    secrets: dict[str, Any] | None = None
    runtime_contract_version: str = "marketing_runtime_v1"


class AdminRunDiagnostics(BaseModel):
    run: dict[str, Any]
    attempts: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    events: list[dict[str, Any]]
    usage: list[dict[str, Any]]
    reconciliation: dict[str, Any] | None = None
