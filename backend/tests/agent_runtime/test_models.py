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


def test_confirmed_scope_is_valid_memory_type() -> None:
    from app.agent_runtime.models import MemoryEntry

    entry = MemoryEntry(memory_type="confirmed_scope", content_json={"domain": "brand"})
    assert entry.memory_type == "confirmed_scope"
    checks = {
        constraint.name: constraint.sqltext.text
        for constraint in MemoryEntry.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "confirmed_scope" in checks["ck_memory_entries_type"]


def test_agent_uploads_table_is_registered() -> None:
    from app.agent_runtime.models import AgentUpload

    table = AgentUpload.__table__
    assert table.name == "agent_uploads"
    required = {
        "id",
        "user_id",
        "session_id",
        "run_id",
        "original_filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "storage_key",
        "status",
        "error_code",
        "created_at",
        "completed_at",
    }
    assert required.issubset(set(table.c.keys()))
    assert table.c.run_id.nullable is True
    assert table.c.error_code.nullable is True
    assert table.c.completed_at.nullable is True
    assert table.c.status.nullable is False


def test_evidence_items_upload_id_xor_tool_call_id() -> None:
    from app.agent_runtime.models import EvidenceItem

    table = EvidenceItem.__table__
    assert "upload_id" in table.c
    assert "normalization_version" in table.c
    assert "normalization_status" in table.c
    assert "field_mapping_json" in table.c
    assert "unmapped_fields_json" in table.c
    assert "truncated" in table.c
    assert "normalization_error_code" in table.c
    assert table.c.tool_call_id.nullable is True
    assert table.c.upload_id.nullable is True

    xor_checks = [
        constraint.sqltext.text
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_evidence_items_tool_call_xor_upload"
    ]
    assert xor_checks, "missing ck_evidence_items_tool_call_xor_upload"
    xor_text = xor_checks[0]
    assert "tool_call_id" in xor_text and "upload_id" in xor_text and "NULL" in xor_text

    assert any(
        tuple(column.name for column in index.columns) == ("session_id", "collected_at")
        for index in table.indexes
    )
    assert any(
        tuple(column.name for column in index.columns) == ("upload_id",)
        for index in table.indexes
    )
