"""Persistent fair scheduler for Pi Gateway Runs.

The scheduler is deliberately small: queue ownership, session mutual exclusion,
gateway capacity and the durable tenant cursor are all changed in one database
transaction. Secret resolution and response serialization happen above this
boundary and can roll the transaction back without leaving a running Attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentSession
from app.agent_runtime.state import RunStatus
from app.licensing.models import TenantLicense
from app.pi_gateway.models import PiGatewayInstance, PiTenantQueueState
from app.tenancy.models import Tenant, TenantMembership


@dataclass
class QueueTenant:
    """In-memory projection used to make round-robin ordering deterministic."""

    tenant_id: str
    queued_count: int
    oldest_queued_at: datetime
    last_claimed_at: datetime | None


@dataclass(frozen=True)
class PreparedPiClaim:
    run: AgentRun
    attempt: AgentRunAttempt
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class HeartbeatDecision:
    cancel_requested: bool
    lease_expires_at: datetime


class GatewayModeError(ValueError):
    def __init__(self, code: str = "pi_gateway_mode_invalid") -> None:
        self.code = code
        super().__init__(code)


class PiRunScheduler:
    """Fair, durable queue ownership for the ``runtime_backend=pi`` lane."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        lease_seconds: int = 60,
        now_fn: Any = None,
    ) -> None:
        self.db = db
        self.lease_seconds = lease_seconds
        self.now_fn = now_fn or (lambda: datetime.now(UTC).replace(tzinfo=None))

    @staticmethod
    def validate_mode(mode: str) -> Literal["active", "draining"]:
        if mode not in {"active", "draining"}:
            raise GatewayModeError()
        return mode  # type: ignore[return-value]

    @staticmethod
    def claims_allowed(*, mode: str, active_runs: int, capacity: int) -> bool:
        PiRunScheduler.validate_mode(mode)
        return mode == "active" and active_runs < capacity

    @staticmethod
    def choose_fair_tenant(tenants: Mapping[str, QueueTenant]) -> str | None:
        eligible = [item for item in tenants.values() if item.queued_count > 0]
        if not eligible:
            return None
        # A tenant that has never been served gets the first turn. Thereafter
        # the oldest cursor wins; queue time/id break ties deterministically.
        eligible.sort(
            key=lambda item: (
                item.last_claimed_at is not None,
                item.last_claimed_at or datetime.min,
                item.oldest_queued_at,
                item.tenant_id,
            )
        )
        return eligible[0].tenant_id

    async def claim_next(
        self,
        gateway_id: str,
        capacity: int,
        *,
        commit: bool = True,
    ) -> PreparedPiClaim | None:
        if not gateway_id or not isinstance(capacity, int) or not 1 <= capacity <= 128:
            raise GatewayModeError("pi_gateway_capacity_invalid")
        now = self.now_fn()
        instance = await self._lock_gateway(gateway_id, now, desired_capacity=capacity)
        effective_capacity = min(capacity, max(0, instance.desired_capacity))
        if instance.status != "active" or instance.mode != "active":
            if commit:
                await self.db.commit()
            return None
        active_count = await self.db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.runtime_backend == "pi",
                AgentRun.gateway_id == gateway_id,
                AgentRun.status.in_((RunStatus.RUNNING, RunStatus.REVIEWING)),
            )
        )
        if not self.claims_allowed(
            mode=instance.mode,
            active_runs=int(active_count or 0),
            capacity=effective_capacity,
        ):
            if commit:
                await self.db.commit()
            return None

        candidates = await self._candidate_tenants(now)
        if not candidates:
            if commit:
                await self.db.commit()
            return None
        run: AgentRun | None = None
        state: PiTenantQueueState | None = None
        while candidates and run is None:
            tenant_id = self.choose_fair_tenant(candidates)
            if tenant_id is None:
                break
            candidates.pop(tenant_id, None)
            candidate = await self._find_oldest_run(tenant_id)
            if candidate is None:
                continue
            scope = await self._lock_candidate_scope(candidate, now)
            if scope is None:
                state = None
                continue
            session = await self._lock_session(candidate.session_id)
            run = await self._lock_run(candidate.id)
            if (
                run is None
                or run.status != RunStatus.QUEUED
                or run.tenant_id != tenant_id
                or run.user_id != candidate.user_id
                or session is None
                or session.tenant_id != run.tenant_id
                or session.active_run_id not in (None, run.id)
            ):
                run = None
                state = None
                continue
            active_ids = list(
                await self.db.scalars(
                    select(AgentRun.id)
                    .where(
                        AgentRun.session_id == run.session_id,
                        AgentRun.run_kind == "user",
                        AgentRun.visibility == "user",
                        AgentRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.REVIEWING)),
                        AgentRun.id != run.id,
                    )
                    .with_for_update()
                )
            )
            if active_ids:
                raise GatewayModeError("pi_gateway_session_mutex_conflict")
            # The candidate projection is intentionally only a hint. Active
            # counters are re-read after the tenant/license, Session and Run
            # locks are held, so concurrent gateways cannot pass stale quotas.
            state = await self._lock_queue_state(tenant_id)
            if not await self._locked_capacity_allowed(run, scope[1]):
                run = None
                state = None
                continue
        if run is None or state is None:
            if commit:
                await self.db.commit()
            return None
        from .service import hash_lease_token, lease_expiry, new_lease_token

        max_attempt = await self.db.scalar(
            select(func.max(AgentRunAttempt.attempt)).where(AgentRunAttempt.run_id == run.id)
        )
        attempt = AgentRunAttempt(
            id=str(uuid4()),
            run_id=run.id,
            attempt=(max_attempt or 0) + 1,
            started_at=now,
            ended_at=None,
            decision_count=0,
            outcome="running",
        )
        token = new_lease_token()
        expiry = lease_expiry(now, self.lease_seconds)
        run.status = RunStatus.RUNNING
        run.started_at = run.started_at or now
        run.gateway_id = gateway_id
        run.gateway_lease_hash = hash_lease_token(token)
        run.gateway_lease_expires_at = expiry
        run.lease_owner = gateway_id
        run.lease_expires_at = expiry
        session.active_run_id = run.id
        state.last_claimed_at = now
        state.active_runs = max(0, state.active_runs) + 1
        state.version += 1
        self.db.add(attempt)
        await self.db.flush()
        if commit:
            await self.db.commit()
        return PreparedPiClaim(run, attempt, token, expiry)

    async def heartbeat(
        self,
        run_id: str,
        attempt_id: str,
        lease: str,
        *,
        gateway_id: str | None = None,
        commit: bool = True,
    ) -> HeartbeatDecision:
        from .service import verify_lease_token

        now = self.now_fn()
        if gateway_id is not None:
            instance = await self._lock_gateway(gateway_id, now)
            if instance.status != "active":
                raise ValueError("pi_gateway_gateway_unavailable")
        run = await self.db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.id == attempt_id, AgentRunAttempt.run_id == run_id)
            .with_for_update()
        )
        if (
            run is None
            or attempt is None
            or attempt.outcome != "running"
            or run.runtime_backend != "pi"
            or run.status != RunStatus.RUNNING
            or run.gateway_id != (gateway_id or run.gateway_id)
            or not run.gateway_lease_hash
            or run.gateway_lease_expires_at is None
        ):
            raise ValueError("pi_gateway_lease_invalid")
        expected_gateway_id = gateway_id or run.gateway_id
        if expected_gateway_id is None:
            raise ValueError("pi_gateway_lease_invalid")
        verify_lease_token(
            lease,
            run.gateway_lease_hash,
            gateway_id=expected_gateway_id,
            expected_gateway_id=run.gateway_id or "",
            run_id=run_id,
            expected_run_id=run.id,
            attempt_id=attempt_id,
            expected_attempt_id=attempt.id,
            expires_at=run.gateway_lease_expires_at.timestamp(),
            now=now.timestamp(),
        )
        expiry = now + timedelta(seconds=self.lease_seconds)
        run.heartbeat_at = now
        run.gateway_lease_expires_at = expiry
        run.lease_expires_at = expiry
        if commit:
            await self.db.commit()
        return HeartbeatDecision(bool(run.cancel_requested), expiry)

    async def set_gateway_mode(
        self,
        gateway_id: str,
        mode: Literal["active", "draining"],
        *,
        commit: bool = True,
    ) -> None:
        self.validate_mode(mode)
        now = self.now_fn()
        instance = await self._lock_gateway(gateway_id, now)
        instance.mode = mode
        instance.updated_at = now
        if commit:
            await self.db.commit()

    async def set_gateway_capacity(
        self, gateway_id: str, capacity: int, *, commit: bool = True
    ) -> None:
        """Persist an administrative desired-capacity limit for a gateway."""
        if not isinstance(capacity, int) or not 0 <= capacity <= 128:
            raise GatewayModeError("pi_gateway_capacity_invalid")
        now = self.now_fn()
        instance = await self._lock_gateway(gateway_id, now, desired_capacity=capacity)
        instance.desired_capacity = capacity
        if commit:
            await self.db.commit()

    async def release_run(self, run: AgentRun, *, commit: bool = False) -> None:
        """Release the durable per-tenant active count exactly once."""
        if getattr(run, "runtime_backend", None) != "pi" or not getattr(run, "tenant_id", None):
            return
        lease_hash = getattr(run, "gateway_lease_hash", None)
        if lease_hash is None:
            return
        # The lease hash is the durable per-Run release marker.  Clear it with
        # an atomic conditional UPDATE before touching the tenant counter; a
        # duplicate terminal/recovery path therefore observes rowcount=0 and
        # cannot decrement active_runs twice.
        result = await self.db.execute(
            update(AgentRun)
            .where(AgentRun.id == run.id, AgentRun.gateway_lease_hash == lease_hash)
            .values(
                gateway_lease_hash=None,
                gateway_lease_expires_at=None,
                gateway_id=None,
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        if result.rowcount != 1:
            return
        run.gateway_lease_hash = None
        run.gateway_lease_expires_at = None
        run.gateway_id = None
        run.lease_owner = None
        run.lease_expires_at = None
        state = await self._lock_queue_state(run.tenant_id)
        state.active_runs = max(0, state.active_runs - 1)
        state.version += 1
        if commit:
            await self.db.commit()

    async def _lock_gateway(
        self, gateway_id: str, now: datetime, *, desired_capacity: int = 1
    ) -> PiGatewayInstance:
        instance = await self.db.scalar(
            select(PiGatewayInstance)
            .where(PiGatewayInstance.gateway_id == gateway_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if instance is None:
            await self._insert_ignore(
                PiGatewayInstance.__table__,
                {
                    "id": str(uuid4()),
                    "gateway_id": gateway_id,
                    "status": "active",
                    "mode": "active",
                    "desired_capacity": desired_capacity,
                    "last_seen_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            instance = await self.db.scalar(
                select(PiGatewayInstance)
                .where(PiGatewayInstance.gateway_id == gateway_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        if instance is None:
            raise GatewayModeError("pi_gateway_instance_unavailable")
        instance.last_seen_at = now
        instance.updated_at = now
        return instance

    async def _lock_queue_state(self, tenant_id: str) -> PiTenantQueueState:
        state = await self.db.scalar(
            select(PiTenantQueueState)
            .where(PiTenantQueueState.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if state is None:
            await self._insert_ignore(PiTenantQueueState.__table__, {"tenant_id": tenant_id})
            state = await self.db.scalar(
                select(PiTenantQueueState)
                .where(PiTenantQueueState.tenant_id == tenant_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        if state is None:
            raise GatewayModeError("pi_gateway_queue_state_unavailable")
        return state

    async def _find_oldest_run(self, tenant_id: str) -> AgentRun | None:
        return await self.db.scalar(
            select(AgentRun)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.runtime_backend == "pi",
                AgentRun.status == RunStatus.QUEUED,
            )
            .order_by(AgentRun.queued_at, AgentRun.id)
            .execution_options(populate_existing=True)
            .limit(1)
        )

    async def _lock_session(self, session_id: str) -> AgentSession | None:
        return await self.db.scalar(
            select(AgentSession)
            .where(AgentSession.id == session_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    async def _lock_run(self, run_id: str) -> AgentRun | None:
        return await self.db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    async def _lock_candidate_scope(
        self, run: AgentRun, now: datetime
    ) -> tuple[Tenant, TenantLicense] | None:
        """Lock and validate Tenant/License before Session→Run locks."""
        row = await self.db.execute(
            select(Tenant, TenantLicense, TenantMembership)
            .join(TenantLicense, TenantLicense.id == Tenant.active_license_id)
            .join(
                TenantMembership,
                (TenantMembership.tenant_id == Tenant.id)
                & (TenantMembership.user_id == run.user_id),
            )
            .where(Tenant.id == run.tenant_id)
            .with_for_update()
        )
        record = row.first()
        if record is None:
            return None
        tenant, license_row, membership = record
        feature = "kol_detail" if run.profile_name.startswith("kol_detail") else "kol_selection"
        if (
            tenant.status != "active"
            or tenant.license_status != "active"
            or membership.status != "active"
            or license_row.features_json.get(feature) is not True
        ):
            return None
        if license_row.valid_from > now or (
            license_row.valid_until is not None and license_row.valid_until <= now
        ):
            return None
        return tenant, license_row

    async def _locked_capacity_allowed(
        self, run: AgentRun, license_row: TenantLicense
    ) -> bool:
        """Revalidate tenant/license/user limits after queue-state locking."""
        active_tenant = await self.db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.tenant_id == run.tenant_id,
                AgentRun.runtime_backend == "pi",
                AgentRun.status.in_((RunStatus.RUNNING, RunStatus.REVIEWING)),
            )
        )
        active_user = await self.db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.tenant_id == run.tenant_id,
                AgentRun.user_id == run.user_id,
                AgentRun.runtime_backend == "pi",
                AgentRun.status.in_((RunStatus.RUNNING, RunStatus.REVIEWING)),
            )
        )
        return (
            int(active_tenant or 0) < license_row.max_concurrent_runs
            and int(active_user or 0) < license_row.max_user_concurrent_runs
        )

    async def _candidate_tenants(self, now: datetime) -> dict[str, QueueTenant]:
        rows = await self.db.execute(
            select(AgentRun, TenantLicense)
            .join(Tenant, Tenant.id == AgentRun.tenant_id)
            .join(TenantLicense, TenantLicense.id == Tenant.active_license_id)
            .join(
                TenantMembership,
                (TenantMembership.tenant_id == Tenant.id)
                & (TenantMembership.user_id == AgentRun.user_id),
            )
            .where(
                AgentRun.runtime_backend == "pi",
                AgentRun.status == RunStatus.QUEUED,
                Tenant.status == "active",
                Tenant.license_status == "active",
                TenantMembership.status == "active",
            )
        )
        runs_by_tenant: dict[str, list[AgentRun]] = {}
        limits: dict[str, TenantLicense] = {}
        for run, license_row in rows:
            feature = "kol_detail" if run.profile_name.startswith("kol_detail") else "kol_selection"
            if license_row.features_json.get(feature) is not True:
                continue
            if license_row.valid_from > now or (
                license_row.valid_until is not None and license_row.valid_until <= now
            ):
                continue
            runs_by_tenant.setdefault(run.tenant_id, []).append(run)
            limits[run.tenant_id] = license_row
        if not runs_by_tenant:
            return {}
        active_rows = await self.db.execute(
            select(AgentRun.tenant_id, AgentRun.user_id, func.count())
            .where(
                AgentRun.runtime_backend == "pi",
                AgentRun.status.in_((RunStatus.RUNNING, RunStatus.REVIEWING)),
            )
            .group_by(AgentRun.tenant_id, AgentRun.user_id)
        )
        active_tenant: dict[str, int] = {}
        active_user: dict[tuple[str, str], int] = {}
        for tenant_id, user_id, count in active_rows:
            active_tenant[tenant_id] = active_tenant.get(tenant_id, 0) + int(count)
            active_user[(tenant_id, user_id)] = int(count)
        session_rows = await self.db.scalars(
            select(AgentSession).where(
                AgentSession.id.in_({run.session_id for runs in runs_by_tenant.values() for run in runs})
            )
        )
        session_slots = {row.id: row.active_run_id for row in session_rows}
        states = {
            row.tenant_id: row
            for row in await self.db.scalars(
                select(PiTenantQueueState).where(PiTenantQueueState.tenant_id.in_(runs_by_tenant))
            )
        }
        result: dict[str, QueueTenant] = {}
        for tenant_id, runs in runs_by_tenant.items():
            available = [
                run
                for run in runs
                if session_slots.get(run.session_id) in (None, run.id)
                and active_tenant.get(tenant_id, 0) < limits[tenant_id].max_concurrent_runs
                and active_user.get((tenant_id, run.user_id), 0)
                < limits[tenant_id].max_user_concurrent_runs
            ]
            if not available:
                continue
            state = states.get(tenant_id)
            result[tenant_id] = QueueTenant(
                tenant_id=tenant_id,
                queued_count=len(available),
                oldest_queued_at=min(run.queued_at for run in available),
                last_claimed_at=state.last_claimed_at if state else None,
            )
        return result

    async def _insert_ignore(self, table: Any, values: dict[str, Any]) -> None:
        bind = self.db.get_bind()
        dialect = bind.dialect.name if bind is not None else "mysql"
        statement = insert(table).values(**values)
        if dialect == "mysql":
            statement = statement.prefix_with("IGNORE")
        elif dialect == "sqlite":
            statement = statement.prefix_with("OR IGNORE")
        try:
            await self.db.execute(statement)
            await self.db.flush()
        except IntegrityError:
            # A concurrent creator won the insert. The following locking SELECT
            # reads the winner's current row; do not leak an integrity error.
            await self.db.rollback()


__all__ = [
    "GatewayModeError",
    "HeartbeatDecision",
    "PiRunScheduler",
    "PreparedPiClaim",
    "QueueTenant",
]
