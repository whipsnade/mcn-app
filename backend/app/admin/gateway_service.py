"""Audited, secret-free administration for the Pi Agent Gateway (B6A)."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.admin.models import AdminAuditLog, AdminIdempotencyRecord
from app.admin.schemas import (
    AdminGatewayItem,
    AdminUserCreate,
    AdminUserDisableResult,
    AdminUserItem,
    AdminUserUpdate,
    AdminGatewayUpdate,
    AdminLicenseCreate,
    AdminLicenseItem,
    AdminLicenseStatusUpdate,
    AdminQuotaPolicyItem,
    AdminQuotaPolicyUpdate,
    AdminRunDiagnostics,
    AdminRuntimeConfigCreate,
    AdminRuntimeConfigItem,
    AdminTenantCreate,
    AdminTenantItem,
    AdminTenantUpdate,
    AdminTenantUserCreate,
    AdminTenantUserItem,
    AdminWalletAdjustRequest,
    AdminWalletAdjustResponse,
    AdminWalletItem,
)
from app.agent_runtime.models import (
    AgentEvent,
    AgentRun,
    AgentRunAttempt,
    AgentStep,
    AgentToolCall,
)
from app.billing.models import RuntimeUsageRecord, TenantUserQuotaPolicy, TenantWallet
from app.core.config import get_settings
from app.core.redaction import redact_for_log
from app.identity.models import AuthIdentity, User
from app.licensing.models import TenantLicense
from app.pi_gateway.accounting import (
    RuntimeUsageService,
    TenantAccountingService,
    TenantWalletInsufficientError,
)
from app.pi_gateway.models import PiGatewayInstance
from app.runtime_config.models import RuntimeConfigVersion
from app.runtime_config.schemas import RuntimeConfigSnapshot, RuntimeSecretBundle
from app.runtime_config.service import RUNTIME_CONTRACT_VERSION, RuntimeConfigError, RuntimeConfigService
from app.marketing_capability_pack.runtime import MarketingRunCapability
from app.tenancy.models import SUPPORTED_LICENSE_FEATURES, Tenant, TenantMembership


class GatewayAdminError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_json(value: Any) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _request_fingerprint(payload: Mapping[str, Any]) -> str:
    """Stable SHA-256 over the operation's full logical request identity."""
    return hashlib.sha256(_canonical_json(dict(payload))).hexdigest()


def _secret_fingerprints(secrets: Mapping[str, Any]) -> dict[str, Any]:
    """One-way fingerprints of write-only secret fields for request hashing.

    Secret values never enter the idempotency record or audit trail; two
    requests that differ only in secret material still hash differently.
    """

    def fp(value: Any) -> Any:
        if isinstance(value, str):
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
        if isinstance(value, Mapping):
            return {str(key): fp(item) for key, item in value.items()}
        return value

    return {str(key): fp(value) for key, value in secrets.items()}


T = TypeVar("T", bound=BaseModel)


