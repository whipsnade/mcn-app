from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import AdminAuditLog
from app.admin.schemas import (
    AdminUserCreate,
    AdminUserItem,
    AdminUserUpdate,
    PointsHistoryEntry,
)
from app.agent_runtime.evidence import EvidencePersistenceError, EvidenceWriter
from app.agent_runtime.models import (
    AgentRun,
    AgentToolCall,
    AgentToolCallReconciliation,
    EvidenceItem,
)
from app.agent_runtime.tools.factory import resolve_allowlist_entry
from app.agent_runtime.tools.mcp import AgentMcpAccounting
from app.billing.models import TenantWalletTransaction, Wallet, WalletTransaction
from app.billing.service import WalletService
from app.core.redaction import redact_for_log
from app.identity.models import AuthIdentity, LoginSession, User, UserChannelPermission
from app.tenancy.models import TenantMembership
from app.tenancy.service import TenantService
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.models import McpCall
from app.mcp_gateway.transport import resolve_remote_result_status
from app.mcp_gateway.validation import McpValidationError, validate_output
from app.pi_gateway.accounting import TenantAccountingService
from app.pi_gateway.result import validate_reviewed_result_json
from app.quick.models import QuickMcpCall
from app.tasks.models import AnalysisTask
from app.workspace.models import WorkspaceSession


HISTORY_KINDS = ("settle", "admin_adjust", "welcome_grant")

# 快捷功能 feature → 积分流水展示用中文名。
QUICK_FEATURE_TITLES = {
    "kol_recommend": "达人推荐",
    "kol_detail": "达人详情",
    "top_posts": "爆贴查询",
}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PhoneConflictError(Exception):
    """Raised when a phone number already belongs to another account."""


