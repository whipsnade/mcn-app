from sqlalchemy import CheckConstraint, UniqueConstraint

from app.db.base import Base
import app.agent_runtime.models  # noqa: F401
import app.agent_artifacts.models  # noqa: F401


def test_agent_runtime_tables_are_registered() -> None:
    expected = {
        "agent_sessions",
        "agent_messages",
        "agent_runs",
        "agent_run_attempts",
        "agent_steps",
        "agent_tool_calls",
        "evidence_items",
        "agent_events",
        "agent_tool_call_reconciliations",
        "memory_entries",
        "agent_artifacts",
        "artifact_drafts",
        "artifact_draft_revisions",
        "artifact_review_batches",
        "artifact_review_items",
        "artifact_review_attempts",
        "agent_artifact_versions",
        "artifact_events",
        "agent_artifact_read_states",
        "kol_detail_cache",
    }
    assert expected.issubset(Base.metadata.tables)


def test_agent_runs_kind_and_visibility_check_constraints() -> None:
    runs = Base.metadata.tables["agent_runs"]
    checks = {
        constraint.name: constraint.sqltext.text
        for constraint in runs.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["ck_agent_runs_kind"] == "run_kind IN ('user','internal')"
    assert checks["ck_agent_runs_visibility"] == "visibility IN ('user','internal')"


def test_agent_runs_cancel_requested_column_is_not_nullable() -> None:
    runs = Base.metadata.tables["agent_runs"]
    assert "cancel_requested" in runs.c
    assert runs.c.cancel_requested.nullable is False


def test_agent_runs_status_lease_index() -> None:
    runs = Base.metadata.tables["agent_runs"]
    assert any(
        index.name == "ix_agent_runs_status_lease"
        and tuple(column.name for column in index.columns) == ("status", "lease_expires_at")
        for index in runs.indexes
    )


def test_agent_steps_run_sequence_unique() -> None:
    steps = Base.metadata.tables["agent_steps"]
    assert any(
        tuple(column.name for column in constraint.columns) == ("run_id", "sequence")
        for constraint in steps.constraints
        if isinstance(constraint, UniqueConstraint)
    )


def test_agent_events_run_sequence_unique() -> None:
    events = Base.metadata.tables["agent_events"]
    assert any(
        tuple(column.name for column in constraint.columns) == ("run_id", "sequence")
        for constraint in events.constraints
        if isinstance(constraint, UniqueConstraint)
    )


def test_agent_tool_calls_step_id_not_null() -> None:
    tool_calls = Base.metadata.tables["agent_tool_calls"]
    assert tool_calls.c.step_id.nullable is False


def test_agent_tool_calls_logical_call_id_unique() -> None:
    tool_calls = Base.metadata.tables["agent_tool_calls"]
    assert any(
        tuple(column.name for column in constraint.columns) == ("logical_call_id",)
        for constraint in tool_calls.constraints
        if isinstance(constraint, UniqueConstraint)
    )
