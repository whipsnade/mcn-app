"""Claim, lease and terminal helpers for the authenticated Gateway."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentMessage, AgentRun, AgentRunAttempt
from app.agent_runtime.state import RunStatus
from app.mcp_gateway.models import McpToolCatalog
from app.runtime_config.schemas import RuntimeSecretBundle
from app.runtime_config.service import RuntimeConfigService

from .auth import seal_secret_bundle
from .contracts import (
    PiGatewayAdapterCatalogEntry,
    PiGatewayClaimRequest,
    PiGatewayClaimResponse,
)
from .scheduler import PiRunScheduler


class PiGatewayLeaseError(ValueError):
    """Stable lease failure without exposing identifiers or token material."""

    def __init__(self, code: str = "pi_gateway_lease_invalid") -> None:
        self.code = code
        super().__init__(code)


def hash_lease_token(token: str) -> str:
    if not isinstance(token, str) or len(token) < 16:
        raise PiGatewayLeaseError()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_lease_token(
    token: str,
    expected_hash: str,
    *,
    gateway_id: str,
    expected_gateway_id: str,
    run_id: str,
    expected_run_id: str,
    attempt_id: str,
    expected_attempt_id: str,
    expires_at: int | float,
    now: int | float,
) -> bool:
    if (
        gateway_id != expected_gateway_id
        or run_id != expected_run_id
        or attempt_id != expected_attempt_id
        or expires_at <= now
    ):
        raise PiGatewayLeaseError()
    try:
        actual = hash_lease_token(token)
    except PiGatewayLeaseError:
        raise
    if not hmac.compare_digest(actual, expected_hash):
        raise PiGatewayLeaseError()
    return True


def new_lease_token() -> str:
    return secrets.token_urlsafe(32)


def lease_expiry(now: datetime | None = None, seconds: int = 60) -> datetime:
    current = now or datetime.now(UTC).replace(tzinfo=None)
    return current + timedelta(seconds=seconds)


class PiGatewayClaimError(ValueError):
    """Stable claim failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PiGatewayService:
    """The B2B response boundary layered over the persistent Pi scheduler."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        gateway_id: str,
        lease_seconds: int = 60,
        now_fn: Callable[[], datetime] | None = None,
        secret_bundle_resolver: Callable[[AgentRun], Awaitable[RuntimeSecretBundle]] | None = None,
    ) -> None:
        self.db = db
        self.gateway_id = gateway_id
        self.lease_seconds = lease_seconds
        self.now_fn = now_fn or (lambda: datetime.now(UTC).replace(tzinfo=None))
        self._secret_bundle_resolver = secret_bundle_resolver
        self.scheduler = PiRunScheduler(
            db,
            lease_seconds=lease_seconds,
            now_fn=self.now_fn,
        )

    async def claim_next(self, request: PiGatewayClaimRequest) -> PiGatewayClaimResponse | None:
        prepared = await self.scheduler.claim_next(
            self.gateway_id,
            request.capacity,
            commit=False,
        )
        if prepared is None:
            await self.db.commit()
            return None
        run = prepared.run
        attempt = prepared.attempt
        token = prepared.lease_token
        if self._secret_bundle_resolver is None:
            try:
                secret_bundle = await RuntimeConfigService(self.db).resolve_secret_bundle(
                    run.runtime_config_version_id, run.id
                )
            except Exception as exc:
                await self.db.rollback()
                raise PiGatewayClaimError("pi_gateway_secret_unavailable") from exc
        else:
            try:
                secret_bundle = await self._secret_bundle_resolver(run)
                secret_bundle = RuntimeSecretBundle.model_validate(secret_bundle)
            except Exception as exc:
                await self.db.rollback()
                raise PiGatewayClaimError("pi_gateway_secret_unavailable") from exc
        try:
            snapshot_model = await RuntimeConfigService(self.db).snapshot_for_existing_run(run)
        except Exception as exc:
            await self.db.rollback()
            raise PiGatewayClaimError("pi_gateway_snapshot_invalid") from exc
        snapshot = snapshot_model.model_dump(mode="json")
        try:
            await self.db.flush()
            bundle = _secret_bundle_dict(secret_bundle)
            envelope = seal_secret_bundle(
                bundle,
                lease_token=token,
                run_id=run.id,
                attempt_id=attempt.id,
                config_version_id=run.runtime_config_version_id,
                gateway_id=self.gateway_id,
            )
            rows = await self.db.scalars(
                select(McpToolCatalog)
                .where(McpToolCatalog.review_status == "approved", McpToolCatalog.is_enabled.is_(True))
                .order_by(McpToolCatalog.internal_tool_name)
                .limit(32)
            )
            adapter_catalog = [
                PiGatewayAdapterCatalogEntry(
                    catalog_entry_id=row.id,
                    adapter_visible_name=row.internal_tool_name,
                    service=row.service_slug,
                    remote_name=row.internal_tool_name,
                    input_schema_digest=(
                        row.discovery_digest
                        if row.discovery_digest.startswith("sha256:")
                        else f"sha256:{row.discovery_digest}"
                    ),
                )
                for row in rows
            ]
            messages = await self.db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == run.session_id)
                .order_by(AgentMessage.sequence)
                .limit(100)
            )
            response = PiGatewayClaimResponse(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_token=token,
                runtime_snapshot=snapshot,
                transcript=[{"role": item.role, "content": item.content} for item in messages],
                secret_envelope=envelope,
                adapter_catalog=adapter_catalog,
                internal_tools=[{"name": name} for name in (
                    "get_session_context",
                    "load_marketing_skill",
                    "search_evidence",
                    "read_tool_result",
                    "read_artifact",
                    "build_brand_report_draft",
                    "build_campaign_report_draft",
                    "build_kol_selection_draft",
                    "build_kol_analysis_draft",
                    "build_kol_detail_draft",
                    "build_insight_draft",
                    "publish_artifacts",
                    "request_clarification",
                )],
            )
        except Exception as exc:
            await self.db.rollback()
            raise PiGatewayClaimError("pi_gateway_response_invalid") from exc
        await self.db.commit()
        return response

    async def leased_run(self, run_id: str, attempt_id: str, lease_token: str) -> AgentRun:
        """Lock and validate the exact Run/Attempt lease using current DB state."""
        run = await self.db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            run is None
            or run.runtime_backend != "pi"
            or run.status != RunStatus.RUNNING
            or run.gateway_id != self.gateway_id
            or not run.gateway_lease_hash
            or run.gateway_lease_expires_at is None
        ):
            raise PiGatewayLeaseError()
        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(
                AgentRunAttempt.id == attempt_id,
                AgentRunAttempt.run_id == run_id,
            )
            .with_for_update()
        )
        if attempt is None or attempt.outcome != "running":
            raise PiGatewayLeaseError()
        verify_lease_token(
            lease_token,
            run.gateway_lease_hash,
            gateway_id=self.gateway_id,
            expected_gateway_id=run.gateway_id or "",
            run_id=run_id,
            expected_run_id=run.id,
            attempt_id=attempt_id,
            expected_attempt_id=attempt.id,
            expires_at=run.gateway_lease_expires_at.timestamp(),
            now=self.now_fn().timestamp(),
        )
        return run


def _secret_bundle_dict(bundle: RuntimeSecretBundle) -> dict[str, Any]:
    return {
        "model_base_url": bundle.model_base_url.get_secret_value(),
        "model_api_key": bundle.model_api_key.get_secret_value(),
        "datatap_token": bundle.datatap_token.get_secret_value(),
        "datatap_urls": {
            key: value.get_secret_value() for key, value in bundle.datatap_urls.items()
        },
    }
