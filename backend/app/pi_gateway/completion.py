"""Pi Run 的统一业务完成契约。

该模块是正常 terminal、ACK 丢失恢复以及系统 force-complete 共用的唯一成功
判定。尤其是 required artifact 必须来自 Run 创建时冻结的快照，不能从当前
profile、builder 调用记录或模型文本反推。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactPublishAttempt,
    ArtifactDraftRevision,
)
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_artifacts.schemas import FrozenLineage
from app.billing.models import TenantWalletTransaction


@dataclass(frozen=True)
class CompletionValidationResult:
    """可记录、可传输的稳定业务完成判定。"""

    ok: bool
    code: str | None = None
    detail: str | None = None
    artifact_version_id: str | None = None

    def __bool__(self) -> bool:
        return self.ok


class CompletionValidator:
    """在已锁定 Run 上执行不可绕过的 Pi 成功门禁。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def validate(self, run: AgentRun) -> CompletionValidationResult:
        """按固定顺序检查 durable message、运行中工作、未决 MCP 与产物。"""
        assistant_messages = list(
            (
                await self.db.scalars(
                    select(AgentMessage)
                    .where(
                        AgentMessage.run_id == run.id,
                        AgentMessage.role == "assistant",
                    )
                )
            ).all()
        )
        assistant_id = next(
            (
                message.id
                for message in assistant_messages
                if isinstance(message.content, str)
                and message.content.strip()
                and not (
                    isinstance(message.metadata_json, dict)
                    and message.metadata_json.get("system_loop_guard") is True
                )
            ),
            None,
        )
        if assistant_id is None:
            return CompletionValidationResult(
                False,
                "pi_gateway_terminal_missing_completion",
                "durable assistant completion is required",
            )

        running_step_id = await self.db.scalar(
            select(AgentStep.id)
            .where(AgentStep.run_id == run.id, AgentStep.status == "running")
            .limit(1)
        )
        if running_step_id is not None:
            return CompletionValidationResult(
                False,
                "pi_gateway_running_agent_steps",
                "running AgentStep remains open",
            )

        unresolved_call_id = await self.db.scalar(
            select(AgentToolCall.id)
            .where(
                AgentToolCall.run_id == run.id,
                # planned 也不能在成功终态留下；它代表还未完成 permit 生命周期。
                AgentToolCall.status.in_(
                    ("planned", "reserved", "running", "unknown")
                ),
            )
            .limit(1)
        )
        if unresolved_call_id is not None:
            return CompletionValidationResult(
                False,
                "pi_gateway_unresolved_mcp_calls",
                "MCP ToolCall or permit remains unresolved",
            )

        reserve = TenantWalletTransaction
        terminal_ledger = aliased(TenantWalletTransaction)
        unresolved_permit_id = await self.db.scalar(
            select(reserve.id)
            .where(
                reserve.run_id == run.id,
                reserve.tool_call_id.is_not(None),
                reserve.kind == "reserve",
                ~select(terminal_ledger.id)
                .where(
                    terminal_ledger.reference_id == reserve.id,
                    terminal_ledger.kind.in_(("settle", "release")),
                )
                .exists(),
            )
            .limit(1)
        )
        if unresolved_permit_id is not None:
            return CompletionValidationResult(
                False,
                "pi_gateway_unresolved_mcp_calls",
                "tenant MCP permit remains unresolved",
            )

        # The legacy/current executor has its own artifact lifecycle and does
        # not create a Pi RuntimeSnapshot.  The frozen capability-pack
        # contract below is specifically the Pi production boundary; shared
        # event code still benefits from the assistant/Step/MCP checks above.
        if run.runtime_backend != "pi":
            return CompletionValidationResult(True)

        snapshot = run.runtime_config_snapshot_json
        if not isinstance(snapshot, dict):
            return CompletionValidationResult(
                False,
                "required_artifact_missing",
                "frozen runtime snapshot is missing",
            )
        capability_pack = snapshot.get("capability_pack")
        pack_version = snapshot.get("capability_pack_version")
        manifest_digest = snapshot.get("capability_pack_manifest_digest")
        if (
            not isinstance(capability_pack, dict)
            or not isinstance(pack_version, str)
            or not pack_version
            or not isinstance(manifest_digest, str)
            or not manifest_digest
            or capability_pack.get("pack_version") != pack_version
            or capability_pack.get("manifest_digest") != manifest_digest
        ):
            return CompletionValidationResult(
                False,
                "required_artifact_missing",
                "frozen capability pack audit fields are missing or inconsistent",
            )
        required_contract = (
            snapshot.get("required_artifact_contract")
        )
        artifact_mode = snapshot.get("artifact_contract_mode")
        if artifact_mode not in {"required", "none"}:
            return CompletionValidationResult(
                False,
                "required_artifact_missing",
                "frozen artifact contract mode is missing",
            )
        if artifact_mode == "required" and not required_contract:
            return CompletionValidationResult(
                False,
                "required_artifact_missing",
                "frozen required artifact contract is missing",
            )
        if artifact_mode == "none":
            if required_contract is not None:
                return CompletionValidationResult(
                    False,
                    "required_artifact_missing",
                    "no-artifact snapshot carries a required contract",
                )
            return CompletionValidationResult(True)
        if not isinstance(required_contract, str) or not required_contract:
            return CompletionValidationResult(
                False,
                "required_artifact_missing",
                "frozen required artifact contract is invalid",
            )

        version, validation_error = await self._find_valid_published_version(
            run, required_contract
        )
        if version is None:
            code = (
                "required_artifact_invalid_lineage"
                if validation_error == "published artifact lineage snapshot is invalid"
                else "required_artifact_missing"
            )
            return CompletionValidationResult(False, code, validation_error)
        return CompletionValidationResult(True, artifact_version_id=version.id)

    async def _find_valid_published_version(
        self, run: AgentRun, required_contract: str
    ) -> tuple[AgentArtifactVersion | None, str | None]:
        """只接受当前 Run 发布的、当前 latest、lineage 完整的 Version。"""
        row = (
            await self.db.execute(
                select(
                    AgentArtifactVersion,
                    AgentArtifact,
                    ArtifactDraftRevision,
                    ArtifactPublishAttempt,
                )
                .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
                .join(AgentSession, AgentSession.id == AgentArtifact.session_id)
                .join(
                    ArtifactDraftRevision,
                    ArtifactDraftRevision.id
                    == AgentArtifactVersion.source_draft_revision_id,
                )
                .join(
                    ArtifactPublishAttempt,
                    (
                        (ArtifactPublishAttempt.published_version_id == AgentArtifactVersion.id)
                        & (ArtifactPublishAttempt.draft_revision_id == ArtifactDraftRevision.id)
                    ),
                )
                .where(
                    AgentArtifact.session_id == run.session_id,
                    AgentArtifact.user_id == run.user_id,
                    AgentSession.tenant_id == run.tenant_id,
                    AgentArtifact.status == "published",
                    AgentArtifactVersion.version == AgentArtifact.latest_version,
                    AgentArtifactVersion.schema_version == required_contract,
                    AgentArtifactVersion.source_run_id == run.id,
                    ArtifactDraftRevision.run_id == run.id,
                    ArtifactPublishAttempt.run_id == run.id,
                    ArtifactPublishAttempt.status == "published",
                    ArtifactDraftRevision.schema_version == required_contract,
                    AgentArtifactVersion.lineage_snapshot_json.is_not(None),
                    AgentArtifactVersion.validation_json.is_not(None),
                )
                .limit(1)
            )
        ).first()
        if row is None:
            return None, "no published artifact Version belongs to this Run"
        version, _artifact, _revision, publication = row
        if not await self._valid_lineage(version.lineage_snapshot_json, run, version):
            return None, "published artifact lineage snapshot is invalid"
        validation = version.validation_json
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            return None, "published artifact validation snapshot is invalid"
        publication_validation = publication.validation_json
        if not isinstance(publication_validation, dict) or publication_validation.get("valid") is not True:
            return None, "published artifact publication validation snapshot is invalid"
        if not isinstance(version.payload_json, dict):
            return None, "published artifact payload is invalid"
        return version, None

    async def _valid_lineage(
        self, value: Any, run: AgentRun, version: AgentArtifactVersion
    ) -> bool:
        if not isinstance(value, dict):
            return False
        try:
            lineage = FrozenLineage.model_validate(value)
        except (TypeError, ValueError):
            return False
        if not lineage.refs:
            return False
        evidence_ids = {
            source.evidence_id
            for ref in lineage.refs
            for source in ref.sources
        }
        if not evidence_ids:
            return False
        allowed_evidence_ids = set(
            (
                await self.db.scalars(
                    select(EvidenceItem.id).where(
                        EvidenceItem.session_id == run.session_id,
                        EvidenceItem.run_id == run.id,
                        EvidenceItem.availability_status == "available",
                    )
                )
            ).all()
        )
        # A child artifact may legitimately freeze evidence inherited through
        # its explicitly reviewed parent Version (for example an insight
        # board).  The freezer has already expanded that parent into Evidence
        # leaves; accept only those leaves, never arbitrary historical rows in
        # the same session.
        if version.parent_artifact_version_id is not None:
            parent_row = (
                await self.db.execute(
                    select(
                        AgentArtifactVersion,
                        AgentArtifact,
                        ArtifactPublishAttempt,
                    )
                    .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
                    .join(
                        ArtifactPublishAttempt,
                        (
                            (ArtifactPublishAttempt.published_version_id == AgentArtifactVersion.id)
                            & (
                                ArtifactPublishAttempt.draft_revision_id
                                == AgentArtifactVersion.source_draft_revision_id
                            )
                        ),
                    )
                    .where(
                        AgentArtifactVersion.id == version.parent_artifact_version_id,
                        AgentArtifact.session_id == run.session_id,
                        AgentArtifact.user_id == run.user_id,
                        AgentArtifact.status == "published",
                        AgentArtifactVersion.lineage_snapshot_json.is_not(None),
                        AgentArtifactVersion.validation_json.is_not(None),
                        ArtifactPublishAttempt.status == "published",
                        ArtifactPublishAttempt.validation_json.is_not(None),
                    )
                )
            ).first()
            if parent_row is None:
                return False
            parent_version, _parent_artifact, parent_publication = parent_row
            if not isinstance(parent_version.validation_json, dict) or not parent_version.validation_json.get(
                "valid"
            ):
                return False
            if not isinstance(parent_publication.validation_json, dict) or not parent_publication.validation_json.get(
                "valid"
            ):
                return False
            try:
                parent_lineage = FrozenLineage.model_validate(
                    parent_version.lineage_snapshot_json
                )
            except (TypeError, ValueError):
                return False
            if not parent_lineage.refs:
                return False
            allowed_evidence_ids.update(
                source.evidence_id
                for ref in parent_lineage.refs
                for source in ref.sources
            )
        evidence_rows = list(
            (
                await self.db.scalars(
                    select(EvidenceItem).where(
                        EvidenceItem.id.in_(evidence_ids),
                        EvidenceItem.session_id == run.session_id,
                        EvidenceItem.availability_status == "available",
                    )
                )
            ).all()
        )
        evidence_by_id = {item.id: item for item in evidence_rows}
        if not evidence_ids.issubset(allowed_evidence_ids):
            return False
        if len(evidence_by_id) != len(evidence_ids):
            return False
        for ref in lineage.refs:
            for source in ref.sources:
                evidence = evidence_by_id.get(source.evidence_id)
                if evidence is None or source.payload_hash != evidence.payload_hash:
                    return False
                if source.tool_call_id is not None and source.tool_call_id != evidence.tool_call_id:
                    return False
        return True