class AdminService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        tool_call_reconciler: Any | None = None,
    ) -> None:
        self.db = db
        # 管理员核对时取回上游 payload 的注入器（生产由 app.state 接线到
        # transport.reconcile_tool_call；测试注入假 reconciler）。为 None 时
        # confirm_success 视为无 payload 可取。
        self._tool_call_reconciler = tool_call_reconciler

    async def _get_user(self, user_id: str) -> User:
        user = await self.db.get(User, user_id)
        if user is None:
            raise LookupError("user_not_found")
        return user

    async def _phone_of(self, user_id: str) -> str | None:
        return await self.db.scalar(
            select(AuthIdentity.provider_subject).where(
                AuthIdentity.provider == "sms",
                AuthIdentity.user_id == user_id,
            )
        )

    async def _channels_of(self, user_id: str) -> list[str]:
        return list(
            (
                await self.db.scalars(
                    select(UserChannelPermission.channel).where(
                        UserChannelPermission.user_id == user_id,
                        UserChannelPermission.is_enabled.is_(True),
                    )
                )
            ).all()
        )

    async def _to_item(self, user: User) -> AdminUserItem:
        try:
            wallet = await WalletService(self.db).get_wallet(user.id)
        except LookupError:
            # Admin lists may include a legacy/auth fixture user before its
            # first wallet grant; preserve the old zero-valued projection.
            wallet = None
        tenant_id = await self.db.scalar(
            select(TenantMembership.tenant_id).where(
                TenantMembership.user_id == user.id,
                TenantMembership.status == "active",
            )
        )
        if wallet is not None:
            available_points, reserved_points = await WalletService(self.db).quota_view(user.id)
        else:
            available_points, reserved_points = 0, 0
        return AdminUserItem(
            id=user.id,
            tenant_id=tenant_id,
            nickname=user.nickname,
            role=user.role,
            status=user.status,
            phone=await self._phone_of(user.id),
            # Admin user rows show the member's quota intersection, not a
            # duplicated tenant-pool balance.  ``tenant_id`` identifies the
            # shared pool adjusted by the points endpoint.
            points=available_points,
            reserved_points=reserved_points,
            channels=await self._channels_of(user.id),
            industries=[str(item) for item in (user.industries or ["美食"])],
            created_at=user.created_at,
        )

    def _audit(
        self,
        admin_id: str,
        *,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict[str, Any],
    ) -> AdminAuditLog:
        entry = AdminAuditLog(
            id=str(uuid4()),
            admin_user_id=admin_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail_json=redact_for_log(detail),
            created_at=utc_now(),
        )
        self.db.add(entry)
        return entry

    async def list_users(
        self,
        *,
        keyword: str | None,
        channel: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[AdminUserItem], int]:
        statement = select(User)
        if keyword:
            like = f"%{keyword}%"
            phone_matches = select(AuthIdentity.user_id).where(
                AuthIdentity.provider == "sms",
                AuthIdentity.provider_subject.like(like),
            )
            statement = statement.where(
                or_(User.nickname.like(like), User.id.in_(phone_matches))
            )
        if channel:
            channel_matches = select(UserChannelPermission.user_id).where(
                UserChannelPermission.channel == channel,
                UserChannelPermission.is_enabled.is_(True),
            )
            statement = statement.where(User.id.in_(channel_matches))
        total = await self.db.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        users = list(
            (
                await self.db.scalars(
                    statement.order_by(User.created_at.desc(), User.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        return [await self._to_item(user) for user in users], total or 0

    async def create_user(self, admin: User, payload: AdminUserCreate) -> AdminUserItem:
        conflict_id = await self.db.scalar(
            select(AuthIdentity.user_id).where(
                AuthIdentity.provider == "sms",
                AuthIdentity.provider_subject == payload.phone,
            )
        )
        if conflict_id is not None:
            raise PhoneConflictError()
        now = utc_now()
        user = User(
            id=str(uuid4()),
            nickname=payload.nickname,
            role=payload.role,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.db.add(user)
        await self.db.flush()
        tenant_context = await TenantService(self.db).provision_personal_tenant(
            user.id, name=payload.nickname, now=now
        )
        from app.pi_gateway.accounting import TenantAccountingService

        accounting = TenantAccountingService(self.db)
        await accounting.ensure_tenant_wallet(tenant_context.tenant_id)
        await accounting.ensure_user_quota(tenant_context.tenant_id, user.id)
        self.db.add(
            AuthIdentity(
                id=str(uuid4()),
                user_id=user.id,
                provider="sms",
                provider_subject=payload.phone,
                created_at=now,
                updated_at=now,
            )
        )
        for channel in payload.channels:
            self.db.add(
                UserChannelPermission(
                    id=str(uuid4()),
                    user_id=user.id,
                    channel=channel,
                    is_enabled=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        audit = self._audit(
            admin.id,
            action="user.create",
            target_type="user",
            target_id=user.id,
            detail={
                "after": {
                    "nickname": user.nickname,
                    "phone": payload.phone,
                    "role": user.role,
                    "channels": payload.channels,
                    "points": payload.points,
                }
            },
        )
        await self.db.flush()
        if payload.points > 0:
            await WalletService(self.db).admin_adjust(
                user.id,
                delta=payload.points,
                reason="initial grant",
                idempotency_key=f"admin-create:{user.id}",
                reference_id=audit.id,
            )
        await self.db.flush()
        return await self._to_item(user)

    async def update_user(
        self, admin: User, user_id: str, payload: AdminUserUpdate
    ) -> AdminUserItem:
        user = await self._get_user(user_id)
        if user.id == admin.id:
            if payload.role is not None and payload.role != "admin":
                raise ValueError("self_role_change_forbidden")
            if payload.status is not None and payload.status != "active":
                raise ValueError("self_disable_forbidden")
        before = {
            "nickname": user.nickname,
            "phone": await self._phone_of(user.id),
            "role": user.role,
            "status": user.status,
            "channels": await self._channels_of(user.id),
            "industries": [str(item) for item in (user.industries or [])],
        }
        if payload.phone is not None and payload.phone != before["phone"]:
            conflict_id = await self.db.scalar(
                select(AuthIdentity.user_id).where(
                    AuthIdentity.provider == "sms",
                    AuthIdentity.provider_subject == payload.phone,
                    AuthIdentity.user_id != user.id,
                )
            )
            if conflict_id is not None:
                raise PhoneConflictError()
            identity = await self.db.scalar(
                select(AuthIdentity).where(
                    AuthIdentity.provider == "sms",
                    AuthIdentity.user_id == user.id,
                )
            )
            now = utc_now()
            if identity is None:
                self.db.add(
                    AuthIdentity(
                        id=str(uuid4()),
                        user_id=user.id,
                        provider="sms",
                        provider_subject=payload.phone,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                identity.provider_subject = payload.phone
                identity.updated_at = now
        if payload.nickname is not None:
            user.nickname = payload.nickname
        if payload.role is not None:
            user.role = payload.role
        if payload.status is not None:
            user.status = payload.status
        if payload.industries is not None:
            user.industries = list(payload.industries)
        user.updated_at = utc_now()
        if payload.channels is not None:
            existing = list(
                (
                    await self.db.scalars(
                        select(UserChannelPermission).where(
                            UserChannelPermission.user_id == user.id
                        )
                    )
                ).all()
            )
            for row in existing:
                await self.db.delete(row)
            # Flush the deletes before re-adding rows so the (user_id, channel)
            # unique constraint is not violated when a channel is kept.
            await self.db.flush()
            now = utc_now()
            for channel in payload.channels:
                self.db.add(
                    UserChannelPermission(
                        id=str(uuid4()),
                        user_id=user.id,
                        channel=channel,
                        is_enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
        after = {
            "nickname": user.nickname,
            "phone": payload.phone if payload.phone is not None else before["phone"],
            "role": user.role,
            "status": user.status,
            "channels": (
                payload.channels if payload.channels is not None else before["channels"]
            ),
            "industries": (
                list(payload.industries)
                if payload.industries is not None
                else before["industries"]
            ),
        }
        self._audit(
            admin.id,
            action="user.update",
            target_type="user",
            target_id=user.id,
            detail={"before": before, "after": after},
        )
        await self.db.flush()
        return await self._to_item(user)

    async def disable_user(self, admin: User, user_id: str) -> None:
        if user_id == admin.id:
            raise ValueError("self_delete_forbidden")
        user = await self._get_user(user_id)
        now = utc_now()
        user.status = "disabled"
        user.updated_at = now
        sessions = list(
            (
                await self.db.scalars(
                    select(LoginSession).where(
                        LoginSession.user_id == user.id,
                        LoginSession.revoked_at.is_(None),
                    )
                )
            ).all()
        )
        for session in sessions:
            session.revoked_at = now
        self._audit(
            admin.id,
            action="user.disable",
            target_type="user",
            target_id=user.id,
            detail={
                "before": {"status": "active"},
                "after": {"status": "disabled"},
                "revoked_sessions": len(sessions),
            },
        )
        await self.db.flush()

    async def adjust_points(
        self,
        admin: User,
        user_id: str,
        *,
        delta: int,
        reason: str,
        idempotency_key: str | None,
    ) -> tuple[Wallet, WalletTransaction]:
        user = await self._get_user(user_id)
        wallet_service = WalletService(self.db)
        if idempotency_key is not None:
            applied = await self.db.scalar(
                select(TenantWalletTransaction).where(
                    TenantWalletTransaction.idempotency_key == idempotency_key
                )
            )
            if applied is None:
                applied = await self.db.scalar(
                    select(WalletTransaction).where(
                        WalletTransaction.idempotency_key == idempotency_key
                    )
                )
            if applied is not None:
                return await wallet_service.get_wallet(user.id), applied
        audit = self._audit(
            admin.id,
            action="points.adjust",
            target_type="wallet",
            target_id=user.id,
            detail={
                "delta": delta,
                "reason": reason,
                "phone": await self._phone_of(user.id),
            },
        )
        await self.db.flush()
        wallet, transaction = await wallet_service.admin_adjust(
            user.id,
            delta=delta,
            reason=reason,
            idempotency_key=idempotency_key or f"admin-adjust:{uuid4()}",
            reference_id=audit.id,
        )
        audit.detail_json = redact_for_log(
            {
                "delta": delta,
                "reason": reason,
                "phone": await self._phone_of(user.id),
                "balance_after": wallet.balance,
            }
        )
        await self.db.flush()
        return wallet, transaction

    async def points_history(
        self, user_id: str, *, limit: int, offset: int
    ) -> tuple[list[PointsHistoryEntry], int]:
        await self._get_user(user_id)
        tenant_wallet = await WalletService(self.db)._tenant_wallet(user_id)
        ledger_model = TenantWalletTransaction if tenant_wallet is not None else WalletTransaction
        statement = select(ledger_model).where(
            ledger_model.user_id == user_id,
            ledger_model.kind.in_(HISTORY_KINDS),
        )
        total = await self.db.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        transactions = list(
            (
                await self.db.scalars(
                    statement.order_by(
                        ledger_model.created_at.desc(), ledger_model.id
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        settle_ids = [tx.id for tx in transactions if tx.kind == "settle"]
        settle_call_ids = [
            tx.tool_call_id
            for tx in transactions
            if tx.kind == "settle" and getattr(tx, "tool_call_id", None)
        ]
        context: dict[str, tuple[str | None, str | None]] = {}
        if settle_ids or settle_call_ids:
            rows = (
                await self.db.execute(
                    select(
                        McpCall.id,
                        McpCall.settlement_transaction_id,
                        McpCall.service_slug,
                        WorkspaceSession.title,
                        WorkspaceSession.platforms,
                    )
                    .join(AnalysisTask, McpCall.task_id == AnalysisTask.id)
                    .join(WorkspaceSession, AnalysisTask.session_id == WorkspaceSession.id)
                    .where(
                        or_(
                            McpCall.settlement_transaction_id.in_(settle_ids)
                            if settle_ids
                            else False,
                            McpCall.id.in_(settle_call_ids)
                            if settle_call_ids
                            else False,
                        )
                    )
                )
            ).all()
            for call_id, tx_id, service_slug, title, platforms in rows:
                platform = platforms[0] if platforms else service_slug
                if tx_id is not None:
                    context[tx_id] = (title, platform)
                context[call_id] = (title, platform)
        quick_settle_ids = [
            tx.id
            for tx in transactions
            if tx.kind == "settle" and tx.reference_type == "quick_mcp_call"
        ]
        if quick_settle_ids:
            quick_rows = (
                await self.db.execute(
                    select(
                        QuickMcpCall.settlement_transaction_id,
                        QuickMcpCall.feature,
                        QuickMcpCall.arguments_json,
                    ).where(QuickMcpCall.settlement_transaction_id.in_(quick_settle_ids))
                )
            ).all()
            for tx_id, feature, arguments in quick_rows:
                title = QUICK_FEATURE_TITLES.get(feature, feature)
                datasource = (arguments or {}).get("datasource")
                platform = (
                    str(datasource[0])
                    if isinstance(datasource, list) and datasource
                    else None
                )
                context[tx_id] = (title, platform)
        items = [
            PointsHistoryEntry(
                id=tx.id,
                kind=tx.kind,
                points=-tx.reserved_delta if tx.kind == "settle" else tx.balance_delta,
                session_title=context.get(
                    tx.id, context.get(getattr(tx, "tool_call_id", None), (None, None))
                )[0],
                platform=context.get(
                    tx.id, context.get(getattr(tx, "tool_call_id", None), (None, None))
                )[1],
                created_at=tx.created_at,
            )
            for tx in transactions
        ]
        return items, total or 0

    async def reconcile_tool_call(
        self,
        admin: User,
        call_id: str,
        *,
        decision: str,
        note: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """管理员核对 result_unknown 的 Agent 工具调用（§11.1 / §16）。

        - confirm_success：能取回 payload 时创建 Evidence 并结算；取不回则只
          结算并标记 result_unavailable（管理员不能伪造 Evidence）；
        - confirm_failure：释放预留；
        - keep_unknown：保持 reserved/unknown 并追加核对审计。
        终态（settled/failed）重放幂等：按当前状态返回，不再改钱包或重复审计。

        注意：Idempotency-Key 目前仅记录进审计日志，去重依赖终态守卫 + 钱包
        幂等键（agent-mcp:{logical_call_id}:{settle|release}）。完整的
        Idempotency-Key 级去重延后到 Task 24 恢复循环接线 reconciler 时实现。
        """
        call = await self.db.scalar(
            select(AgentToolCall)
            .where(AgentToolCall.id == call_id)
            .with_for_update()
        )
        if call is None:
            raise LookupError("agent_tool_call_not_found")
        run = await self.db.get(AgentRun, call.run_id)
        if run is None:
            raise LookupError("agent_run_not_found")
        user_id = await self._agent_run_user_id(call)

        if call.status in ("settled", "failed"):
            evidence = await self._evidence_by_call(call.id)
            return self._reconcile_payload(
                call, evidence_id=evidence.id if evidence is not None else None
            )

        before_status = call.status
        evidence_id: str | None = None
        if decision == "confirm_success":
            # Pi's model-visible MCP result is never reconstructed by the
            # admin path. A Pi unknown call may only be settled after an
            # authoritative upstream fact is supplied; this route does not
            # accept arbitrary payloads or create Evidence for Pi.
            if run.runtime_backend == "pi":
                if self._tool_call_reconciler is None or not call.upstream_request_id:
                    raise ValueError("pi_mcp_success_confirmation_unavailable")
                result = await self._tool_call_reconciler(call.upstream_request_id)
                if result is None or getattr(result, "is_error", False):
                    raise ValueError("pi_mcp_success_confirmation_unavailable")
                await TenantAccountingService(self.db).settle_mcp_call_metadata(
                    await self._tenant_permit_id(call),
                    {
                        "outcome": "succeeded",
                        "upstream_request_id": call.upstream_request_id,
                    },
                )
                call.status = "settled"
                call.points_reserved = 0
                call.points_settled = 10
                call.error_type = None
                call.safe_error_message = None
                call.completed_at = utc_now()
                evidence_id = None
            else:
                evidence, invalid_payload = await self._retrievable_evidence(call)
                if evidence is not None:
                    evidence_id = evidence.id
                if invalid_payload:
                    # Payload was confirmed to exist but failed the reviewed output
                    # contract: it is a confirmed failure, not an unavailable
                    # success. Release the reservation and never create Evidence.
                    await AgentMcpAccounting(self.db).release(
                        user_id,
                        call,
                        error_type="failed_confirmed",
                        message="reconciled payload failed output schema validation",
                    )
                else:
                    await AgentMcpAccounting(self.db).settle(user_id, call)
                if evidence is None and not invalid_payload:
                    call.error_type = "result_unavailable"
                    call.safe_error_message = "result_unavailable"
        elif decision == "confirm_failure":
            await AgentMcpAccounting(self.db).release(
                user_id,
                call,
                error_type="admin_confirmed_failure",
                message=note or "admin confirmed failure",
            )
        elif decision == "keep_unknown":
            pass  # 保持 reserved/unknown，不触碰钱包
        else:  # pragma: no cover - schema 已约束
            raise ValueError("invalid reconcile decision")

        if decision == "keep_unknown":
            already = await self.db.scalar(
                select(AgentToolCallReconciliation).where(
                    AgentToolCallReconciliation.tool_call_id == call.id,
                    AgentToolCallReconciliation.source == "admin",
                    AgentToolCallReconciliation.decision == "keep_unknown",
                )
            )
            if already is not None:
                await self.db.flush()
                return self._reconcile_payload(call, evidence_id=evidence_id)

        self._append_admin_reconciliation(call, decision, admin.id, note)
        self._audit(
            admin.id,
            action="agent_tool_call.reconcile",
            target_type="agent_tool_call",
            target_id=call.id,
            detail={
                "decision": decision,
                "note": note,
                "before_status": before_status,
                "after_status": call.status,
                "idempotency_key": idempotency_key,
            },
        )
        await self.db.flush()
        return self._reconcile_payload(call, evidence_id=evidence_id)

    async def _agent_run_user_id(self, call: AgentToolCall) -> str:
        run = await self.db.get(AgentRun, call.run_id)
        if run is None:
            raise LookupError("agent_run_not_found")
        return run.user_id

    async def _tenant_permit_id(self, call: AgentToolCall) -> str:
        """Find the still-open tenant reserve for one unknown call."""
        reserves = list(
            (
                await self.db.scalars(
                    select(TenantWalletTransaction)
                    .where(
                        TenantWalletTransaction.tool_call_id == call.id,
                        TenantWalletTransaction.kind == "reserve",
                    )
                    .order_by(TenantWalletTransaction.created_at.desc())
                )
            ).all()
        )
        for reserve in reserves:
            terminal = await self.db.scalar(
                select(TenantWalletTransaction.id).where(
                    TenantWalletTransaction.reference_id == reserve.id,
                    TenantWalletTransaction.kind.in_(("settle", "release")),
                )
            )
            if terminal is None:
                return reserve.id
        raise ValueError("tenant_mcp_permit_not_found")

    async def _evidence_by_call(self, call_id: str) -> EvidenceItem | None:
        return await self.db.scalar(
            select(EvidenceItem).where(EvidenceItem.tool_call_id == call_id)
        )

    async def _retrievable_evidence(
        self, call: AgentToolCall
    ) -> tuple[EvidenceItem | None, bool]:
        """已落库的 Evidence 优先；否则经注入的 reconciler 取回 payload 并新建。

        设计 §5.3：人工/恢复取回的 payload 必须重新过输出 Schema 校验才能写
        Evidence（与 execute/reconcile 路径一致）；明确取回但 Schema 不通过
        时返回 confirmed-invalid，交由调用方 release，而非伪装成
        result_unavailable。
        """
        from app.agent_runtime.normalization import NormalizationRegistry

        existing = await self._evidence_by_call(call.id)
        if existing is not None:
            run = await self.db.get(AgentRun, call.run_id)
            if run is None or existing.run_id != call.run_id or existing.session_id != run.session_id:
                return None, True
            try:
                service = DataTapService(call.service)
                entry = resolve_allowlist_entry(service, call.internal_tool_name)
                if entry is None:
                    return None, True
                _remote_name, _description, output_schema = entry
                if not validate_reviewed_result_json(
                    service.value, output_schema, existing.raw_payload_json
                ):
                    return None, True
                validated = validate_output(existing.raw_payload_json, output_schema)
                NormalizationRegistry().normalize(call.internal_tool_name, validated)
            except (McpValidationError, TypeError, ValueError):
                return None, True
            return existing, False
        if self._tool_call_reconciler is None or not call.upstream_request_id:
            return None, False
        result = await self._tool_call_reconciler(call.upstream_request_id)
        if result is None:
            return None, False
        if getattr(result, "is_error", False):
            return None, True
        result_status, shape_error = resolve_remote_result_status(result)
        if shape_error is not None or result_status is None:
            return None, True
        if result_status in ("empty", "unavailable"):
            # Explicit empty/unavailable states are success outcomes without a
            # trusted payload.  Never let an inconsistent legacy payload leak
            # through the schema validator and become Evidence.
            return None, False
        if result.structured_content is None:
            return None, False
        try:
            service = DataTapService(call.service)
        except ValueError:
            return None, True
        entry = resolve_allowlist_entry(service, call.internal_tool_name)
        if entry is None:
            return None, True
        _remote_name, _description, output_schema = entry
        try:
            if not validate_reviewed_result_json(
                service.value, output_schema, result.structured_content
            ):
                return None, True
            validated = validate_output(result.structured_content, output_schema)
        except McpValidationError:
            return None, True
        run = await self.db.get(AgentRun, call.run_id)
        if run is None:
            return None, True
        try:
            async with self.db.begin_nested():
                evidence = await EvidenceWriter(self.db).write(
                    session_id=run.session_id,
                    run_id=call.run_id,
                    tool_call_id=call.id,
                    source_type="mcp",
                    source_name=call.internal_tool_name,
                    scope_json=self._scope_from_arguments(call.arguments_json),
                    period_json=None,
                    raw_payload=validated,
                    normalization=NormalizationRegistry().normalize(
                        call.internal_tool_name, validated
                    ),
                )
        except (EvidencePersistenceError, TypeError, ValueError):
            # The upstream result is confirmed, so local persistence failure is
            # a billable unavailable outcome, not an unknown transport result.
            return None, False
        return evidence, False

    @staticmethod
    def _scope_from_arguments(arguments: dict[str, Any] | None) -> dict[str, Any] | None:
        """只提取 scope 相关键作为 scope_json；无匹配键时返回 None（与
        agent_runtime.tools.mcp._extract_scope_period 保持一致，不把完整参数
        当 scope 存储）。"""
        if not arguments:
            return None
        keys = ("scope", "brand", "keyword", "platform", "datasource")
        scope = {key: value for key, value in arguments.items() if key in keys}
        return scope or None

    def _append_admin_reconciliation(
        self, call: AgentToolCall, decision: str, admin_id: str, note: str | None
    ) -> None:
        self.db.add(
            AgentToolCallReconciliation(
                id=str(uuid4()),
                tool_call_id=call.id,
                source="admin",
                decision=decision,
                actor_user_id=admin_id,
                note=note,
                created_at=utc_now(),
            )
        )

    @staticmethod
    def _reconcile_payload(call: AgentToolCall, *, evidence_id: str | None) -> dict[str, Any]:
        return {
            "call_id": call.id,
            "status": call.status,
            "error_type": call.error_type,
            "points_reserved": call.points_reserved,
            "points_settled": call.points_settled,
            "evidence_id": evidence_id,
        }
