from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.models import McpCall
from app.reporting.models import Kol, KolSnapshot, TaskCandidate
from app.reporting.normalizers import normalize_tool_evidence
from app.reporting.schemas import CandidateVersion, CandidateVersionItem, ToolEvidence
from app.reporting.scoring import SCORE_VERSION, score_candidate
from app.tasks.models import AnalysisTask


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class ReportingService:
    """把已结算 MCP 证据转换成不可变、可恢复的候选版本。"""

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def build_candidate_version(self, task_id: str, profile: str) -> CandidateVersion:
        async with self._transaction():
            task = await self._db.scalar(
                select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
            )
            if task is None:
                raise LookupError("analysis_task_not_found")
            if task.plan_json is None:
                raise ValueError("analysis_task_not_ready_for_reporting")

            evidence = await self._successful_evidence(task_id)
            normalized = normalize_tool_evidence(evidence)
            if not normalized:
                raise ValueError("no_successful_tool_evidence")
            evidence_digest = _digest(
                {
                    "profile": profile,
                    "score_version": SCORE_VERSION,
                    "evidence": [row.as_dict() for row in normalized],
                }
            )
            existing = await self._existing_version(task_id, evidence_digest)
            if existing is not None:
                return existing

            candidate_version = await self._next_candidate_version(task_id)
            draft: list[tuple[Kol, KolSnapshot, Any, Any]] = []
            for row in normalized:
                kol = await self._upsert_kol(row.platform, row.platform_account_id, row.normalized_profile_url)
                snapshot = KolSnapshot(
                    id=str(uuid4()),
                    kol_id=kol.id,
                    source_mcp_call_id=row.evidence_references[0]
                    if row.evidence_references
                    else None,
                    normalized_json=row.as_dict(),
                    collected_at=row.collected_at,
                    created_at=_now(),
                )
                score = score_candidate(row.dimensions(), profile)
                draft.append((kol, snapshot, row, score))

            draft.sort(
                key=lambda item: (
                    -item[3].total,
                    -(item[3].dimensions["audience"].raw_score or 0),
                    -(item[3].dimensions["engagement"].raw_score or 0),
                    item[0].platform_account_id,
                )
            )
            created_at = _now()
            for rank, (kol, snapshot, row, score) in enumerate(draft, start=1):
                self._db.add(snapshot)
                self._db.add(
                    TaskCandidate(
                        id=str(uuid4()),
                        task_id=task.id,
                        kol_id=kol.id,
                        snapshot_id=snapshot.id,
                        candidate_version=candidate_version,
                        total_score=Decimal(str(score.total)),
                        score_breakdown_json=score.as_dict(),
                        rank=rank,
                        matched_conditions_json=["normalized_evidence"],
                        risk_flags_json=list(row.risk_flags),
                        recommendation_text="",
                        evidence_json={
                            "candidate_set_digest": evidence_digest,
                            "source_call_ids": list(row.evidence_references),
                            "normalized": row.as_dict(),
                        },
                        created_at=created_at,
                    )
                )
            await self._db.flush()
            return CandidateVersion(
                candidate_version=candidate_version,
                evidence_digest=evidence_digest,
                candidates=tuple(
                    CandidateVersionItem(
                        platform=kol.platform,
                        platform_account_id=kol.platform_account_id,
                        rank=rank,
                        total_score=score.total,
                        snapshot_id=snapshot.id,
                        kol_id=kol.id,
                    )
                    for rank, (kol, snapshot, _row, score) in enumerate(draft, start=1)
                ),
            )

    async def _successful_evidence(self, task_id: str) -> tuple[ToolEvidence, ...]:
        calls = list(
            (
                await self._db.scalars(
                    select(McpCall)
                    .where(
                        McpCall.task_id == task_id,
                        McpCall.status == McpCallStatus.SETTLED.value,
                    )
                    .order_by(McpCall.plan_step_id, McpCall.id)
                )
            ).all()
        )
        result: list[ToolEvidence] = []
        for call in calls:
            evidence = call.evidence_json or {}
            payload = evidence.get("structured_content")
            if evidence.get("outcome") != "succeeded" or not isinstance(payload, dict):
                continue
            result.append(
                ToolEvidence(
                    internal_tool_name=call.internal_tool_name,
                    payload=payload,
                    source_call_id=call.id,
                    collected_at=call.completed_at or call.updated_at,
                )
            )
        return tuple(result)

    async def _existing_version(
        self, task_id: str, evidence_digest: str
    ) -> CandidateVersion | None:
        rows = list(
            (
                await self._db.execute(
                    select(TaskCandidate, Kol)
                    .join(Kol, Kol.id == TaskCandidate.kol_id)
                    .where(TaskCandidate.task_id == task_id)
                    .order_by(
                        TaskCandidate.candidate_version.desc(),
                        TaskCandidate.rank.asc(),
                    )
                )
            ).all()
        )
        versions: dict[int, list[tuple[TaskCandidate, Kol]]] = defaultdict(list)
        for candidate, kol in rows:
            versions[candidate.candidate_version].append((candidate, kol))
        for version, candidates in versions.items():
            if all(
                candidate.evidence_json.get("candidate_set_digest") == evidence_digest
                for candidate, _kol in candidates
            ):
                return CandidateVersion(
                    candidate_version=version,
                    evidence_digest=evidence_digest,
                    candidates=tuple(
                        CandidateVersionItem(
                            platform=kol.platform,
                            platform_account_id=kol.platform_account_id,
                            rank=candidate.rank,
                            total_score=float(candidate.total_score),
                            snapshot_id=candidate.snapshot_id,
                            kol_id=candidate.kol_id,
                        )
                        for candidate, kol in candidates
                    ),
                )
        return None

    async def _next_candidate_version(self, task_id: str) -> int:
        rows = list(
            (
                await self._db.scalars(
                    select(TaskCandidate.candidate_version)
                    .where(TaskCandidate.task_id == task_id)
                    .order_by(TaskCandidate.candidate_version.desc())
                )
            ).all()
        )
        return (rows[0] if rows else 0) + 1

    async def _upsert_kol(
        self, platform: str, account_id: str, profile_url: str | None
    ) -> Kol:
        kol = await self._db.scalar(
            select(Kol)
            .where(Kol.platform == platform, Kol.platform_account_id == account_id)
            .with_for_update()
        )
        if kol is not None:
            if profile_url is not None:
                kol.normalized_profile_url = profile_url
                kol.updated_at = _now()
            return kol
        now = _now()
        kol = Kol(
            id=str(uuid4()),
            platform=platform,
            platform_account_id=account_id,
            normalized_profile_url=profile_url,
            created_at=now,
            updated_at=now,
        )
        self._db.add(kol)
        await self._db.flush()
        return kol

    def _transaction(self):
        return self._db.begin_nested() if self._db.in_transaction() else self._db.begin()
