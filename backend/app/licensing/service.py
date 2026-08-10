from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun
from app.licensing.models import TenantLicense
from app.identity.models import User
from app.tenancy.models import Tenant, TenantMembership


class LicenseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    code: Literal[
        "ok",
        "license_inactive",
        "license_not_started",
        "license_expired",
        "feature_disabled",
        "tenant_concurrency_exceeded",
        "user_concurrency_exceeded",
    ]
    max_tenant_concurrency: int
    max_user_concurrency: int


class LicenseService:
    ACTIVE_RUN_STATUSES = ("queued", "running", "reviewing")

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def authorize_feature(self, tenant_id: str, user_id: str, feature: str) -> bool:
        """Check a feature without counting the already-running Run.

        MCP preflight happens inside an existing Pi Run, so reusing
        ``authorize_run`` would incorrectly trip the concurrency ceiling.
        """
        decision = await self.authorize_feature_decision(tenant_id, user_id, feature)
        return decision.allowed

    async def authorize_feature_decision(
        self, tenant_id: str, user_id: str, feature: str
    ) -> LicenseDecision:
        """Same semantics as ``authorize_feature`` with the stable denial code."""
        row = await self.db.execute(
            select(Tenant, TenantLicense)
            .join(TenantLicense, TenantLicense.id == Tenant.active_license_id)
            .join(TenantMembership, TenantMembership.tenant_id == Tenant.id)
            .join(User, User.id == TenantMembership.user_id)
            .where(
                Tenant.id == tenant_id,
                TenantMembership.user_id == user_id,
                TenantMembership.status == "active",
                User.status == "active",
            )
        )
        result = row.one_or_none()
        if result is None:
            return self._denied("license_inactive")
        tenant, license_row = result
        if tenant.status != "active" or tenant.license_status != "active":
            return self._denied("license_inactive")
        now = datetime.now(UTC).replace(tzinfo=None)
        if license_row.valid_from > now:
            return self._denied("license_not_started")
        if license_row.valid_until is not None and license_row.valid_until <= now:
            return self._denied("license_expired")
        if license_row.features_json.get(feature) is not True:
            return self._denied("feature_disabled")
        return LicenseDecision(
            allowed=True,
            code="ok",
            max_tenant_concurrency=license_row.max_concurrent_runs,
            max_user_concurrency=license_row.max_user_concurrent_runs,
        )

    async def authorize_run(
        self, tenant_id: str, user_id: str, feature: str
    ) -> LicenseDecision:
        row = await self.db.execute(
            select(Tenant, TenantLicense)
            .join(TenantLicense, TenantLicense.id == Tenant.active_license_id)
            .join(
                TenantMembership,
                TenantMembership.tenant_id == Tenant.id,
            )
            .join(User, User.id == TenantMembership.user_id)
            .where(
                Tenant.id == tenant_id,
                TenantMembership.user_id == user_id,
                TenantMembership.status == "active",
                User.status == "active",
            )
        )
        result = row.one_or_none()
        if result is None:
            return self._denied("license_inactive")
        tenant, license_row = result
        if tenant.status != "active" or tenant.license_status != "active":
            return self._denied("license_inactive")
        now = datetime.now(UTC).replace(tzinfo=None)
        if license_row.valid_from > now:
            return self._denied("license_not_started")
        if license_row.valid_until is not None and license_row.valid_until <= now:
            return self._denied("license_expired")
        if license_row.features_json.get(feature) is not True:
            return self._denied("feature_disabled")

        tenant_count = await self.db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.status.in_(self.ACTIVE_RUN_STATUSES),
            )
        )
        user_count = await self.db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.user_id == user_id,
                AgentRun.status.in_(self.ACTIVE_RUN_STATUSES),
            )
        )
        if (tenant_count or 0) >= license_row.max_concurrent_runs:
            return self._denied(
                "tenant_concurrency_exceeded",
                license_row.max_concurrent_runs,
                license_row.max_user_concurrent_runs,
            )
        if (user_count or 0) >= license_row.max_user_concurrent_runs:
            return self._denied(
                "user_concurrency_exceeded",
                license_row.max_concurrent_runs,
                license_row.max_user_concurrent_runs,
            )
        return LicenseDecision(
            allowed=True,
            code="ok",
            max_tenant_concurrency=license_row.max_concurrent_runs,
            max_user_concurrency=license_row.max_user_concurrent_runs,
        )

    @staticmethod
    def _denied(
        code: str, max_tenant_concurrency: int = 0, max_user_concurrency: int = 0
    ) -> LicenseDecision:
        return LicenseDecision(
            allowed=False,
            code=code,
            max_tenant_concurrency=max_tenant_concurrency,
            max_user_concurrency=max_user_concurrency,
        )
