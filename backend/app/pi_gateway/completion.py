"""Pi Run 的统一平台完成契约。

该模块是正常 terminal、ACK 丢失恢复以及系统 force-complete 共用的唯一成功
判定。它守住平台一致性；formal analysis Run 需要当前 Run 发布至少一个顶层
主 Artifact，但具体 contract 由 Pi 在冻结 allowlist 内自主选择。interaction、
澄清终态与 utility/kol-detail Run 不需要主报告。历史 Snapshot 的显式
required_artifact_contract 仅按旧版本语义兼容读取。
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
    ArtifactDraft,
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
    warnings: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


class CompletionValidator:
    """在已锁定 Run 上执行不可绕过的 Pi 成功门禁。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def validate(self, run: AgentRun) -> CompletionValidationResult:
        """按固定顺序检查 durable message、运行中工作、MCP 与已存在产物。

        ``result_unknown`` 是会计上的未决事实，不是平台可以擅自重放或释放
        的失败。只要没有 running/unresolved row，它会以 warning 伴随文本
        完成；真正仍在生命周期中的 permit/Step 继续阻止成功终态。formal
        analysis Run 还必须有当前 Run 的顶层已发布 Version；该判断不读取
        新 Snapshot 的固定 Artifact 类型。
        """
        warnings: list[str] = []
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

        unresolved_calls = list(
            (
                await self.db.execute(
                    select(AgentToolCall.id, AgentToolCall.status).where(
                        AgentToolCall.run_id == run.id,
                        AgentToolCall.status.in_(
                            ("planned", "reserved", "running", "unknown")
                        ),
                    )
                )
            ).all()
        )
        active_call = next(
            ((call_id, call_status) for call_id, call_status in unresolved_calls if call_status != "unknown"),
            None,
        )
        if active_call is not None:
            return CompletionValidationResult(
                False,
                "pi_gateway_unresolved_mcp_calls",
                "MCP ToolCall or permit remains unresolved",
            )
        if unresolved_calls:
            warnings.append("pi_gateway_result_unknown")

        reserve = TenantWalletTransaction
        terminal_ledger = aliased(TenantWalletTransaction)
        unresolved_reserves = list(
            (
                await self.db.execute(
                    select(reserve.id, AgentToolCall.status)
                    .outerjoin(AgentToolCall, AgentToolCall.id == reserve.tool_call_id)
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
                )
            ).all()
        )
        for _reserve_id, call_status in unresolved_reserves:
            if call_status == "unknown":
                if "pi_gateway_result_unknown" not in warnings:
                    warnings.append("pi_gateway_result_unknown")
                continue
            return CompletionValidationResult(
                False,
                "pi_gateway_unresolved_mcp_calls",
                "tenant MCP permit remains unresolved",
            )

        active_draft_id = await self.db.scalar(
            select(ArtifactDraft.id).where(
                ArtifactDraft.owner_run_id == run.id,
                ArtifactDraft.status.in_(("drafting", "reviewing")),
            ).limit(1)
        )
        if active_draft_id is not None:
            return CompletionValidationResult(
                False,
                "pi_gateway_active_artifact_draft",
                "an active Artifact Draft must be published or abandoned before completion",
            )
        abandoned_draft = await self.db.scalar(
            select(ArtifactDraft.id)
            .join(ArtifactDraftRevision, ArtifactDraftRevision.draft_id == ArtifactDraft.id)
            .where(
                ArtifactDraftRevision.run_id == run.id,
                ArtifactDraft.status == "failed",
            )
            .limit(1)
        )
        if abandoned_draft is not None:
            warnings.append("pi_gateway_abandoned_artifact_draft")

        # A Draft owned by another live Run is not an unfinished obligation of
        # this Run. Cross-Run isolation is checked by publication/lineage, but
        # it must not turn an otherwise valid text completion into a global
        # session business gate.
        # The legacy/current executor has its own artifact lifecycle and does
        # not create a Pi RuntimeSnapshot.  Shared lifecycle checks above still
        # apply to it.
        if run.runtime_backend != "pi":
            return CompletionValidationResult(True, warnings=tuple(warnings))

        snapshot = run.runtime_config_snapshot_json
        if not isinstance(snapshot, dict):
            return CompletionValidationResult(
                False,
                "pi_gateway_snapshot_invalid",
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
                "pi_gateway_snapshot_invalid",
                "frozen capability pack audit fields are missing or inconsistent",
            )

        allowed_contracts = snapshot.get("allowed_artifact_contracts", ())
        if not isinstance(allowed_contracts, (list, tuple)) or any(
            not isinstance(contract, str) or not contract for contract in allowed_contracts
        ) or len(set(allowed_contracts)) != len(allowed_contracts):
            return CompletionValidationResult(
                False,
                "pi_gateway_snapshot_invalid",
                "frozen artifact contract allowlist is invalid",
            )
        completion_mode = snapshot.get("completion_mode", "formal_analysis")
        if completion_mode not in {"formal_analysis", "interaction"}:
            return CompletionValidationResult(
                False,
                "pi_gateway_snapshot_invalid",
                "frozen completion mode is invalid",
            )

        # Historical Snapshots may carry an explicit required contract.  Keep
        # that immutable meaning for replay only; new Run Snapshots omit both
        # legacy fields and use the candidate allowlist below.
        legacy_mode = snapshot.get("artifact_contract_mode")
        legacy_contract = snapshot.get("required_artifact_contract")
        if legacy_mode == "required":
            if not isinstance(legacy_contract, str) or not legacy_contract:
                return CompletionValidationResult(
                    False,
                    "required_artifact_missing",
                    "historical required artifact contract is invalid",
                )
            version, validation_error = await self._find_valid_published_version(
                run, legacy_contract
            )
            if version is None:
                code = (
                    "required_artifact_invalid_lineage"
                    if validation_error == "published artifact lineage snapshot is invalid"
                    else "required_artifact_missing"
                )
                return CompletionValidationResult(False, code, validation_error)
            return CompletionValidationResult(
                True, artifact_version_id=version.id, warnings=tuple(warnings)
            )
        if legacy_contract is not None:
            return CompletionValidationResult(
                False,
                "required_artifact_missing",
                "historical required artifact contract mode is invalid",
            )

        versions = await self._current_published_versions(run)
        if not versions:
            published_attempt_exists = await self.db.scalar(
                select(ArtifactPublishAttempt.id)
                .where(
                    ArtifactPublishAttempt.run_id == run.id,
                    ArtifactPublishAttempt.status == "published",
                )
                .limit(1)
            )
            if published_attempt_exists is not None:
                return CompletionValidationResult(
                    False,
                    "pi_gateway_artifact_invalid",
                    "published artifact Version is missing or does not belong to this Run",
                )
            if self._requires_main_report(run, completion_mode=completion_mode):
                return CompletionValidationResult(
                    False,
                    "pi_gateway_main_artifact_missing",
                    "a formal analysis Run requires a current top-level published main Artifact",
                )
            return CompletionValidationResult(True, warnings=tuple(warnings))

        main_versions = [
            row for row in versions if row[1].parent_artifact_id is None
        ]
        if self._requires_main_report(run, completion_mode=completion_mode) and not main_versions:
            return CompletionValidationResult(
                False,
                "pi_gateway_main_artifact_missing",
                "child insight Artifacts do not satisfy the formal analysis main Artifact requirement",
            )

        for version, artifact, revision, publication in versions:
            if (
                version.artifact_id != artifact.id
                or version.source_draft_revision_id != revision.id
                or publication.published_version_id != version.id
                or publication.artifact_id != artifact.id
                or revision.schema_version != version.schema_version
                or revision.artifact_id != artifact.id
            ):
                return CompletionValidationResult(
                    False,
                    "pi_gateway_artifact_invalid",
                    "published artifact Version chain is inconsistent",
                )
            if version.schema_version not in set(allowed_contracts):
                return CompletionValidationResult(
                    False,
                    "pi_gateway_artifact_contract_not_allowed",
                    "published artifact contract is outside the frozen capability allowlist",
                )
            validation_error = await self._validate_published_version(
                run, version, publication
            )
            if validation_error is not None:
                code = "pi_gateway_artifact_invalid"
                return CompletionValidationResult(False, code, validation_error)
        return CompletionValidationResult(
            True,
            artifact_version_id=(
                main_versions[0][0].id if main_versions else versions[0][0].id
            ),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _requires_main_report(
        run: AgentRun, *, completion_mode: str = "formal_analysis"
    ) -> bool:
        """判断当前 Run 是否是显式 formal analysis 完成边界。

        ``completion_mode`` 是建 Run 时由服务端 RuntimeSnapshot 冻结的显式
        平台元数据，不是用户文本、模型输出或 Builder 推导出的 Artifact
        目标。缺省保持普通用户 session analyst 的 formal 语义；interaction
        仅用于明确的文本/协议交互 Run。具体主报告类型始终由 Snapshot
        allowlist + Pi 选择决定。
        """
        if completion_mode == "interaction":
            return False
        return (
            run.runtime_backend == "pi"
            and run.run_kind == "user"
            and run.visibility == "user"
            and run.status != "clarification_requested"
            and not run.profile_name.startswith("utility_")
            and run.profile_name != "kol_detail_v1"
        )

    async def _find_valid_published_version(
        self, run: AgentRun, required_contract: str
    ) -> tuple[AgentArtifactVersion | None, str | None]:
        """只按历史 Snapshot 的固定 contract 查找当前 Run 的合法 Version。"""
        rows = await self._current_published_versions(
            run, required_contract=required_contract
        )
        if not rows:
            return None, "no published artifact Version belongs to this Run"
        for version, _artifact, _revision, publication in rows:
            error = await self._validate_published_version(run, version, publication)
            if error is None:
                return version, None
            return None, error
        return None, "published artifact Version is invalid"

    async def _current_published_versions(
        self, run: AgentRun, *, required_contract: str | None = None
    ) -> list[tuple[AgentArtifactVersion, AgentArtifact, ArtifactDraftRevision, ArtifactPublishAttempt]]:
        """Load only versions whose complete publication chain belongs to Run."""
        statement = (
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
                ArtifactDraftRevision.id == AgentArtifactVersion.source_draft_revision_id,
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
                AgentArtifactVersion.source_run_id == run.id,
                ArtifactDraftRevision.run_id == run.id,
                ArtifactPublishAttempt.run_id == run.id,
                ArtifactPublishAttempt.status == "published",
            )
        )
        if required_contract is not None:
            statement = statement.where(
                AgentArtifactVersion.schema_version == required_contract
            )
        return list((await self.db.execute(statement)).all())

    async def _validate_published_version(
        self,
        run: AgentRun,
        version: AgentArtifactVersion,
        publication: ArtifactPublishAttempt,
    ) -> str | None:
        """Validate lineage and immutable publication snapshots for one Version."""
        if version.lineage_snapshot_json is None:
            return "published artifact lineage snapshot is invalid"
        if not await self._valid_lineage(version.lineage_snapshot_json, run, version):
            return "published artifact lineage snapshot is invalid"
        validation = version.validation_json
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            return "published artifact validation snapshot is invalid"
        publication_validation = publication.validation_json
        if not isinstance(publication_validation, dict) or publication_validation.get("valid") is not True:
            return "published artifact publication validation snapshot is invalid"
        if not isinstance(version.payload_json, dict):
            return "published artifact payload is invalid"
        return None

    async def _valid_lineage(
        self, value: Any, run: AgentRun, version: AgentArtifactVersion
    ) -> bool:
        if not isinstance(value, dict):
            return False
        if value.get("mode") == "model_direct_v1":
            # Direct Artifact Skill payloads have no trusted Evidence claim.
            # The marker is explicit and still binds optional audit handles to
            # this exact Run; it cannot be used by a historical Artifact.
            if set(value) - {"mode", "refs", "source_tool_call_ids"}:
                return False
            if value.get("refs") != [] or version.source_run_id != run.id:
                return False
            source_ids = value.get("source_tool_call_ids", [])
            if (
                not isinstance(source_ids, list)
                or len(source_ids) > 32
                or any(not isinstance(item, str) or not item for item in source_ids)
                or len(set(source_ids)) != len(source_ids)
            ):
                return False
            if source_ids:
                owned = set(
                    (
                        await self.db.scalars(
                            select(AgentToolCall.id).where(
                                AgentToolCall.run_id == run.id,
                                AgentToolCall.id.in_(source_ids),
                            )
                        )
                    ).all()
                )
                if owned != set(source_ids):
                    return False
            return True
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