def _pi_rollout_config_compatible(config: object) -> bool:
    """Validate the active tenant config before allowing a Pi cutover."""
    try:
        if config is None:
            return False
        if getattr(config, "runtime_contract_version", None) != RUNTIME_CONTRACT_VERSION:
            return False
        if getattr(config, "runtime_backend", None) != "pi":
            return False
        if not getattr(config, "secret_refs_json", None):
            return False
        config_payload = dict(getattr(config, "config_json", None) or {})
        # ``profile_artifact_contracts`` is admin/config metadata used by the
        # Run-creation snapshot resolver; it is deliberately not part of the
        # public immutable RuntimeConfigSnapshot schema.  Validate the same
        # public projection that a new Run will receive.
        config_payload.pop("profile_artifact_contracts", None)
        snapshot = RuntimeConfigSnapshot.model_validate(config_payload)
        if snapshot.runtime_backend != "pi":
            return False
        MarketingRunCapability.model_validate(snapshot.capability_pack)
    except Exception:
        return False
    return True


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class GatewayAdminService:
    """All methods are bound to one DB transaction owned by the router."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _audit(
        self,
        admin_id: str,
        *,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> AdminAuditLog:
        safe_detail = redact_for_log({**detail, **({"idempotency_key": idempotency_key} if idempotency_key else {})})
        row = AdminAuditLog(
            id=str(uuid4()),
            admin_user_id=admin_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail_json=safe_detail,
            created_at=_now(),
        )
        self.db.add(row)
        return row

    async def _idempotent(
        self,
        admin: User,
        *,
        action: str,
        idempotency_key: str,
        fingerprint: Mapping[str, Any],
        target_type: str,
        response_model: type[T],
        produce: Callable[[], Awaitable[tuple[T, str]]],
    ) -> T:
        """Durable admin-write idempotency in the router-owned transaction.

        Same key + same request replays the stored safe projection; same key
        + different request is a stable 409.  The business writes, the audit
        row and this record commit or roll back together.
        """
        request_hash = _request_fingerprint(fingerprint)
        statement = select(AdminIdempotencyRecord).where(
            AdminIdempotencyRecord.actor_id == admin.id,
            AdminIdempotencyRecord.action == action,
            AdminIdempotencyRecord.idempotency_key == idempotency_key,
        )
        existing = await self.db.scalar(statement.with_for_update())
        if existing is not None:
            if existing.request_hash != request_hash:
                raise GatewayAdminError("admin_idempotency_conflict")
            return response_model.model_validate(existing.response_json)
        dto, target_id = await produce()
        self.db.add(
            AdminIdempotencyRecord(
                id=str(uuid4()),
                actor_id=admin.id,
                action=action,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                target_type=target_type,
                target_id=target_id,
                response_json=dto.model_dump(mode="json"),
                created_at=_now(),
            )
        )
        try:
            await self.db.flush()
        except IntegrityError as exc:
            # A concurrent retry with the same key won the unique constraint;
            # roll back this attempt's writes and replay the winner's result.
            await self.db.rollback()
            winner = await self.db.scalar(statement)
            if winner is not None and winner.request_hash == request_hash:
                return response_model.model_validate(winner.response_json)
            raise GatewayAdminError("admin_idempotency_conflict") from exc
        return dto

    async def _tenant(self, tenant_id: str, *, for_update: bool = False) -> Tenant:
        statement = select(Tenant).where(Tenant.id == tenant_id)
        if for_update:
            statement = statement.with_for_update()
        tenant = await self.db.scalar(statement)
        if tenant is None:
            raise GatewayAdminError("tenant_not_found")
        return tenant

    async def list_tenants(self, *, limit: int, offset: int) -> tuple[list[AdminTenantItem], int]:
        statement = select(Tenant).order_by(Tenant.created_at.desc(), Tenant.id).limit(limit).offset(offset)
        tenants = list((await self.db.scalars(statement)).all())
        total = int(await self.db.scalar(select(func.count()).select_from(Tenant)) or 0)
        items: list[AdminTenantItem] = []
        for tenant in tenants:
            member_count = int(
                await self.db.scalar(
                    select(func.count()).select_from(TenantMembership).where(TenantMembership.tenant_id == tenant.id)
                )
                or 0
            )
            active_runs = int(
                await self.db.scalar(
                    select(func.count()).select_from(AgentRun).where(
                        AgentRun.tenant_id == tenant.id,
                        AgentRun.status.in_(('queued', 'running', 'reviewing')),
                    )
                )
                or 0
            )
            items.append(self._tenant_item(tenant, member_count, active_runs))
        return items, total

    @staticmethod
    def _tenant_item(tenant: Tenant, member_count: int, active_runs: int) -> AdminTenantItem:
        return AdminTenantItem(
            id=tenant.id,
            slug=tenant.slug,
            name=tenant.name,
            status=tenant.status,
            is_internal=tenant.is_internal,
            runtime_backend=tenant.runtime_backend,
            license_status=tenant.license_status,
            active_license_id=tenant.active_license_id,
            active_runtime_config_id=tenant.active_runtime_config_id,
            member_count=member_count,
            active_run_count=active_runs,
            created_at=tenant.created_at,
            updated_at=tenant.updated_at,
        )

    async def create_tenant(self, admin: User, payload: AdminTenantCreate, *, idempotency_key: str) -> AdminTenantItem:
        async def produce() -> tuple[AdminTenantItem, str]:
            if await self.db.scalar(select(Tenant.id).where(Tenant.slug == payload.slug)) is not None:
                raise GatewayAdminError("tenant_slug_conflict")
            now = _now()
            tenant_id = str(uuid4())
            tenant = Tenant(
                id=tenant_id,
                slug=payload.slug,
                name=payload.name,
                status="active",
                is_internal=payload.is_internal,
                runtime_backend="current",
                license_status="active",
                active_license_id=None,
                active_runtime_config_id=None,
                created_at=now,
                updated_at=now,
            )
            self.db.add(tenant)
            await self.db.flush()
            license_row = TenantLicense(
                id=str(uuid4()), tenant_id=tenant.id, version=1, valid_from=now,
                valid_until=None,
                features_json={feature: True for feature in SUPPORTED_LICENSE_FEATURES},
                max_concurrent_runs=4, max_user_concurrent_runs=2,
                created_by=admin.id, created_at=now,
            )
            self.db.add(license_row)
            tenant.active_license_id = license_row.id
            await TenantAccountingService(self.db).ensure_tenant_wallet(tenant.id)
            self._audit(admin.id, action="tenant.create", target_type="tenant", target_id=tenant.id,
                        detail={"after": {"slug": tenant.slug, "name": tenant.name}}, idempotency_key=idempotency_key)
            await self.db.flush()
            return self._tenant_item(tenant, 0, 0), tenant.id

        return await self._idempotent(
            admin,
            action="tenant.create",
            idempotency_key=idempotency_key,
            fingerprint=payload.model_dump(mode="json"),
            target_type="tenant",
            response_model=AdminTenantItem,
            produce=produce,
        )

    async def update_tenant(self, admin: User, tenant_id: str, payload: AdminTenantUpdate, *, idempotency_key: str) -> AdminTenantItem:
        async def produce() -> tuple[AdminTenantItem, str]:
            return await self._produce_update_tenant(admin, tenant_id, payload, idempotency_key)

        return await self._idempotent(
            admin,
            action="tenant.update",
            idempotency_key=idempotency_key,
            fingerprint={"tenant_id": tenant_id, **payload.model_dump(mode="json", exclude_unset=True)},
            target_type="tenant",
            response_model=AdminTenantItem,
            produce=produce,
        )

    async def _produce_update_tenant(self, admin: User, tenant_id: str, payload: AdminTenantUpdate, idempotency_key: str) -> tuple[AdminTenantItem, str]:
        tenant = await self._tenant(tenant_id, for_update=True)
        before = {"name": tenant.name, "status": tenant.status, "runtime_backend": tenant.runtime_backend}
        if payload.runtime_backend == "pi" and tenant.runtime_backend != "pi":
            if tenant.license_status != "active" or tenant.active_license_id is None:
                raise GatewayAdminError("pi_rollout_license_required")
            active_license = await self.db.get(TenantLicense, tenant.active_license_id)
            feature_enabled = active_license if active_license is not None and active_license.features_json.get("kol_selection") is True else None
            config = await self.db.scalar(
                select(RuntimeConfigVersion).where(
                    RuntimeConfigVersion.id == tenant.active_runtime_config_id,
                    RuntimeConfigVersion.scope == "tenant",
                    RuntimeConfigVersion.tenant_id == tenant.id,
                    RuntimeConfigVersion.runtime_backend == "pi",
                    RuntimeConfigVersion.status == "active",
                )
            )
            now = _now()
            gateway_lease = get_settings().pi_gateway_lease_seconds
            healthy_gateway = await self.db.scalar(
                select(PiGatewayInstance.id).where(
                    PiGatewayInstance.status == "active",
                    PiGatewayInstance.mode == "active",
                    PiGatewayInstance.desired_capacity > 0,
                    PiGatewayInstance.last_seen_at.is_not(None),
                    PiGatewayInstance.last_seen_at >= now - timedelta(seconds=gateway_lease * 2),
                ).limit(1)
            )
            if feature_enabled is None or not _pi_rollout_config_compatible(config) or healthy_gateway is None:
                raise GatewayAdminError("pi_rollout_precondition_failed")
        if payload.status == "disabled":
            active = await self.db.scalar(
                select(AgentRun.id).where(
                    AgentRun.tenant_id == tenant.id,
                    AgentRun.status.in_(('queued', 'running', 'reviewing')),
                ).limit(1)
            )
            unknown = await self.db.scalar(
                select(AgentToolCall.id).join(AgentRun, AgentRun.id == AgentToolCall.run_id).where(
                    AgentRun.tenant_id == tenant.id, AgentToolCall.status == "unknown"
                ).limit(1)
            )
            if active is not None or unknown is not None:
                raise GatewayAdminError("tenant_disable_blocked")
        if payload.name is not None:
            tenant.name = payload.name
        if payload.status is not None:
            tenant.status = payload.status
        if payload.runtime_backend is not None:
            tenant.runtime_backend = payload.runtime_backend
        tenant.updated_at = _now()
        self._audit(admin.id, action="tenant.update", target_type="tenant", target_id=tenant.id,
                    detail={"before": before, "after": {"name": tenant.name, "status": tenant.status, "runtime_backend": tenant.runtime_backend}}, idempotency_key=idempotency_key)
        members = int(await self.db.scalar(select(func.count()).select_from(TenantMembership).where(TenantMembership.tenant_id == tenant.id)) or 0)
        runs = int(await self.db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.tenant_id == tenant.id, AgentRun.status.in_(('queued','running','reviewing')))) or 0)
        return self._tenant_item(tenant, members, runs), tenant.id

    async def list_users(self, tenant_id: str, *, limit: int, offset: int) -> tuple[list[AdminTenantUserItem], int]:
        await self._tenant(tenant_id)
        statement = (
            select(User, TenantMembership).join(TenantMembership, TenantMembership.user_id == User.id)
            .where(TenantMembership.tenant_id == tenant_id)
            .order_by(User.created_at, User.id).limit(limit).offset(offset)
        )
        rows = (await self.db.execute(statement)).all()
        total = int(await self.db.scalar(select(func.count()).select_from(TenantMembership).where(TenantMembership.tenant_id == tenant_id)) or 0)
        return [AdminTenantUserItem(id=u.id, nickname=u.nickname, role=m.role, status=m.status, created_at=u.created_at) for u, m in rows], total

    async def create_user(self, admin: User, tenant_id: str, payload: AdminTenantUserCreate, *, idempotency_key: str) -> AdminTenantUserItem:
        async def produce() -> tuple[AdminTenantUserItem, str]:
            return await self._produce_create_user(admin, tenant_id, payload, idempotency_key)

        return await self._idempotent(
            admin,
            action="tenant.user.create",
            idempotency_key=idempotency_key,
            fingerprint={"tenant_id": tenant_id, **payload.model_dump(mode="json")},
            target_type="tenant_user",
            response_model=AdminTenantUserItem,
            produce=produce,
        )

    async def _produce_create_user(self, admin: User, tenant_id: str, payload: AdminTenantUserCreate, idempotency_key: str) -> tuple[AdminTenantUserItem, str]:
        tenant = await self._tenant(tenant_id, for_update=True)
        if tenant.status != "active":
            raise GatewayAdminError("tenant_disabled")
        existing_phone = await self.db.scalar(select(AuthIdentity.user_id).where(AuthIdentity.provider == "sms", AuthIdentity.provider_subject == payload.phone))
        if existing_phone is not None:
            raise GatewayAdminError("user_phone_conflict")
        now = _now()
        user_id = str(uuid4())
        user = User(id=user_id, nickname=payload.nickname, role="user", status="active", created_at=now, updated_at=now)
        self.db.add(user)
        await self.db.flush()
        self.db.add(AuthIdentity(id=str(uuid4()), user_id=user_id, provider="sms", provider_subject=payload.phone, created_at=now, updated_at=now))
        membership = TenantMembership(id=str(uuid4()), tenant_id=tenant.id, user_id=user_id, role=payload.role, status="active", created_at=now, updated_at=now)
        self.db.add(membership)
        await self.db.flush()
        accounting = TenantAccountingService(self.db)
        await accounting.ensure_user_quota(tenant.id, user_id, points_limit=payload.points or 1000)
        await accounting.ensure_tenant_wallet(tenant.id)
        self._audit(admin.id, action="tenant.user.create", target_type="tenant_user", target_id=user_id, detail={"tenant_id": tenant.id, "after": {"nickname": payload.nickname, "role": payload.role}}, idempotency_key=idempotency_key)
        return AdminTenantUserItem(id=user.id, nickname=user.nickname, role=membership.role, status=membership.status, created_at=user.created_at), user.id

    @staticmethod
    def _license_item(row: TenantLicense, active_id: str | None) -> AdminLicenseItem:
        return AdminLicenseItem(id=row.id, tenant_id=row.tenant_id, version=row.version, valid_from=row.valid_from, valid_until=row.valid_until, features={str(k): bool(v) for k, v in row.features_json.items()}, max_concurrent_runs=row.max_concurrent_runs, max_user_concurrent_runs=row.max_user_concurrent_runs, active=row.id == active_id, created_at=row.created_at)

    async def list_licenses(self, tenant_id: str) -> list[AdminLicenseItem]:
        tenant = await self._tenant(tenant_id)
        rows = list((await self.db.scalars(select(TenantLicense).where(TenantLicense.tenant_id == tenant_id).order_by(TenantLicense.version.desc()))).all())
        return [self._license_item(row, tenant.active_license_id) for row in rows]

    async def create_license(self, admin: User, tenant_id: str, payload: AdminLicenseCreate, *, idempotency_key: str) -> AdminLicenseItem:
        async def produce() -> tuple[AdminLicenseItem, str]:
            return await self._produce_create_license(admin, tenant_id, payload, idempotency_key)

        return await self._idempotent(
            admin,
            action="license.append",
            idempotency_key=idempotency_key,
            fingerprint={"tenant_id": tenant_id, **payload.model_dump(mode="json")},
            target_type="tenant_license",
            response_model=AdminLicenseItem,
            produce=produce,
        )

    async def _produce_create_license(self, admin: User, tenant_id: str, payload: AdminLicenseCreate, idempotency_key: str) -> tuple[AdminLicenseItem, str]:
        tenant = await self._tenant(tenant_id, for_update=True)
        if set(payload.features) - SUPPORTED_LICENSE_FEATURES or any(not isinstance(v, bool) for v in payload.features.values()):
            raise GatewayAdminError("license_feature_invalid")
        version = int(await self.db.scalar(select(func.coalesce(func.max(TenantLicense.version), 0)).where(TenantLicense.tenant_id == tenant_id)) or 0) + 1
        now = _now()
        row = TenantLicense(id=str(uuid4()), tenant_id=tenant_id, version=version, valid_from=payload.valid_from or now, valid_until=payload.valid_until, features_json=dict(payload.features), max_concurrent_runs=payload.max_concurrent_runs, max_user_concurrent_runs=payload.max_user_concurrent_runs, created_by=admin.id, created_at=now)
        self.db.add(row)
        await self.db.flush()
        self._audit(admin.id, action="license.append", target_type="tenant_license", target_id=row.id, detail={"tenant_id": tenant_id, "after": {"version": version, "features": payload.features}}, idempotency_key=idempotency_key)
        return self._license_item(row, tenant.active_license_id), row.id

    async def update_license_status(self, admin: User, tenant_id: str, license_id: str, payload: AdminLicenseStatusUpdate, *, idempotency_key: str) -> AdminLicenseItem:
        async def produce() -> tuple[AdminLicenseItem, str]:
            return await self._produce_update_license_status(admin, tenant_id, license_id, payload, idempotency_key)

        return await self._idempotent(
            admin,
            action="license.status",
            idempotency_key=idempotency_key,
            fingerprint={"tenant_id": tenant_id, "license_id": license_id, **payload.model_dump(mode="json")},
            target_type="tenant_license",
            response_model=AdminLicenseItem,
            produce=produce,
        )

    async def _produce_update_license_status(self, admin: User, tenant_id: str, license_id: str, payload: AdminLicenseStatusUpdate, idempotency_key: str) -> tuple[AdminLicenseItem, str]:
        tenant = await self._tenant(tenant_id, for_update=True)
        row = await self.db.scalar(select(TenantLicense).where(TenantLicense.id == license_id, TenantLicense.tenant_id == tenant_id).with_for_update())
        if row is None:
            raise GatewayAdminError("license_not_found")
        if payload.status == "active":
            tenant.active_license_id = row.id
            tenant.license_status = "active"
        elif tenant.active_license_id == row.id:
            tenant.active_license_id = None
            tenant.license_status = "suspended"
        self._audit(admin.id, action="license.status", target_type="tenant_license", target_id=row.id, detail={"tenant_id": tenant_id, "status": payload.status}, idempotency_key=idempotency_key)
        return self._license_item(row, tenant.active_license_id), row.id

    async def list_usage(self, tenant_id: str, *, group_by: str, limit: int, offset: int) -> list[dict[str, Any]]:
        await self._tenant(tenant_id)
        rows = await RuntimeUsageService(self.db).aggregate_usage(tenant_id, group_by=group_by)  # type: ignore[arg-type]
        return [item.model_dump(mode="json") for item in rows[offset : offset + limit]]

    async def adjust_wallet(
        self,
        admin: User,
        tenant_id: str,
        payload: AdminWalletAdjustRequest,
        *,
        idempotency_key: str,
    ) -> AdminWalletAdjustResponse:
        """租户钱包人工调整：admin_adjust 账本 + 审计 + 持久化幂等同一事务。"""

        async def produce() -> tuple[AdminWalletAdjustResponse, str]:
            await self._tenant(tenant_id, for_update=True)
            member = await self.db.scalar(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant_id,
                    TenantMembership.user_id == payload.user_id,
                    TenantMembership.status == "active",
                )
            )
            if member is None:
                raise GatewayAdminError("tenant_membership_not_found")
            try:
                wallet, transaction = await TenantAccountingService(self.db).admin_adjust(
                    tenant_id,
                    payload.user_id,
                    delta=payload.delta,
                    idempotency_key=f"admin-wallet-adjust:{admin.id}:{idempotency_key}",
                    reference_id=f"{admin.id}:{idempotency_key}",
                )
            except ValueError as exc:
                raise GatewayAdminError(str(exc)) from exc
            except TenantWalletInsufficientError as exc:
                raise GatewayAdminError("tenant_wallet_insufficient") from exc
            self._audit(
                admin.id,
                action="tenant.wallet_adjust",
                target_type="tenant",
                target_id=tenant_id,
                detail={
                    "user_id": payload.user_id,
                    "delta": payload.delta,
                    "reason": payload.reason,
                    "balance_after": wallet.balance,
                },
                idempotency_key=idempotency_key,
            )
            return (
                AdminWalletAdjustResponse(
                    tenant_id=tenant_id,
                    balance=wallet.balance,
                    reserved=wallet.reserved,
                    transaction_id=transaction.id,
                ),
                tenant_id,
            )

        return await self._idempotent(
            admin,
            action="tenant.wallet_adjust",
            idempotency_key=idempotency_key,
            fingerprint={"tenant_id": tenant_id, **payload.model_dump(mode="json")},
            target_type="tenant",
            response_model=AdminWalletAdjustResponse,
            produce=produce,
        )

    async def get_wallet(self, tenant_id: str) -> AdminWalletItem:
        """租户钱包只读投影；无钱包行时抛 tenant_wallet_not_found（路由映射 404）。"""
        await self._tenant(tenant_id)
        wallet = await self.db.scalar(
            select(TenantWallet).where(TenantWallet.tenant_id == tenant_id)
        )
        if wallet is None:
            raise GatewayAdminError("tenant_wallet_not_found")
        return AdminWalletItem(
            tenant_id=tenant_id,
            balance=wallet.balance,
            reserved=wallet.reserved,
        )

    async def list_quota(self, tenant_id: str) -> list[AdminQuotaPolicyItem]:
        await self._tenant(tenant_id)
        rows = list(
            (
                await self.db.scalars(
                    select(TenantUserQuotaPolicy)
                    .where(TenantUserQuotaPolicy.tenant_id == tenant_id)
                    .order_by(TenantUserQuotaPolicy.user_id)
                )
            ).all()
        )
        return [
            AdminQuotaPolicyItem(
                user_id=row.user_id,
                period="monthly",
                points_limit=row.points_limit,
                status=row.status,
            )
            for row in rows
        ]

    async def set_quota(
        self,
        admin: User,
        tenant_id: str,
        user_id: str,
        payload: AdminQuotaPolicyUpdate,
        *,
        idempotency_key: str,
    ) -> AdminQuotaPolicyItem:
        """用户周期额度 upsert：同一事务写审计与幂等记录。"""

        async def produce() -> tuple[AdminQuotaPolicyItem, str]:
            await self._tenant(tenant_id, for_update=True)
            member = await self.db.scalar(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant_id,
                    TenantMembership.user_id == user_id,
                    TenantMembership.status == "active",
                )
            )
            if member is None:
                raise GatewayAdminError("tenant_membership_not_found")
            policy = await self.db.scalar(
                select(TenantUserQuotaPolicy)
                .where(
                    TenantUserQuotaPolicy.tenant_id == tenant_id,
                    TenantUserQuotaPolicy.user_id == user_id,
                    TenantUserQuotaPolicy.period == "monthly",
                )
                .with_for_update()
            )
            if policy is None:
                policy = TenantUserQuotaPolicy(
                    id=str(uuid4()),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    period="monthly",
                    points_limit=payload.points_limit,
                    status="active",
                    created_at=_now(),
                    updated_at=_now(),
                )
                self.db.add(policy)
            else:
                policy.points_limit = payload.points_limit
                policy.updated_at = _now()
            await self.db.flush()
            self._audit(
                admin.id,
                action="tenant.quota_set",
                target_type="tenant",
                target_id=tenant_id,
                detail={"user_id": user_id, "points_limit": payload.points_limit},
                idempotency_key=idempotency_key,
            )
            return (
                AdminQuotaPolicyItem(
                    user_id=user_id,
                    period="monthly",
                    points_limit=policy.points_limit,
                    status=policy.status,
                ),
                policy.id,
            )

        return await self._idempotent(
            admin,
            action="tenant.quota_set",
            idempotency_key=idempotency_key,
            fingerprint={"tenant_id": tenant_id, "user_id": user_id, **payload.model_dump(mode="json")},
            target_type="tenant_quota",
            response_model=AdminQuotaPolicyItem,
            produce=produce,
        )

    # ------------------------------------------------------------------ #
    # legacy 账号管理写操作：与 Task 10 模块共用同一套持久化幂等指纹
    # ------------------------------------------------------------------ #

    async def create_legacy_user(
        self, admin: User, payload: AdminUserCreate, *, idempotency_key: str
    ) -> AdminUserItem:
        from app.admin.service import AdminService

        async def produce() -> tuple[AdminUserItem, str]:
            dto = await AdminService(self.db).create_user(admin, payload)
            return dto, dto.id

        return await self._idempotent(
            admin,
            action="user.create",
            idempotency_key=idempotency_key,
            fingerprint=payload.model_dump(mode="json"),
            target_type="user",
            response_model=AdminUserItem,
            produce=produce,
        )

    async def update_legacy_user(
        self, admin: User, user_id: str, payload: AdminUserUpdate, *, idempotency_key: str
    ) -> AdminUserItem:
        from app.admin.service import AdminService

        async def produce() -> tuple[AdminUserItem, str]:
            dto = await AdminService(self.db).update_user(admin, user_id, payload)
            return dto, dto.id

        return await self._idempotent(
            admin,
            action="user.update",
            idempotency_key=idempotency_key,
            fingerprint={"user_id": user_id, **payload.model_dump(mode="json", exclude_unset=True)},
            target_type="user",
            response_model=AdminUserItem,
            produce=produce,
        )

    async def disable_legacy_user(
        self, admin: User, user_id: str, *, idempotency_key: str
    ) -> AdminUserDisableResult:
        from app.admin.service import AdminService

        async def produce() -> tuple[AdminUserDisableResult, str]:
            await AdminService(self.db).disable_user(admin, user_id)
            return AdminUserDisableResult(id=user_id), user_id

        return await self._idempotent(
            admin,
            action="user.disable",
            idempotency_key=idempotency_key,
            fingerprint={"user_id": user_id},
            target_type="user",
            response_model=AdminUserDisableResult,
            produce=produce,
        )

    @staticmethod
    def _gateway_item(row: PiGatewayInstance) -> AdminGatewayItem:
        return AdminGatewayItem(id=row.id, gateway_id=row.gateway_id, status=row.status, mode=row.mode, desired_capacity=row.desired_capacity, last_seen_at=row.last_seen_at, updated_at=row.updated_at)

    async def list_gateways(self, *, limit: int, offset: int) -> tuple[list[AdminGatewayItem], int]:
        rows = list((await self.db.scalars(select(PiGatewayInstance).order_by(PiGatewayInstance.gateway_id).limit(limit).offset(offset))).all())
        total = int(await self.db.scalar(select(func.count()).select_from(PiGatewayInstance)) or 0)
        return [self._gateway_item(row) for row in rows], total

    async def update_gateway(self, admin: User, gateway_id: str, payload: AdminGatewayUpdate, *, idempotency_key: str) -> AdminGatewayItem:
        async def produce() -> tuple[AdminGatewayItem, str]:
            row = await self.db.scalar(select(PiGatewayInstance).where(PiGatewayInstance.gateway_id == gateway_id).with_for_update())
            if row is None:
                raise GatewayAdminError("gateway_not_found")
            if payload.desired_capacity is not None:
                row.desired_capacity = payload.desired_capacity
            if payload.mode is not None:
                row.mode = payload.mode
            row.updated_at = _now()
            self._audit(admin.id, action="gateway.update", target_type="pi_gateway", target_id=row.gateway_id, detail={"after": {"mode": row.mode, "desired_capacity": row.desired_capacity}}, idempotency_key=idempotency_key)
            return self._gateway_item(row), row.gateway_id

        return await self._idempotent(
            admin,
            action="gateway.update",
            idempotency_key=idempotency_key,
            fingerprint={"gateway_id": gateway_id, **payload.model_dump(mode="json", exclude_unset=True)},
            target_type="pi_gateway",
            response_model=AdminGatewayItem,
            produce=produce,
        )

    @staticmethod
    def _config_item(row: RuntimeConfigVersion) -> AdminRuntimeConfigItem:
        return AdminRuntimeConfigItem(id=row.id, scope=row.scope, tenant_id=row.tenant_id, version=row.version, status=row.status, runtime_backend=row.runtime_backend, runtime_contract_version=row.runtime_contract_version, model=dict((row.config_json or {}).get("model") or {}), datatap=dict((row.config_json or {}).get("datatap") or {}), limits=dict((row.config_json or {}).get("limits") or {}), billing=dict((row.config_json or {}).get("billing") or {}), profile_artifact_contracts=dict((row.config_json or {}).get("profile_artifact_contracts") or {}), secret_refs=[{"kind": str(ref.get("kind")), "masked_value": "••••", "fingerprint": "stored"} for ref in (row.secret_refs_json or []) if isinstance(ref, dict)], created_by=row.created_by, created_at=row.created_at, activated_at=row.activated_at)

    async def list_runtime_configs(self, tenant_id: str, *, limit: int, offset: int) -> tuple[list[AdminRuntimeConfigItem], int]:
        await self._tenant(tenant_id)
        rows = list((await self.db.scalars(select(RuntimeConfigVersion).where(RuntimeConfigVersion.scope == "tenant", RuntimeConfigVersion.tenant_id == tenant_id).order_by(RuntimeConfigVersion.version.desc()).limit(limit).offset(offset))).all())
        total = int(await self.db.scalar(select(func.count()).select_from(RuntimeConfigVersion).where(RuntimeConfigVersion.scope == "tenant", RuntimeConfigVersion.tenant_id == tenant_id)) or 0)
        return [self._config_item(row) for row in rows], total

    async def create_runtime_config(self, admin: User, payload: AdminRuntimeConfigCreate, *, idempotency_key: str) -> AdminRuntimeConfigItem:
        async def produce() -> tuple[AdminRuntimeConfigItem, str]:
            await self._tenant(payload.tenant_id, for_update=True)
            secrets = RuntimeSecretBundle.model_validate(payload.secrets) if payload.secrets is not None else None
            try:
                row = await RuntimeConfigService(self.db).create_tenant_version(payload.tenant_id, created_by=admin.id, runtime_backend=payload.runtime_backend, model=payload.model, datatap=payload.datatap, limits=payload.limits, billing=payload.billing, secrets=secrets, profile_artifact_contracts=payload.profile_artifact_contracts, runtime_contract_version=payload.runtime_contract_version)
            except RuntimeConfigError as exc:
                raise GatewayAdminError(str(exc)) from exc
            self._audit(admin.id, action="runtime_config.create", target_type="runtime_config", target_id=row.id, detail={"tenant_id": payload.tenant_id, "after": {"runtime_backend": row.runtime_backend, "version": row.version, "secret_count": len(row.secret_refs_json or [])}}, idempotency_key=idempotency_key)
            return self._config_item(row), row.id

        fingerprint_payload = payload.model_dump(mode="json")
        if payload.secrets is not None:
            # 只用单向指纹区分请求，密文值绝不进入幂等记录或审计。
            fingerprint_payload["secrets"] = _secret_fingerprints(payload.secrets)
        return await self._idempotent(
            admin,
            action="runtime_config.create",
            idempotency_key=idempotency_key,
            fingerprint=fingerprint_payload,
            target_type="runtime_config",
            response_model=AdminRuntimeConfigItem,
            produce=produce,
        )

    async def activate_runtime_config(self, admin: User, config_id: str, *, idempotency_key: str) -> AdminRuntimeConfigItem:
        async def produce() -> tuple[AdminRuntimeConfigItem, str]:
            try:
                row = await RuntimeConfigService(self.db).activate(config_id)
            except RuntimeConfigError as exc:
                raise GatewayAdminError(str(exc)) from exc
            self._audit(admin.id, action="runtime_config.activate", target_type="runtime_config", target_id=row.id, detail={"tenant_id": row.tenant_id, "status": row.status}, idempotency_key=idempotency_key)
            return self._config_item(row), row.id

        return await self._idempotent(
            admin,
            action="runtime_config.activate",
            idempotency_key=idempotency_key,
            fingerprint={"config_id": config_id},
            target_type="runtime_config",
            response_model=AdminRuntimeConfigItem,
            produce=produce,
        )

    async def run_diagnostics(self, admin: User, run_id: str) -> AdminRunDiagnostics:
        del admin
        run = await self.db.get(AgentRun, run_id)
        if run is None:
            raise GatewayAdminError("run_not_found")
        attempts = list((await self.db.scalars(select(AgentRunAttempt).where(AgentRunAttempt.run_id == run.id).order_by(AgentRunAttempt.attempt))).all())
        steps = list((await self.db.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.sequence))).all())
        calls = list((await self.db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id).order_by(AgentToolCall.id))).all())
        events = list((await self.db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence))).all())
        usage = list((await self.db.scalars(select(RuntimeUsageRecord).where(RuntimeUsageRecord.run_id == run.id).order_by(RuntimeUsageRecord.observed_at))).all())
        reconciliation = None
        try:
            reconciliation = (await RuntimeUsageService(self.db).reconcile_run(run.id)).model_dump(mode="json")
        except Exception:
            reconciliation = None
        return AdminRunDiagnostics(
            run={"id": run.id, "tenant_id": run.tenant_id, "session_id": run.session_id, "user_id": run.user_id, "status": run.status, "outcome": run.outcome, "runtime_backend": run.runtime_backend, "error_code": run.error_code, "created_at": run.created_at, "started_at": run.started_at, "completed_at": run.completed_at},
            attempts=[{"id": row.id, "attempt": row.attempt, "outcome": row.outcome, "started_at": row.started_at, "ended_at": row.ended_at} for row in attempts],
            steps=[{"id": row.id, "sequence": row.sequence, "step_type": row.step_type, "status": row.status, "duration_ms": row.duration_ms, "created_at": row.created_at} for row in steps],
            tool_calls=[{"id": row.id, "logical_call_id": row.logical_call_id, "service": row.service, "internal_tool_name": row.internal_tool_name, "status": row.status, "points_reserved": row.points_reserved, "points_settled": row.points_settled, "error_type": row.error_type, "completed_at": row.completed_at} for row in calls],
            events=[{"id": row.id, "sequence": row.sequence, "event_type": row.event_type, "created_at": row.created_at} for row in events],
            usage=[{"id": row.id, "kind": row.kind, "backend": row.backend, "provider": row.provider, "model": row.model, "input_tokens": row.input_tokens, "output_tokens": row.output_tokens, "cost_micros": row.cost_micros, "currency": row.currency, "usage_status": row.usage_status, "cost_status": row.cost_status, "observed_at": row.observed_at} for row in usage],
            reconciliation=reconciliation,
        )