async def close_open_runtime_rows(
    db: AsyncSession,
    run_id: str,
    now: datetime,
    *,
    error_code: str,
) -> None:
    """失败终态前关闭 Step，并把尚未确认外发的调用保留为 unknown。

    ``reserved``/``running`` 不能在这里释放，因为失败终态本身不证明请求未
    外发；只有 ``planned`` 能确定没有外发，可以直接 release。该函数不提交，
    由统一 terminal 事务提交。
    """
    steps = (
        await db.scalars(
            select(AgentStep)
            .where(AgentStep.run_id == run_id, AgentStep.status == "running")
            .with_for_update()
        )
    ).all()
    for step in steps:
        step.status = "failed"
        step.output_json = {
            **(step.output_json or {}),
            "error_code": error_code,
            "terminal_cleanup": True,
        }

    calls = (
        await db.scalars(
            select(AgentToolCall)
            .where(
                AgentToolCall.run_id == run_id,
                AgentToolCall.status.in_(("planned", "reserved", "running")),
            )
            .with_for_update()
        )
    ).all()
    run_user_id = await db.scalar(select(AgentRun.user_id).where(AgentRun.id == run_id))
    accounting = None
    if run_user_id is not None:
        from app.agent_runtime.tools.mcp import AgentMcpAccounting

        accounting = AgentMcpAccounting(db)

    for call in calls:
        call.completed_at = now
        call.safe_error_message = f"run terminated before tool completion: {error_code}"
        if call.status == "planned":
            call.status = "failed"
            call.error_type = "definitely_not_sent"
            call.points_reserved = 0
        elif call.status == "reserved":
            # reserved 是外发前的状态：worker 尚未进入 running，因此可以确认
            # 请求没有外发，必须释放预留而不是把它伪装成 result_unknown。
            reserve_id = await db.scalar(
                select(TenantWalletTransaction.id).where(
                    TenantWalletTransaction.tool_call_id == call.id,
                    TenantWalletTransaction.kind == "reserve",
                )
            )
            if accounting is not None and reserve_id is not None and run_user_id is not None:
                await accounting.release(
                    run_user_id,
                    call,
                    error_type="definitely_not_sent",
                    message=f"run terminated before tool dispatch: {error_code}",
                )
            else:
                call.status = "failed"
                call.error_type = "definitely_not_sent"
                call.points_reserved = 0
        else:
            call.status = "unknown"
            call.error_type = "result_unknown"


__all__ = [
    "CompletionValidationResult",
    "CompletionValidator",
    "close_open_runtime_rows",
]
