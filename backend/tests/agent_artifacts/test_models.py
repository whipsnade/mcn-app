from sqlalchemy import UniqueConstraint

from app.db.base import Base
import app.agent_runtime.models  # noqa: F401
import app.agent_artifacts.models  # noqa: F401


def test_artifact_tables_are_registered() -> None:
    expected = {
        "agent_artifacts",
        "artifact_drafts",
        "artifact_draft_revisions",
        "artifact_review_batches",
        "artifact_review_items",
        "artifact_review_attempts",
        "agent_artifact_versions",
        "artifact_events",
        "artifact_read_states",
        "kol_detail_cache",
    }
    assert expected.issubset(Base.metadata.tables)


def test_artifact_drafts_unique_artifact_id() -> None:
    drafts = Base.metadata.tables["artifact_drafts"]
    assert any(
        tuple(column.name for column in constraint.columns) == ("artifact_id",)
        for constraint in drafts.constraints
        if isinstance(constraint, UniqueConstraint)
    )


def test_artifact_review_attempts_unique_review_item_attempt() -> None:
    attempts = Base.metadata.tables["artifact_review_attempts"]
    assert any(
        tuple(column.name for column in constraint.columns) == ("review_item_id", "attempt")
        for constraint in attempts.constraints
        if isinstance(constraint, UniqueConstraint)
    )


def test_artifact_events_session_sequence_unique() -> None:
    events = Base.metadata.tables["artifact_events"]
    assert any(
        tuple(column.name for column in constraint.columns) == ("session_id", "sequence")
        for constraint in events.constraints
        if isinstance(constraint, UniqueConstraint)
    )


def test_artifact_read_states_has_new_schema_columns_and_unique() -> None:
    states = Base.metadata.tables["artifact_read_states"]
    assert "module" in states.c
    assert "last_seen_sequence" in states.c
    assert "updated_at" in states.c
    assert any(
        tuple(column.name for column in constraint.columns)
        == ("user_id", "session_id", "module")
        for constraint in states.constraints
        if isinstance(constraint, UniqueConstraint)
    )
