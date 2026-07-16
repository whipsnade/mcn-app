from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.models import McpCall
from app.orchestration.export_contract import EXPORT_FIELD_CONTRACT_VERSION
from app.reporting.models import (
    BiReport,
    Kol,
    KolSnapshot,
    TaskCandidate,
    TaskCandidatePool,
    TaskCandidatePoolItem,
    UserKolFavorite,
)
from app.reporting.normalizers import normalize_tool_evidence, redact_evidence_for_storage
from app.reporting.schemas import AnalystConclusion, CandidateVersion, CandidateVersionItem, ToolEvidence
from app.reporting.scoring import SCORE_VERSION, score_candidate
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.state import TaskEventType
from app.workspace.models import WorkspaceSession


_FINAL_CANDIDATE_LIMIT = 10


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class ReportingService:
    """把已结算 MCP 证据转换成不可变、可恢复的候选版本。"""

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    @staticmethod
    def _select_top_candidates(
        draft: list[tuple[Kol, KolSnapshot, Any, Any]],
    ) -> list[tuple[Kol, KolSnapshot, Any, Any]]:
        return ReportingService._rank_candidates(draft)[:_FINAL_CANDIDATE_LIMIT]

    @staticmethod
    def _rank_candidates(
        draft: list[tuple[Kol, KolSnapshot, Any, Any]],
    ) -> list[tuple[Kol, KolSnapshot, Any, Any]]:
        return sorted(
            draft,
            key=lambda item: (
                -item[3].total,
                -(item[3].dimensions["audience"].raw_score or 0),
                -(item[3].dimensions["engagement"].raw_score or 0),
                item[0].platform_account_id,
            ),
        )

    async def build_candidate_version(
        self, task_id: str, profile: str, *, lease_owner: str | None = None
    ) -> CandidateVersion:
        async with self._transaction():
            task = await self._db.scalar(
                select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
            )
            if task is None:
                raise LookupError("analysis_task_not_found")
            self._require_active_lease(task, lease_owner)
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
                normalized_json = redact_evidence_for_storage(row.as_dict())
                snapshot = KolSnapshot(
                    id=str(uuid4()),
                    kol_id=kol.id,
                    source_mcp_call_id=row.evidence_references[0]
                    if row.evidence_references
                    else None,
                    normalized_json=normalized_json,
                    collected_at=row.collected_at,
                    created_at=_now(),
                )
                score = score_candidate(row.dimensions(), profile)
                draft.append((kol, snapshot, row, score))

            ranked_draft = self._rank_candidates(draft)
            selected_draft = ranked_draft[:_FINAL_CANDIDATE_LIMIT]
            created_at = _now()
            pool = TaskCandidatePool(
                id=str(uuid4()),
                task_id=task.id,
                pool_version=candidate_version,
                field_contract_version=EXPORT_FIELD_CONTRACT_VERSION,
                candidate_count=len(ranked_draft),
                evidence_digest=evidence_digest,
                created_at=created_at,
            )
            self._db.add(pool)
            await self._db.flush()
            selected_ids = {id(snapshot) for _kol, snapshot, _row, _score in selected_draft}
            for full_rank, (kol, snapshot, row, score) in enumerate(ranked_draft, start=1):
                self._db.add(snapshot)
                self._db.add(
                    TaskCandidatePoolItem(
                        id=str(uuid4()),
                        pool_id=pool.id,
                        kol_id=kol.id,
                        snapshot_id=snapshot.id,
                        full_rank=full_rank,
                        is_shortlisted=id(snapshot) in selected_ids,
                        total_score=Decimal(str(score.total)),
                        score_breakdown_json=score.as_dict(),
                        risk_flags_json=redact_evidence_for_storage(list(row.risk_flags)),
                        evidence_json={
                            "candidate_set_digest": evidence_digest,
                            "source_call_ids": list(row.evidence_references),
                            "normalized": redact_evidence_for_storage(row.as_dict()),
                        },
                        created_at=created_at,
                    )
                )
            for rank, (kol, snapshot, row, score) in enumerate(selected_draft, start=1):
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
                        risk_flags_json=redact_evidence_for_storage(list(row.risk_flags)),
                        recommendation_text="",
                        evidence_json={
                            "candidate_set_digest": evidence_digest,
                            "source_call_ids": list(row.evidence_references),
                            "normalized": normalized_json,
                        },
                        created_at=created_at,
                    )
                )
            await self._db.flush()
            await self._append_event(
                task,
                TaskEventType.CANDIDATES_UPDATED,
                {
                    "candidate_version": candidate_version,
                    "total": len(selected_draft),
                    "pool_total": len(ranked_draft),
                    "phase": "ai_summary",
                    "label": "AI 汇总",
                },
            )
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
                    for rank, (kol, snapshot, _row, score) in enumerate(selected_draft, start=1)
                ),
            )

    async def build_bi_report(
        self,
        task_id: str,
        *,
        analyst_conclusion: AnalystConclusion | None = None,
        lease_owner: str | None = None,
    ) -> BiReport:
        """从最新的不可变候选版本派生确定性 BI；模型结论不会参与评分或版本选择。"""
        async with self._transaction():
            task = await self._db.scalar(
                select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
            )
            if task is None:
                raise LookupError("analysis_task_not_found")
            self._require_active_lease(task, lease_owner)
            candidate_version = await self._latest_candidate_version(task.id)
            if candidate_version is None:
                raise ValueError("candidate_version_not_found")
            existing = await self._db.scalar(
                select(BiReport)
                .where(
                    BiReport.task_id == task.id,
                    BiReport.candidate_version == candidate_version,
                    BiReport.status == "completed",
                )
                .order_by(BiReport.report_version.desc())
            )
            if existing is not None:
                return existing
            candidates = await self._candidate_rows(task.id, candidate_version)
            if not candidates:
                raise ValueError("candidate_version_empty")
            chart_data, conclusion, evidence = await self._build_bi_payload(candidates, candidate_version)
            if analyst_conclusion is not None:
                conclusion = analyst_conclusion.conclusion
                evidence["analyst"] = analyst_conclusion.model_dump(mode="json")
            next_version = await self._next_report_version(task.id)
            now = _now()
            report = BiReport(
                id=str(uuid4()),
                task_id=task.id,
                session_id=task.session_id,
                candidate_version=candidate_version,
                report_version=next_version,
                chart_data_json=chart_data,
                conclusion_text=conclusion,
                evidence_json=evidence,
                status="completed",
                completed_at=now,
                created_at=now,
                updated_at=now,
            )
            self._db.add(report)
            await self._db.flush()
            await self._append_event(
                task,
                TaskEventType.BI_UPDATED,
                {
                    "report_id": report.id,
                    "report_version": report.report_version,
                    "candidate_version": candidate_version,
                    "phase": "ai_summary",
                    "label": "BI 报告已生成",
                },
            )
            return report

    async def analyst_input(self, task_id: str) -> dict[str, Any]:
        """提供已脱敏的确定性 BI 输入；不暴露原始 MCP 载荷。"""
        task = await self._db.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id))
        if task is None:
            raise LookupError("analysis_task_not_found")
        version = await self._latest_candidate_version(task_id)
        if version is None:
            raise ValueError("candidate_version_not_found")
        candidates = await self._candidate_rows(task_id, version)
        if not candidates:
            raise ValueError("candidate_version_empty")
        chart_data, _conclusion, _evidence = await self._build_bi_payload(candidates, version)
        return {"task_id": task_id, "candidate_version": version, "bi": chart_data}

    async def list_candidates(
        self, user_id: str, task_id: str, *, sort: str, direction: str
    ) -> tuple[int, list[tuple[TaskCandidate, Kol, KolSnapshot]]]:
        await self._owned_task(user_id, task_id)
        version = await self._latest_candidate_version(task_id)
        if version is None:
            return 0, []
        rows = await self._candidate_rows(task_id, version)
        reverse = direction == "desc"
        valid_sorts = {"rank", "total", "audience", "content", "engagement", "budget", "growth", "brand_safety"}
        selected_sort = sort if sort in valid_sorts else "rank"

        def numeric_score(candidate: TaskCandidate) -> float:
            if selected_sort == "rank":
                return float(candidate.rank)
            if selected_sort == "total":
                return float(candidate.total_score)
            dimensions = candidate.score_breakdown_json.get("dimensions", {})
            value = dimensions.get(selected_sort, {}).get("raw_score")
            return float(value) if value is not None else -1.0

        # rank 的默认升序符合用户看到的候选序，其他字段可由 direction 决定。
        if reverse:
            rows.sort(key=lambda row: row[0].rank)
            rows.sort(key=lambda row: numeric_score(row[0]), reverse=True)
        else:
            rows.sort(key=lambda row: (numeric_score(row[0]), row[0].rank))
        return version, rows

    async def get_owned_report(self, user_id: str, report_id: str) -> BiReport:
        report = await self._db.scalar(
            select(BiReport)
            .join(AnalysisTask, AnalysisTask.id == BiReport.task_id)
            .join(WorkspaceSession, WorkspaceSession.id == AnalysisTask.session_id)
            .where(
                BiReport.id == report_id,
                AnalysisTask.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
        )
        if report is None:
            raise LookupError("report_not_found")
        return report

    async def list_favorites(self, user_id: str) -> list[tuple[UserKolFavorite, Kol]]:
        return list(
            (
                await self._db.execute(
                    select(UserKolFavorite, Kol)
                    .join(Kol, Kol.id == UserKolFavorite.kol_id)
                    .where(UserKolFavorite.user_id == user_id)
                    .order_by(UserKolFavorite.created_at.desc())
                )
            ).all()
        )

    async def create_favorite(
        self, user_id: str, *, kol_id: str, note: str | None, source_task_id: str | None
    ) -> tuple[UserKolFavorite, Kol]:
        async with self._transaction():
            kol = await self._db.get(Kol, kol_id)
            if kol is None:
                raise LookupError("kol_not_found")
            if source_task_id is not None:
                await self._owned_task(user_id, source_task_id)
                candidate = await self._db.scalar(
                    select(TaskCandidate.id).where(
                        TaskCandidate.task_id == source_task_id, TaskCandidate.kol_id == kol_id
                    )
                )
                if candidate is None:
                    raise LookupError("candidate_not_found")
            now = _now()
            statement = mysql_insert(UserKolFavorite).values(
                id=str(uuid4()),
                user_id=user_id,
                kol_id=kol_id,
                note=note,
                source_task_id=source_task_id,
                created_at=now,
                updated_at=now,
            )
            await self._db.execute(
                statement.on_duplicate_key_update(
                    note=func.coalesce(statement.inserted.note, UserKolFavorite.note),
                    source_task_id=func.coalesce(
                        statement.inserted.source_task_id, UserKolFavorite.source_task_id
                    ),
                    updated_at=now,
                )
            )
            await self._db.flush()
            favorite = await self._db.scalar(
                select(UserKolFavorite).where(
                    UserKolFavorite.user_id == user_id, UserKolFavorite.kol_id == kol_id
                )
            )
            if favorite is None:
                raise RuntimeError("favorite_upsert_failed")
            return favorite, kol

    async def delete_favorite(self, user_id: str, kol_id: str) -> None:
        async with self._transaction():
            favorite = await self._db.scalar(
                select(UserKolFavorite)
                .where(UserKolFavorite.user_id == user_id, UserKolFavorite.kol_id == kol_id)
                .with_for_update()
            )
            if favorite is None:
                raise LookupError("favorite_not_found")
            await self._db.delete(favorite)
            await self._db.flush()

    async def latest_session_analysis(
        self, user_id: str, session_id: str
    ) -> tuple[AnalysisTask | None, int | None, int, BiReport | None]:
        task = await self._db.scalar(
            select(AnalysisTask)
            .join(WorkspaceSession, WorkspaceSession.id == AnalysisTask.session_id)
            .where(
                AnalysisTask.user_id == user_id,
                AnalysisTask.session_id == session_id,
                WorkspaceSession.deleted_at.is_(None),
            )
            .order_by(AnalysisTask.created_at.desc())
        )
        if task is None:
            return None, None, 0, None
        version = await self._latest_candidate_version(task.id)
        total = 0
        if version is not None:
            total = int(
                await self._db.scalar(
                    select(func.count()).select_from(TaskCandidate).where(
                        TaskCandidate.task_id == task.id,
                        TaskCandidate.candidate_version == version,
                    )
                )
                or 0
            )
        report = await self._db.scalar(
            select(BiReport)
            .where(BiReport.task_id == task.id, BiReport.candidate_version == version)
            .order_by(BiReport.report_version.desc())
        )
        return task, version, total, report

    async def latest_candidate_pool(
        self, user_id: str, session_id: str
    ) -> tuple[
        AnalysisTask | None,
        TaskCandidatePool | None,
        list[tuple[TaskCandidatePoolItem, Kol, KolSnapshot]],
    ]:
        task = await self._db.scalar(
            select(AnalysisTask)
            .join(WorkspaceSession, WorkspaceSession.id == AnalysisTask.session_id)
            .where(
                AnalysisTask.user_id == user_id,
                AnalysisTask.session_id == session_id,
                WorkspaceSession.deleted_at.is_(None),
            )
            .order_by(AnalysisTask.created_at.desc())
        )
        if task is None:
            return None, None, []
        pool = await self._db.scalar(
            select(TaskCandidatePool)
            .where(TaskCandidatePool.task_id == task.id)
            .order_by(TaskCandidatePool.pool_version.desc())
        )
        if pool is None:
            # 兼容导出候选池上线前已完成的任务：候选版本仍是同一任务的数据源。
            version = await self._latest_candidate_version(task.id)
            if version is None:
                return task, None, []
            return task, None, await self._candidate_rows(task.id, version)
        rows = list(
            (
                await self._db.execute(
                    select(TaskCandidatePoolItem, Kol, KolSnapshot)
                    .join(Kol, Kol.id == TaskCandidatePoolItem.kol_id)
                    .join(KolSnapshot, KolSnapshot.id == TaskCandidatePoolItem.snapshot_id)
                    .where(TaskCandidatePoolItem.pool_id == pool.id)
                    .order_by(TaskCandidatePoolItem.full_rank.asc())
                )
            ).all()
        )
        return task, pool, rows

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

    async def _latest_candidate_version(self, task_id: str) -> int | None:
        return await self._db.scalar(
            select(func.max(TaskCandidate.candidate_version)).where(TaskCandidate.task_id == task_id)
        )

    async def _candidate_rows(
        self, task_id: str, candidate_version: int
    ) -> list[tuple[TaskCandidate, Kol, KolSnapshot]]:
        return list(
            (
                await self._db.execute(
                    select(TaskCandidate, Kol, KolSnapshot)
                    .join(Kol, Kol.id == TaskCandidate.kol_id)
                    .join(KolSnapshot, KolSnapshot.id == TaskCandidate.snapshot_id)
                    .where(
                        TaskCandidate.task_id == task_id,
                        TaskCandidate.candidate_version == candidate_version,
                    )
                    .order_by(TaskCandidate.rank.asc())
                )
            ).all()
        )

    async def _next_report_version(self, task_id: str) -> int:
        latest = await self._db.scalar(
            select(func.max(BiReport.report_version)).where(BiReport.task_id == task_id)
        )
        return int(latest or 0) + 1

    async def _owned_task(self, user_id: str, task_id: str) -> AnalysisTask:
        task = await self._db.scalar(
            select(AnalysisTask)
            .join(WorkspaceSession, WorkspaceSession.id == AnalysisTask.session_id)
            .where(
                AnalysisTask.id == task_id,
                AnalysisTask.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
        )
        if task is None:
            raise LookupError("task_not_found")
        return task

    @staticmethod
    def _require_active_lease(task: AnalysisTask, lease_owner: str | None) -> None:
        if lease_owner is None:
            return
        if (
            task.lease_owner != lease_owner
            or task.lease_expires_at is None
            or task.lease_expires_at <= _now()
        ):
            raise RuntimeError("task_lease_lost")

    async def _append_event(
        self, task: AnalysisTask, event_type: TaskEventType, payload: dict[str, Any]
    ) -> None:
        self._db.add(
            TaskEvent(
                task_id=task.id,
                user_id=task.user_id,
                event_type=event_type,
                payload_json=payload,
                created_at=_now(),
            )
        )
        await self._db.flush()

    async def _build_bi_payload(
        self, candidates: list[tuple[TaskCandidate, Kol, KolSnapshot]], candidate_version: int
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        dimensions = ("audience", "content", "engagement", "budget", "growth", "brand_safety")
        score_composition = []
        for dimension in dimensions:
            values = [
                row.score_breakdown_json.get("dimensions", {}).get(dimension, {}).get("raw_score")
                for row, _kol, _snapshot in candidates
            ]
            usable = [float(value) for value in values if value is not None]
            score_composition.append(
                {"dimension": dimension, "average": round(sum(usable) / len(usable), 2) if usable else None}
            )
        platform_counts: dict[str, int] = {}
        risks: list[dict[str, Any]] = []
        comparison: list[dict[str, Any]] = []
        source_ids: list[str] = []
        for candidate, kol, snapshot in candidates:
            platform_counts[kol.platform] = platform_counts.get(kol.platform, 0) + 1
            risks.extend(candidate.risk_flags_json)
            nickname = str(snapshot.normalized_json.get("nickname") or "未命名达人")
            comparison.append(
                {
                    "nickname": nickname,
                    "platform": kol.platform,
                    "rank": candidate.rank,
                    "total_score": float(candidate.total_score),
                }
            )
            if snapshot.source_mcp_call_id is not None:
                source_ids.append(snapshot.source_mcp_call_id)
        top, top_kol, _ = candidates[0]
        top_snapshot = candidates[0][2]
        top_nickname = str(top_snapshot.normalized_json.get("nickname") or "未命名达人")
        source_rows = {
            row.id: row
            for row in (
                await self._db.scalars(select(McpCall).where(McpCall.id.in_(set(source_ids))))
            ).all()
        } if source_ids else {}
        chart_data = {
            "overview": {
                "candidate_count": len(candidates),
                "candidate_version": candidate_version,
                "top_nickname": top_nickname,
                "top_score": float(top.total_score),
            },
            "score_composition": score_composition,
            "audience_content_fit": {
                "audience": next(item["average"] for item in score_composition if item["dimension"] == "audience"),
                "content": next(item["average"] for item in score_composition if item["dimension"] == "content"),
            },
            "platform_distribution": [
                {"platform": platform, "count": count}
                for platform, count in sorted(platform_counts.items())
            ],
            "budget_analysis": {
                "average_budget_score": next(item["average"] for item in score_composition if item["dimension"] == "budget")
            },
            "comparison": comparison,
            "risks": risks,
            "sources": [
                {
                    "tool_name_cn": self._source_tool_name(source_rows[source_id]),
                    "collected_at": (source_rows[source_id].completed_at or source_rows[source_id].updated_at).isoformat(),
                    "evidence_id": source_id,
                }
                for source_id in sorted(source_rows)
            ],
        }
        conclusion = f"已基于候选版本 {candidate_version} 生成 {len(candidates)} 位达人对比，当前首选为 {top_nickname}。"
        return chart_data, conclusion, {"candidate_version": candidate_version, "source_call_ids": sorted(set(source_ids))}

    @staticmethod
    def _source_tool_name(call: McpCall) -> str:
        names = {
            "insight-cube-mcp": "聆媒洞察",
            "social-grow-mcp": "达人精选",
            "social-grow-content-mcp": "内容选题",
            "aktools-mcp": "AkTools金融数据",
            "bilibili-mcp": "B站数据采集",
        }
        return names.get(call.service_slug, "已授权数据服务")

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
