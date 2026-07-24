"""迁移 0023 旧数据回填：历史圈选名单与报告登记进 goal/artifact 新表。

回填全部幂等（按 title/artifact_key/已存在 item 跳过），可重复执行。
同步实现供 Alembic 迁移直接调用；异步入口经 AsyncSession.run_sync 包装，供测试与代码使用。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.artifacts.models import TaskArtifact
from app.reporting.models import AnalysisReport
from app.selection.models import KolSelectionItem, KolSelectionSet, SessionKolSelection

# 历史默认名单固定标题：既是回填幂等键，也是 downgrade 的删除依据。
LEGACY_SET_TITLE = "历史默认名单"
LEGACY_ARTIFACT_KEY_PREFIX = "legacy:"


@dataclass
class BackfillStats:
    """回填执行统计（创建/跳过的各类行数）。"""

    report_type_updated: int = 0
    selection_sets_created: int = 0
    selection_items_created: int = 0
    selection_items_skipped: int = 0
    report_artifacts_created: int = 0
    report_artifacts_skipped: int = 0
    set_artifacts_created: int = 0
    set_artifacts_skipped: int = 0


def run_backfill_sync(session: Session) -> BackfillStats:
    """同步回填实现（Alembic upgrade 与 run_backfill 的公共逻辑）。"""
    stats = BackfillStats()
    now = datetime.now(UTC).replace(tzinfo=None)

    # report_type 兜底：0022 的 server_default 只覆盖新写入，防御性补齐 NULL。
    result = session.execute(
        update(AnalysisReport)
        .where(AnalysisReport.report_type.is_(None))
        .values(report_type="kol_analysis")
    )
    stats.report_type_updated = result.rowcount or 0

    _backfill_selection_sets(session, stats, now)

    existing_keys = set(
        session.scalars(
            select(TaskArtifact.artifact_key).where(
                TaskArtifact.artifact_key.like(f"{LEGACY_ARTIFACT_KEY_PREFIX}%")
            )
        ).all()
    )
    _backfill_report_artifacts(session, stats, now, existing_keys)
    _backfill_set_artifacts(session, stats, now, existing_keys)
    session.flush()
    return stats


async def run_backfill(db: AsyncSession) -> BackfillStats:
    """异步入口：在同一连接上以同步会话执行回填。"""
    return await db.run_sync(run_backfill_sync)


def _backfill_selection_sets(session: Session, stats: BackfillStats, now: datetime) -> None:
    session_ids = session.scalars(select(SessionKolSelection.session_id).distinct()).all()
    for session_id in session_ids:
        selection_set = session.scalars(
            select(KolSelectionSet).where(
                KolSelectionSet.session_id == session_id,
                KolSelectionSet.title == LEGACY_SET_TITLE,
            )
        ).first()
        if selection_set is None:
            selection_set = KolSelectionSet(
                id=str(uuid4()),
                session_id=session_id,
                task_id=None,
                goal_id=None,
                version=1,
                title=LEGACY_SET_TITLE,
                scope_json=None,
                status="completed",
                created_at=now,
                updated_at=now,
            )
            session.add(selection_set)
            session.flush()
            stats.selection_sets_created += 1
        existing_items = set(
            session.execute(
                select(KolSelectionItem.platform, KolSelectionItem.kol_uid).where(
                    KolSelectionItem.selection_set_id == selection_set.id
                )
            ).all()
        )
        legacy_rows = session.scalars(
            select(SessionKolSelection).where(SessionKolSelection.session_id == session_id)
        ).all()
        for row in legacy_rows:
            if (row.platform, row.kol_uid) in existing_items:
                stats.selection_items_skipped += 1
                continue
            session.add(
                KolSelectionItem(
                    id=str(uuid4()),
                    user_id=row.user_id,
                    selection_set_id=selection_set.id,
                    platform=row.platform,
                    kol_uid=row.kol_uid,
                    nickname=row.nickname,
                    followers=row.followers,
                    city=row.city,
                    profile_url=row.profile_url,
                    fields_json=row.fields_json,
                    score_json=row.score_json,
                    source_tool=row.source_tool,
                    first_task_id=row.first_task_id,
                    last_task_id=row.last_task_id,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
            )
            stats.selection_items_created += 1
        session.flush()


def _backfill_report_artifacts(
    session: Session, stats: BackfillStats, now: datetime, existing_keys: set[str]
) -> None:
    reports = session.scalars(select(AnalysisReport)).all()
    for report in reports:
        artifact_key = f"{LEGACY_ARTIFACT_KEY_PREFIX}{report.id}:kol_report"
        if artifact_key in existing_keys:
            stats.report_artifacts_skipped += 1
            continue
        session.add(
            TaskArtifact(
                id=str(uuid4()),
                session_id=report.session_id,
                task_id=report.task_id,
                goal_id=None,
                artifact_key=artifact_key,
                artifact_type="kol_report",
                title=report.title,
                version=report.version,
                status=report.status,
                report_id=report.id,
                selection_set_id=None,
                scope_json=None,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
        )
        existing_keys.add(artifact_key)
        stats.report_artifacts_created += 1
    session.flush()


def _backfill_set_artifacts(
    session: Session, stats: BackfillStats, now: datetime, existing_keys: set[str]
) -> None:
    legacy_sets = session.scalars(
        select(KolSelectionSet).where(KolSelectionSet.title == LEGACY_SET_TITLE)
    ).all()
    for selection_set in legacy_sets:
        artifact_key = f"{LEGACY_ARTIFACT_KEY_PREFIX}{selection_set.id}:kol_selection_set"
        if artifact_key in existing_keys:
            stats.set_artifacts_skipped += 1
            continue
        session.add(
            TaskArtifact(
                id=str(uuid4()),
                session_id=selection_set.session_id,
                task_id=selection_set.task_id,
                goal_id=None,
                artifact_key=artifact_key,
                artifact_type="kol_selection_set",
                title=selection_set.title,
                version=selection_set.version,
                status="completed",
                report_id=None,
                selection_set_id=selection_set.id,
                scope_json=None,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
        )
        existing_keys.add(artifact_key)
        stats.set_artifacts_created += 1
    session.flush()
