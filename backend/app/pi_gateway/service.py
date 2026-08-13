"""Claim, lease and terminal helpers for the authenticated Gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import (
    AgentMessage,
    AgentEvent,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
)
from app.billing.models import RuntimeUsageRecord, TenantWalletTransaction
from app.agent_runtime.state import RunStatus
from app.agent_runtime.evidence import EvidencePersistenceError, EvidenceWriter
from app.agent_runtime.normalization import NormalizationRegistry
from app.agent_runtime.tools.contracts import arguments_hash, logical_call_id_for
from app.agent_runtime.tools.mcp import _extract_scope_period
from app.agent_runtime.tools.factory import resolve_allowlist_entry
from app.agent_runtime.tools.mcp import DEFINITELY_NOT_SENT, RESULT_UNKNOWN, AgentMcpAccounting
from app.pi_gateway.accounting import (
    McpPreflightContext,
    RuntimeUsageError,
    RuntimeUsageService,
    TenantAccountingError,
    TenantAccountingService,
)
from app.pi_gateway.result import validate_reviewed_result_json
from app.mcp_gateway.models import McpToolCatalog
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.registry import close_input_schema
from app.mcp_gateway.validation import McpValidationError, validate_input, validate_output
from app.runtime_config.schemas import RuntimeSecretBundle
from app.runtime_config.service import RuntimeConfigService

from .auth import seal_secret_bundle
from .contracts import (
    PiGatewayAdapterCatalogEntry,
    PiGatewayClaimRequest,
    PiGatewayClaimResponse,
    PiGatewayMcpFinalizeRequest,
    PiGatewayMcpPermitResponse,
    PiGatewayMcpPreflightRequest,
)
from .events import (
    PiGatewayEventError,
    canonical_event_type,
    normalize_source_payload,
    normalize_usage_payload,
    parse_source_event_id,
)
from .scheduler import PiRunScheduler
from .result import (
    McpResultEnvelopeError,
    parse_mcp_result_details,
)
from .completion import CompletionValidator, close_open_runtime_rows


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


def _adapter_catalog_entry(
    row: McpToolCatalog,
    discovered_remote_name: str | None = None,
) -> PiGatewayAdapterCatalogEntry:
    """Build the server-owned catalog entry shape for snapshot creation/tests."""
    remote_name = discovered_remote_name or row.internal_tool_name
    digest = row.discovery_digest
    return PiGatewayAdapterCatalogEntry(
        catalog_entry_id=row.id,
        adapter_visible_name=row.internal_tool_name,
        service=row.service_slug,
        remote_name=remote_name,
        input_schema_digest=digest if digest.startswith("sha256:") else f"sha256:{digest}",
    )


def lease_deadline_epoch(value: datetime) -> float:
    """naive UTC datetime → epoch 秒。

    仓库约定按 naive UTC 存储（``datetime.now(UTC).replace(tzinfo=None)``）；
    直接 ``.timestamp()`` 会按本地时区解释，UTC+8 下向外暴露的 lease
    deadline 会偏早 8 小时（Gateway 侧立即判 lease 过期）。对外暴露一律
    走这里；lease token 内部校验两侧同口径，不受影响。
    """
    return value.replace(tzinfo=UTC).timestamp()


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
            try:
                adapter_catalog = [
                    PiGatewayAdapterCatalogEntry.model_validate(entry)
                    for entry in snapshot.get("adapter_catalog", [])
                ]
            except Exception as exc:
                await self.db.rollback()
                raise PiGatewayClaimError("pi_gateway_snapshot_catalog_invalid") from exc
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
                lease_expires_at=lease_deadline_epoch(prepared.lease_expires_at),
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

    async def record_model_usage(
        self,
        run: AgentRun,
        attempt_id: str,
        source_event_id: str,
        payload: dict[str, Any],
    ):
        """Persist a server-normalized model usage event without SSE emission."""

        normalized = normalize_usage_payload(payload)
        return await RuntimeUsageService(self.db).record_model_usage(
            run, attempt_id, source_event_id, normalized
        )

    async def ingest_source_event(
        self,
        run: AgentRun,
        *,
        attempt_id: str,
        source_event_id: str,
        sequence: int,
        event_type: str,
        payload: dict[str, Any],
        broker: AgentEventBroker | None = None,
    ) -> dict[str, object]:
        """Persist one safe Gateway event with attempt/sequence idempotency.

        The Run row is already locked by :meth:`leased_run`.  This method is
        intentionally the only source-event write boundary: a duplicate source
        id returns its original receipt, while a future sequence is rejected
        before any user-visible event or message is written.
        """
        parsed_attempt, parsed_sequence = parse_source_event_id(source_event_id)
        if parsed_attempt != attempt_id or parsed_sequence != sequence:
            raise PiGatewayEventError("pi_gateway_source_event_attempt_mismatch")
        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.id == attempt_id, AgentRunAttempt.run_id == run.id)
            .with_for_update()
        )
        if attempt is None or attempt.outcome != "running":
            raise PiGatewayEventError("pi_gateway_source_event_attempt_invalid")

        existing = await self.db.scalar(
            select(AgentEvent)
            .where(AgentEvent.run_id == run.id, AgentEvent.source_event_id == source_event_id)
        )
        if existing is not None:
            return {"event_id": existing.id, "sequence": existing.sequence, "duplicate": True}
        usage_existing = await self.db.scalar(
            select(RuntimeUsageRecord).where(
                RuntimeUsageRecord.run_id == run.id,
                RuntimeUsageRecord.source_event_id == source_event_id,
            )
        )
        if usage_existing is not None:
            return {
                "usage_record_id": usage_existing.id,
                "sequence": sequence,
                "duplicate": True,
            }

        source_sequences: list[int] = []
        for value in (
            await self.db.scalars(
                select(AgentEvent.source_event_id).where(
                    AgentEvent.run_id == run.id,
                    AgentEvent.source_event_id.is_not(None),
                )
            )
        ).all():
            try:
                prior_attempt, prior_sequence = parse_source_event_id(value or "")
            except PiGatewayEventError:
                continue
            if prior_attempt == attempt_id:
                source_sequences.append(prior_sequence)
        for value in (
            await self.db.scalars(
                select(RuntimeUsageRecord.source_event_id).where(
                    RuntimeUsageRecord.run_id == run.id,
                    RuntimeUsageRecord.source_event_id.is_not(None),
                )
            )
        ).all():
            try:
                prior_attempt, prior_sequence = parse_source_event_id(value or "")
            except PiGatewayEventError:
                continue
            if prior_attempt == attempt_id:
                source_sequences.append(prior_sequence)
        expected = max(source_sequences, default=0) + 1
        if sequence != expected:
            raise PiGatewayEventError(
                "pi_gateway_source_sequence_gap" if sequence > expected
                else "pi_gateway_source_sequence_replayed"
            )

        if event_type == "usage":
            record = await self.record_model_usage(run, attempt_id, source_event_id, payload)
            return {"usage_record_id": record.id, "sequence": sequence, "duplicate": False}

        canonical = canonical_event_type(event_type, payload)
        safe_payload = normalize_source_payload(event_type, payload)
        stream = AgentEventStream(self.db, broker or AgentEventBroker())
        event = await stream.append_locked(
            run,
            canonical,
            {**safe_payload, "source_event_id": source_event_id},
        )
        event.source_event_id = source_event_id
        if canonical == "message.completed":
            await self._append_gateway_assistant_message(run, safe_payload)
        await self.db.flush()
        return {"event_id": event.id, "sequence": event.sequence, "duplicate": False}

    async def _append_gateway_assistant_message(
        self, run: AgentRun, payload: dict[str, Any]
    ) -> None:
        existing = list(
            (
                await self.db.scalars(
                    select(AgentMessage).where(
                        AgentMessage.run_id == run.id,
                        AgentMessage.role == "assistant",
                    )
                )
            ).all()
        )
        if existing:
            raise PiGatewayEventError("pi_gateway_message_completion_duplicate")
        delta_rows = list(
            (
                await self.db.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.run_id == run.id,
                        AgentEvent.event_type == "message.delta",
                    )
                    .order_by(AgentEvent.sequence)
                )
            ).all()
        )
        chunks: list[str] = []
        for row in delta_rows:
            item = row.payload_json or {}
            value = item.get("text", item.get("delta"))
            if isinstance(value, str):
                chunks.append(value)
        merged = "".join(chunks)
        final_text = payload.get("text")
        if isinstance(final_text, str) and final_text:
            if not merged or final_text.startswith(merged):
                merged = final_text
            elif final_text not in merged:
                merged += final_text
        if len(merged) > 64 * 1024:
            raise PiGatewayEventError("pi_gateway_message_too_large")
        max_sequence = await self.db.scalar(
            select(func.max(AgentMessage.sequence)).where(AgentMessage.session_id == run.session_id)
        )
        self.db.add(
            AgentMessage(
                id=str(uuid4()),
                session_id=run.session_id,
                run_id=run.id,
                role="assistant",
                content=merged,
                metadata_json={"gateway_message": True},
                sequence=(max_sequence or 0) + 1,
                created_at=self.now_fn(),
            )
        )

    async def has_assistant_completion(self, run: AgentRun) -> bool:
        """兼容旧调用方，但成功判定统一委托给 CompletionValidator。"""
        return bool(await CompletionValidator(self.db).validate(run))

    async def preflight_mcp(
        self,
        run: AgentRun,
        request: PiGatewayMcpPreflightRequest,
    ) -> PiGatewayMcpPermitResponse:
        """Validate the claimed catalog and commit a durable MCP reservation."""
        if run.runtime_backend != "pi" or not run.tenant_id:
            raise TenantAccountingError("pi_mcp_run_invalid")
        catalog = await self.db.scalar(
            select(McpToolCatalog).where(
                McpToolCatalog.internal_tool_name == request.tool_name,
                McpToolCatalog.service_slug == request.server,
                McpToolCatalog.review_status == "approved",
                McpToolCatalog.is_enabled.is_(True),
            )
        )
        if catalog is None:
            raise TenantAccountingError("mcp_tool_not_allowed")
        try:
            validate_input(request.args, close_input_schema(catalog.input_schema_json))
        except (McpValidationError, TypeError, ValueError) as exc:
            raise TenantAccountingError("mcp_arguments_invalid") from exc
        snapshot_catalog = (run.runtime_config_snapshot_json or {}).get("adapter_catalog")
        if not isinstance(snapshot_catalog, list):
            raise TenantAccountingError("mcp_catalog_snapshot_invalid")
        digest = catalog.discovery_digest
        expected_digest = digest if digest.startswith("sha256:") else f"sha256:{digest}"
        snapshot_entry = next(
            (
                entry
                for entry in snapshot_catalog
                if isinstance(entry, dict)
                and entry.get("catalog_entry_id") == catalog.id
                and entry.get("adapter_visible_name") == catalog.internal_tool_name
                and entry.get("service") == catalog.service_slug
            ),
            None,
        )
        if snapshot_entry is None or snapshot_entry.get("input_schema_digest") != expected_digest:
            raise TenantAccountingError("mcp_catalog_snapshot_digest_mismatch")
        feature = _feature_for_profile(run.profile_name)
        decision = await self._feature_allowed(run.tenant_id, run.user_id, feature)
        if not decision:
            raise TenantAccountingError("feature_disabled")
        args_hash = arguments_hash(request.args)
        logical_id = logical_call_id_for(run.id, catalog.internal_tool_name, args_hash)
        call = await self.db.scalar(
            select(AgentToolCall)
            .where(AgentToolCall.logical_call_id == logical_id)
            .with_for_update()
        )
        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(
                AgentRunAttempt.run_id == run.id,
                AgentRunAttempt.ended_at.is_(None),
                AgentRunAttempt.outcome == "running",
            )
            .order_by(AgentRunAttempt.attempt.desc())
            .with_for_update()
        )
        if attempt is None:
            raise TenantAccountingError("pi_mcp_attempt_not_found")
        if call is not None:
            if call.run_id != run.id:
                raise TenantAccountingError("mcp_call_run_mismatch")
            # A repeated SDK hook must never turn a committed/unknown call into
            # a second external dispatch.  Recovery owns unknown calls; a
            # caller must wait for that state transition rather than retrying
            # through the preflight boundary.
            if call.status != "planned":
                raise TenantAccountingError("mcp_call_already_started")
        if call is None:
            step = await self.db.scalar(
                select(AgentStep)
                .where(AgentStep.run_id == run.id, AgentStep.attempt_id == attempt.id)
                .order_by(AgentStep.sequence.desc())
                .with_for_update()
            )
            if step is None:
                step = AgentStep(
                    id=str(uuid4()),
                    run_id=run.id,
                    attempt_id=attempt.id,
                    sequence=(
                        await self.db.scalar(
                            select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run.id)
                        )
                        or 0
                    )
                    + 1,
                    step_type="tool_call",
                    status="running",
                    visibility="internal",
                    created_at=self.now_fn(),
                )
                self.db.add(step)
                await self.db.flush()
            call = AgentToolCall(
                id=str(uuid4()),
                run_id=run.id,
                step_id=step.id,
                logical_call_id=logical_id,
                service=catalog.service_slug,
                internal_tool_name=catalog.internal_tool_name,
                arguments_json=request.args,
                arguments_hash=args_hash,
                status="planned",
                points_reserved=0,
                points_settled=0,
            )
            self.db.add(call)
            await self.db.flush()
        context = McpPreflightContext(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.id,
            tool_call_id=call.id,
            internal_tool_name=catalog.internal_tool_name,
            service_slug=catalog.service_slug,
            arguments=request.args,
            feature=feature,
        )
        permit = await TenantAccountingService(self.db).reserve_mcp_call(context)
        call.points_reserved = permit.amount
        call.status = "running"
        call.started_at = call.started_at or self.now_fn()
        await self.db.flush()
        return PiGatewayMcpPermitResponse(
            permit_id=permit.permit_id,
            catalog_entry_id=catalog.id,
        )

    async def finalize_mcp(
        self,
        run: AgentRun,
        request: PiGatewayMcpFinalizeRequest,
    ) -> dict[str, object]:
        permit_row = await self.db.scalar(
            select(TenantWalletTransaction)
            .where(TenantWalletTransaction.id == request.permit_id)
            .with_for_update()
        )
        if (
            permit_row is None
            or permit_row.run_id != run.id
            or permit_row.tenant_id != run.tenant_id
            or permit_row.user_id != run.user_id
        ):
            raise TenantAccountingError("mcp_permit_run_mismatch")
        call = await self.db.scalar(
            select(AgentToolCall)
            .where(AgentToolCall.id == permit_row.tool_call_id)
            .with_for_update()
        )
        if call is None:
            raise TenantAccountingError("mcp_tool_call_not_found")
        if call.status in ("settled", "failed", "unknown"):
            # 幂等回放：终态调用不重复结算/释放，也不重复写 Evidence。
            return {"permit_id": request.permit_id, "status": call.status, "receipt": None}
        details = request.details
        mode = details.get("mode")
        accounting = TenantAccountingService(self.db)
        if mode == "mcpResult":
            try:
                parsed_result = parse_mcp_result_details(details)
            except McpResultEnvelopeError as exc:
                # A malformed finalize request does not prove what the
                # provider did.  Keep the reservation for admin reconciliation
                # instead of accepting a legacy/raw payload or releasing money.
                raise TenantAccountingError(exc.code) from exc
            if parsed_result.upstream_request_id is not None:
                if (
                    call.upstream_request_id is not None
                    and call.upstream_request_id != parsed_result.upstream_request_id
                ):
                    raise TenantAccountingError("mcp_upstream_request_id_conflict")
                call.upstream_request_id = parsed_result.upstream_request_id
            if parsed_result.result_status == "empty":
                # Confirmed success with no payload: settle the fixed fee, but
                # keep Evidence absent and distinguish it from unknown.
                receipt = await accounting.settle_mcp_call(request.permit_id, details)
                status = "succeeded_empty"
            elif parsed_result.result_status == "unavailable":
                # The provider result is confirmed, but the payload cannot be
                # trusted/retrieved.  It is billable and must not become
                # result_unknown.
                receipt = await accounting.settle_mcp_call(request.permit_id, details)
                status = "result_unavailable"
            else:
                validated = self._validate_mcp_output(call, parsed_result.structured_content)
                if validated is None:
                    await accounting.fail_mcp_call(request.permit_id, "failed_confirmed")
                    receipt = None
                    status = "failed_confirmed"
                else:
                    scope, period = _extract_scope_period(call.arguments_json or {})
                    try:
                        async with self.db.begin_nested():
                            await EvidenceWriter(self.db).write(
                                session_id=run.session_id,
                                run_id=run.id,
                                tool_call_id=call.id,
                                source_type="mcp",
                                source_name=call.internal_tool_name,
                                scope_json=scope,
                                period_json=period,
                                raw_payload=validated,
                                normalization=NormalizationRegistry().normalize(
                                    call.internal_tool_name, validated
                                ),
                            )
                    except (EvidencePersistenceError, TypeError, ValueError):
                        # MCP 已确认成功；本地 Evidence 不可持久化时仍结算，
                        # 并显式标记 unavailable，绝不让 Gateway fallback 成 unknown。
                        receipt = await accounting.settle_mcp_call(request.permit_id, details)
                        status = "result_unavailable"
                    else:
                        receipt = await accounting.settle_mcp_call(request.permit_id, details)
                        status = "settled"
        else:
            raise TenantAccountingError("mcp_result_envelope_invalid")
        await self._update_mcp_call_status(request.permit_id, status)
        await self.db.flush()
        return {"permit_id": request.permit_id, "status": status, "receipt": receipt.model_dump(mode="json") if receipt else None}

    @staticmethod
    def _validate_mcp_output(call: AgentToolCall, structured: Any) -> Any | None:
        """按审核 allowlist 的输出 Schema 校验结构化结果；非法返回 None。"""
        try:
            entry = resolve_allowlist_entry(DataTapService(call.service), call.internal_tool_name)
        except ValueError:
            return None
        if entry is None:
            return None
        _remote_name, _description, output_schema = entry
        if not validate_reviewed_result_json(call.service, output_schema, structured):
            return None
        try:
            validated = validate_output(structured, output_schema)
            return validated
        except McpValidationError:
            return None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    async def fail_mcp(self, run: AgentRun, permit_id: str, classification: str) -> None:
        permit = await self.db.scalar(
            select(TenantWalletTransaction)
            .where(TenantWalletTransaction.id == permit_id)
            .with_for_update()
        )
        if (
            permit is None
            or permit.run_id != run.id
            or permit.tenant_id != run.tenant_id
            or permit.user_id != run.user_id
        ):
            raise TenantAccountingError("mcp_permit_run_mismatch")
        await TenantAccountingService(self.db).fail_mcp_call(permit_id, classification)
        await self._update_mcp_call_status(permit_id, classification)

    async def _update_mcp_call_status(self, permit_id: str, status: str) -> None:
        transaction = await self.db.scalar(
            select(TenantWalletTransaction).where(TenantWalletTransaction.id == permit_id)
        )
        if transaction is None or not transaction.tool_call_id:
            return
        call = await self.db.scalar(
            select(AgentToolCall)
            .where(AgentToolCall.id == transaction.tool_call_id)
            .with_for_update()
        )
        if call is None:
            raise TenantAccountingError("mcp_tool_call_not_found")
        if status == "settled":
            call.status = "settled"
            call.points_settled = 10
            call.points_reserved = 0
        elif status == "succeeded_empty":
            # 上游成功但无结构化内容：积分已结算，产物不可用，不留 Evidence。
            call.status = "settled"
            call.error_type = "succeeded_empty"
            call.points_settled = 10
            call.points_reserved = 0
        elif status == "result_unavailable":
            call.status = "settled"
            call.error_type = "result_unavailable"
            call.safe_error_message = "confirmed MCP success without a retrievable payload"
            call.points_settled = 10
            call.points_reserved = 0
        elif status == "result_unknown":
            call.status = "unknown"
            call.error_type = RESULT_UNKNOWN
        else:
            call.status = "failed"
            call.error_type = (
                DEFINITELY_NOT_SENT if status == "definitely_not_sent" else "failed_confirmed"
            )
            call.points_reserved = 0
        call.completed_at = self.now_fn()
        await self._close_tool_step_if_terminal(call, status)

    async def _close_tool_step_if_terminal(self, call: AgentToolCall, terminal_status: str) -> None:
        """在 Pi MCP 调用终态后关闭其 durable AgentStep。

        Pi Gateway 的 MCP 钩子不会经过 ``AgentEngine`` 的普通 tool-step 收尾，
        因而必须在同一个事务里把最后一个调用所属的 step 也推进到终态。多个
        MCP 调用可以共享一个 step；只有该 step 下没有 planned/reserved/running
        调用时才允许关闭，避免终结检查看到半开放的 step。
        """
        step = await self.db.scalar(
            select(AgentStep)
            .where(AgentStep.id == call.step_id)
            .with_for_update()
        )
        if step is None or step.status != "running":
            return
        open_call = await self.db.scalar(
            select(AgentToolCall.id)
            .where(
                AgentToolCall.step_id == step.id,
                AgentToolCall.status.in_(("planned", "reserved", "running")),
            )
            .limit(1)
        )
        if open_call is not None:
            return
        terminal_calls = list(
            (
                await self.db.scalars(
                    select(AgentToolCall).where(AgentToolCall.step_id == step.id)
                )
            ).all()
        )
        step.status = (
            "failed"
            if terminal_status in ("result_unknown", "failed_confirmed", "definitely_not_sent")
            or any(item.status == "unknown" for item in terminal_calls)
            else "completed"
        )
        step.output_json = {
            **(step.output_json or {}),
            "mcp_terminal_status": terminal_status,
            "tool_call_count": len(terminal_calls),
        }
        step.duration_ms = max(
            0,
            int((self.now_fn() - step.created_at).total_seconds() * 1000),
        )

    async def _feature_allowed(self, tenant_id: str, user_id: str, feature: str) -> bool:
        from app.licensing.service import LicenseService

        return await LicenseService(self.db).authorize_feature(tenant_id, user_id, feature)


class PiGatewayRecoveryService:
    """Recover an expired Pi lease without invoking the current executor.

    A lost Gateway is an infrastructure failure, not a model/business result.
    The first loss closes the open Attempt and returns the same Run to the
    durable Pi queue.  A second loss is terminal ``failed`` and is emitted via
    the shared event transaction.  No tool call or model prompt is replayed by
    this class.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        broker: AgentEventBroker | None = None,
        now_fn: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
    ) -> None:
        self.db = db
        self.broker = broker or AgentEventBroker()
        self.now_fn = now_fn or (lambda: datetime.now(UTC).replace(tzinfo=None))
        self.scheduler = PiRunScheduler(db, lease_seconds=lease_seconds, now_fn=self.now_fn)

    async def _has_durable_completion(self, run: AgentRun) -> bool:
        """兼容旧恢复调用方，但不再提供 assistant-only 成功判定。"""
        return bool(await CompletionValidator(self.db).validate(run))

    async def recover_expired_run(self, run_id: str) -> Literal["requeued", "failed", "completed", "ignored"]:
        """Atomically consume one expired Gateway lease.

        ``ignored`` covers non-Pi, active-lease, cancelled, queued or already
        terminal Runs.  The caller can safely scan these results repeatedly.
        """
        session_id = await self.db.scalar(select(AgentRun.session_id).where(AgentRun.id == run_id))
        if session_id is None:
            return "ignored"
        session = await self.db.scalar(
            select(AgentSession)
            .where(AgentSession.id == session_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        run = await self.db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        now = self.now_fn()
        if (
            run is None
            or session is None
            or run.runtime_backend != "pi"
            or run.status not in (RunStatus.RUNNING, RunStatus.REVIEWING)
            or run.cancel_requested
            or (run.gateway_lease_expires_at is not None and run.gateway_lease_expires_at > now)
        ):
            await self.db.commit()
            return "ignored"
        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.run_id == run.id, AgentRunAttempt.ended_at.is_(None))
            .order_by(AgentRunAttempt.attempt.desc())
            .with_for_update()
        )
        completion_validator = CompletionValidator(self.db)
        completion = await completion_validator.validate(run)
        if completion.ok:
            # durable completion 已存在且满足完整业务契约（terminal ACK 丢失）：
            # 幂等收口为 completed，绝不新起 Attempt。

            async def complete_before_commit(locked_run: AgentRun) -> None:
                # force_complete 已处理 lease_owner/session slot/Attempt；
                # 这里补齐 gateway 租约字段与租户队列计数（release_run 的
                # gateway_lease_hash 一次性标记必须被消费）。
                await self.scheduler.release_run(locked_run)
                if session.active_run_id == locked_run.id:
                    session.active_run_id = None
                locked_run.gateway_lease_hash = None
                locked_run.gateway_lease_expires_at = None
                locked_run.gateway_id = None

            completion_outcome = (
                RunStatus.COMPLETED_WITH_WARNINGS
                if completion.warnings
                else RunStatus.COMPLETED
            )
            event = await AgentEventStream(self.db, self.broker).settle_terminal(
                run.id,
                run.user_id,
                completion_outcome,
                {
                    "recovered_after_terminal_ack_lost": True,
                    **({"warnings": list(completion.warnings)} if completion.warnings else {}),
                },
                before_commit=complete_before_commit,
                completion_validator=completion_validator.validate,
            )
            await self._reconcile_after_terminal(run.id)
            return (
                "completed"
                if event is not None
                or run.status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS)
                else "ignored"
            )
        if completion.code != "pi_gateway_terminal_missing_completion":
            # assistant 已落库但 artifact/Step/MCP 契约不满足时是稳定业务失败，
            # 不能把它误判为基础设施丢失而启动新 Attempt。
            async def invalid_completion_before_commit(locked_run: AgentRun) -> None:
                await close_open_runtime_rows(
                    self.db,
                    locked_run.id,
                    self.now_fn(),
                    error_code=completion.code or "pi_gateway_completion_rejected",
                )
                await self.scheduler.release_run(locked_run)
                if session.active_run_id == locked_run.id:
                    session.active_run_id = None

            event = await AgentEventStream(self.db, self.broker).settle_terminal(
                run.id,
                run.user_id,
                RunStatus.FAILED,
                {"error_code": completion.code or "pi_gateway_completion_rejected"},
                before_commit=invalid_completion_before_commit,
            )
            await self._reconcile_after_terminal(run.id)
            return "failed" if event is not None or run.status == RunStatus.FAILED else "ignored"
        if run.infrastructure_retry_count < 1:
            await self._mark_attempt_tool_calls_unknown(attempt, now)
            if attempt is not None:
                attempt.outcome = "failed"
                attempt.ended_at = now
            run.infrastructure_retry_count += 1
            run.status = RunStatus.QUEUED
            run.error_code = None
            await self.scheduler.release_run(run)
            await self.db.commit()
            return "requeued"

        async def before_commit(locked_run: AgentRun) -> None:
            await close_open_runtime_rows(
                self.db,
                locked_run.id,
                self.now_fn(),
                error_code="pi_infrastructure_retry_exhausted",
            )
            await self.scheduler.release_run(locked_run)
            if session.active_run_id == locked_run.id:
                session.active_run_id = None

        # 第二次基础设施失败：开放 ToolCall 置 unknown 与终态收口同一事务；
        # force_fail 会同事务关闭敞开的 Attempt 2 并释放 session slot。
        await self._mark_attempt_tool_calls_unknown(attempt, now)
        event = await AgentEventStream(self.db, self.broker).settle_terminal(
            run.id,
            run.user_id,
            RunStatus.FAILED,
            {"error_code": "pi_infrastructure_retry_exhausted"},
            before_commit=before_commit,
        )
        await self._reconcile_after_terminal(run.id)
        return "failed" if event is not None or run.status == RunStatus.FAILED else "ignored"

    async def cancel_expired_run(self, run_id: str) -> bool:
        """收口已请求取消且 Gateway 已失联的 Pi Run，不启动新 Attempt。"""
        session_id = await self.db.scalar(select(AgentRun.session_id).where(AgentRun.id == run_id))
        if session_id is None:
            return False
        session = await self.db.scalar(
            select(AgentSession)
            .where(AgentSession.id == session_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        run = await self.db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        now = self.now_fn()
        if (
            run is None
            or session is None
            or run.runtime_backend != "pi"
            or not run.cancel_requested
            or run.status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS, RunStatus.FAILED, RunStatus.CANCELLED)
            or (run.gateway_lease_expires_at is not None and run.gateway_lease_expires_at > now)
        ):
            await self.db.commit()
            return False

        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.run_id == run.id, AgentRunAttempt.ended_at.is_(None))
            .order_by(AgentRunAttempt.attempt.desc())
            .with_for_update()
        )

        async def before_commit(locked_run: AgentRun) -> None:
            await self._mark_attempt_tool_calls_unknown(
                attempt,
                now,
                release_reserved=True,
                user_id=locked_run.user_id,
            )
            await close_open_runtime_rows(
                self.db,
                locked_run.id,
                self.now_fn(),
                error_code="cancel_requested",
            )
            await self.scheduler.release_run(locked_run)
            if session.active_run_id == locked_run.id:
                session.active_run_id = None

        event = await AgentEventStream(self.db, self.broker).settle_terminal(
            run.id,
            run.user_id,
            RunStatus.CANCELLED,
            {"code": "cancel_requested"},
            before_commit=before_commit,
        )
        await self._reconcile_after_terminal(run.id)
        return event is not None

    async def _reconcile_after_terminal(self, run_id: str) -> None:
        try:
            await RuntimeUsageService(self.db).reconcile_run(run_id)
        except RuntimeUsageError:
            # Recovery must not reopen or mutate a terminal Run because an
            # audit-only reconciliation read is temporarily unavailable.
            return

    async def _mark_attempt_tool_calls_unknown(
        self,
        attempt: AgentRunAttempt | None,
        now: datetime,
        *,
        release_reserved: bool = False,
        user_id: str | None = None,
    ) -> None:
        """Fence incomplete MCP calls before recovery or cancellation is visible."""
        if attempt is None:
            return
        steps = (
            await self.db.scalars(
                select(AgentStep)
                .where(AgentStep.attempt_id == attempt.id, AgentStep.status == "running")
                .with_for_update()
            )
        ).all()
        for step in steps:
            step.status = "failed"
            step.output_json = {
                **(step.output_json or {}),
                "error_code": "pi_gateway_attempt_lost",
                "terminal_cleanup": True,
            }
        calls = (
            await self.db.scalars(
                select(AgentToolCall)
                .join(AgentStep, AgentStep.id == AgentToolCall.step_id)
                .where(
                    AgentStep.attempt_id == attempt.id,
                    AgentToolCall.status.in_(("planned", "reserved", "running")),
                )
                .with_for_update()
            )
        ).all()
        accounting = AgentMcpAccounting(self.db) if release_reserved else None
        for call in calls:
            if call.status == "planned":
                # planned 仍在 durable-before-send 之前；Recovery 可以确认未
                # 外发，关闭为 definitely_not_sent，后续同 logical_call_id
                # 仍可按既有一次性重试规则重新准备。
                call.status = "failed"
                call.error_type = DEFINITELY_NOT_SENT
                call.points_reserved = 0
                call.completed_at = now
                call.safe_error_message = "gateway lost before external dispatch"
                continue
            if release_reserved and call.status == "reserved":
                if user_id is None or accounting is None:
                    raise ValueError("pi_recovery_user_required_for_reserved_release")
                await accounting.release(
                    user_id,
                    call,
                    error_type=DEFINITELY_NOT_SENT,
                    message="cancelled before external dispatch",
                )
                continue
            call.status = "unknown"
            call.error_type = RESULT_UNKNOWN
            call.safe_error_message = "gateway infrastructure failure; result requires reconciliation"
            call.completed_at = now


def _secret_bundle_dict(bundle: RuntimeSecretBundle) -> dict[str, Any]:
    return {
        "model_base_url": bundle.model_base_url.get_secret_value(),
        "model_api_key": bundle.model_api_key.get_secret_value(),
        "datatap_token": bundle.datatap_token.get_secret_value(),
        "datatap_urls": {
            key: value.get_secret_value() for key, value in bundle.datatap_urls.items()
        },
    }


def _feature_for_profile(profile_name: str) -> str:
    lowered = profile_name.lower()
    if "brand" in lowered:
        return "brand_analysis"
    if "campaign" in lowered:
        return "campaign_analysis"
    if "detail" in lowered:
        return "kol_detail"
    if "utility" in lowered:
        return "utility"
    return "kol_selection"
