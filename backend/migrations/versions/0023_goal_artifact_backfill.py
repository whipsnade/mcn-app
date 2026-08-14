"""Backfill legacy kol selections/reports into goal/artifact tables.

自包含实现：回填逻辑内联于此迁移（Task 24 移除 app.artifacts.backfill 执行源后，
迁移仍需可独立运行）。逻辑与旧 app.artifacts.backfill.run_backfill_sync 一致。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from alembic import op
from sqlalchemy import select, update
from sqlalchemy.orm import Session, defer

from app.artifacts.models import TaskArtifact
from app.reporting.models import AnalysisReport
from app.selection.models import KolSelectionItem, KolSelectionSet, SessionKolSelection


# 注：revision 需 ≤32 字符（alembic_version.version_num 为 VARCHAR(32)）。
revision: str = "0023_goal_artifact_backfill"
down_revision: str | None = "0022_goal_artifact_infra"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# 历史默认名单固定标题：既是回填幂等键，也是 downgrade 的删除依据。
LEGACY_SET_TITLE = "历史默认名单"
LEGACY_ARTIFACT_KEY_PREFIX = "legacy:"


@dataclass
class _BackfillStats:
    """回填执行统计（创建/跳过的各类行数）。"""

    report_type_updated: int = 0
    selection_sets_created: int = 0
    selection_items_created: int = 0
    selection_items_skipped: int = 0
    report_artifacts_created: int = 0
    report_artifacts_skipped: int = 0
    set_artifacts_created: int = 0
    set_artifacts_skipped: int = 0


def _backfill_selection_sets(session: Session, stats: _BackfillStats, now: datetime) -> None:
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
    session: Session, stats: _BackfillStats, now: datetime, existing_keys: set[str]
) -> None:
    # payload_json/template_version 由迁移 0026 新增：本回填随 0023 执行时两列尚不存在，
    # defer 使其不进 SELECT 字段列表，保证旧库迁移链路可用；回填本身不读这些列。
    reports = session.scalars(
        select(AnalysisReport).options(
            defer(AnalysisReport.payload_json),
            defer(AnalysisReport.template_version),
        )
    ).all()
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
    session: Session, stats: _BackfillStats, now: datetime, existing_keys: set[str]
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


def run_backfill_sync(session: Session) -> _BackfillStats:
    """同步回填实现（幂等，可重复执行）。"""
    stats = _BackfillStats()
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


def upgrade() -> None:
    with Session(bind=op.get_bind()) as session:
        run_backfill_sync(session)
        session.commit()


def downgrade() -> None:
    # report_type 的兜底 UPDATE 不回滚（列本身保留默认值语义）。
    op.execute("DELETE FROM task_artifacts WHERE artifact_key LIKE 'legacy:%'")
    op.execute(
        "DELETE FROM kol_selection_items WHERE selection_set_id IN "
        f"(SELECT id FROM kol_selection_sets WHERE title = '{LEGACY_SET_TITLE}')"
    )
    op.execute(f"DELETE FROM kol_selection_sets WHERE title = '{LEGACY_SET_TITLE}'")
