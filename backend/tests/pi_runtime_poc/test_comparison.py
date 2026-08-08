"""Pi-only POC Harness 的本地数据契约测试。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

import app.db.models  # 注册所有 FK 目标供 SQLite POC fixture 建表
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
)
from app.agent_runtime.models import AgentMessage, AgentRun
from app.billing.models import Wallet
from app.core.config import Settings
from app.db.base import Base
from app.identity.models import UserChannelPermission
from app.identity.service import IdentityService
from app.pi_runtime_poc.comparison import (
    PiRuntimeCaseExecutor,
    PocCase,
    PocCaseFactory,
    PocCaseResult,
    _is_datatap_audit_service,
    begin_round,
    load_cases,
    write_append_only_round,
    write_case_result,
    write_round_summary,
)
from app.pi_runtime_poc.server import app


@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext_sqlite(element, compiler, **kwargs) -> str:
    return "TEXT"


@pytest_asyncio.fixture
async def db_session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _result(
    case_id: str,
    *,
    status: str = "completed",
    outcome: str | None = "completed",
    artifacts: tuple[str, ...] = (),
    error_code: str | None = None,
    hard_checks: dict[str, bool] | None = None,
) -> PocCaseResult:
    return PocCaseResult(
        case_id=case_id,
        runtime="pi",
        run_id=f"pi-{case_id}" if status != "skipped_dependency" else None,
        status=status,  # type: ignore[arg-type]
        error_code=error_code,
        outcome=outcome,
        artifact_versions=artifacts,
        evidence_ids=(),
        metrics={"datatap_tool_calls": 0, "points_reserved": 0, "points_settled": 0},
        diagnostic_path=f"outputs/{case_id}/pi.json",
        hard_checks=hard_checks or {},
    )


def test_load_cases_contains_six_versioned_scenarios_without_provider_output_or_secrets() -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "pi_runtime_poc" / "cases.json"

    cases = load_cases(fixture)

    assert len(cases) == 6
    assert {case.case_id for case in cases} == {
        "brand-research-v1",
        "campaign-evaluation-v1",
        "kol-selection-v1",
        "artifact-drilldown-v1",
        "scope-clarification-v1",
        "non-marketing-v1",
    }
    assert all(case.date_anchor and case.expected_behavior for case in cases)
    assert "sk-" not in fixture.read_text(encoding="utf-8")
    assert "raw_payload" not in fixture.read_text(encoding="utf-8")


async def test_case_factory_creates_pi_run_without_wallet(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case = PocCase("brand-research-v1", "q", "2026-08-01", "report", "brand_report_v3")
    factory = PocCaseFactory(
        db_session_factory,
        round_id="pi-only",
        model_name="deepseek-v4-pro",
    )

    run_id = await factory.create(case)

    async with db_session_factory() as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        assert run.prompt_snapshot_json["pi_runtime_poc"]["runtime"] == "pi"
        assert run.prompt_snapshot_json["pi_runtime_poc"]["billing_mode"] == "disabled"
        assert await db.get(Wallet, run.user_id) is None
        channels = set(
            (
                await db.scalars(
                    select(UserChannelPermission.channel).where(
                        UserChannelPermission.user_id == run.user_id
                    )
                )
            ).all()
        )
    assert channels == set(IdentityService.default_channels)


def test_comparison_module_exports_only_pi_executor() -> None:
    import app.pi_runtime_poc.comparison as module

    assert PiRuntimeCaseExecutor is module.PiRuntimeCaseExecutor
    assert "PiRuntimeCaseExecutor" in module.__all__
    assert "CurrentRuntimeCaseExecutor" not in module.__all__


def test_round_output_is_append_only_and_redacts_secret_like_strings(tmp_path: Path) -> None:
    result = _result("brand-research-v1")

    summary_path = write_append_only_round(tmp_path, "round-001", (result,))

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["results"][0]["runtime"] == "pi"
    with pytest.raises(FileExistsError):
        write_append_only_round(tmp_path, "round-001", (result,))
    leaking = PocCaseResult(**{**result.__dict__, "diagnostic_path": "sk-should-not-write"})
    with pytest.raises(ValueError, match="poc_output_contains_secret"):
        write_append_only_round(tmp_path, "round-002", (leaking,))


def test_started_round_writes_one_pi_case_then_one_summary(tmp_path: Path) -> None:
    result = _result("brand-research-v1")
    round_dir = begin_round(tmp_path, "round-003")

    case_path = write_case_result(round_dir, result)
    summary_path = write_round_summary(round_dir, (result,), {"gate": "BLOCKED"})

    assert case_path == round_dir / "brand-research-v1" / "pi.json"
    assert case_path.exists()
    assert summary_path.exists()
    with pytest.raises(FileExistsError):
        write_case_result(round_dir, result)
    with pytest.raises(FileExistsError):
        begin_round(tmp_path, "round-003")


def test_poc_internal_server_only_exposes_pi_callback_routes_without_main_lifespan() -> None:
    assert app.url_path_for("healthz") == "/healthz"
    assert (
        app.url_path_for("execute_internal_tool", run_id="test-run")
        == "/api/v1/internal/pi-poc/runs/test-run/internal-tools"
    )
    assert not hasattr(app.state, "agent_executor")


def test_settings_accepts_blank_legacy_endpoint_but_requires_explicit_endpoint_mapping() -> None:
    settings = Settings(
        mysql_password=SecretStr("test-only-password"),
        jwt_secret=SecretStr("test-only-jwt-secret-at-least-32-characters"),
        tencent_plan_api_key=SecretStr("test-only-model-key"),
        datatap_mcp_token=SecretStr("test-only-datatap-token"),
        datatap_mcp_url="",
        datatap_mcp_urls={"insight-cube-mcp": "https://datatap.example.test/insight/mcp"},
    )

    assert settings.datatap_mcp_url is None
    assert str(settings.datatap_mcp_urls["insight-cube-mcp"]) == "https://datatap.example.test/insight/mcp"


def test_datatap_call_count_ignores_internal_and_non_datatap_services() -> None:
    assert _is_datatap_audit_service("insight-cube-mcp")
    assert _is_datatap_audit_service("pi_poc_datatap")
    assert not _is_datatap_audit_service("pi_internal_tool")
    assert not _is_datatap_audit_service("artifact_publication")


async def test_case_factory_reuses_dependency_run_session_and_exact_published_version(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    report = PocCase("brand-research-v1", "完整品牌条件", "2026-08-01", "report", "brand_report_v3")
    drilldown = PocCase(
        "artifact-drilldown-v1",
        "解释已发布版本的一项结论。",
        "2026-08-01",
        "drilldown",
        None,
        depends_on_case_id="brand-research-v1",
    )
    factory = PocCaseFactory(db_session_factory, round_id="round-dependency", model_name="deepseek-v4-pro")
    parent_run_id = await factory.create(report)
    now = datetime.now(UTC).replace(tzinfo=None)

    async with db_session_factory() as db:
        parent = await db.get(AgentRun, parent_run_id)
        assert parent is not None
        artifact = AgentArtifact(
            id=str(uuid4()), session_id=parent.session_id, user_id=parent.user_id,
            module="brand", artifact_type="brand_report_v3", artifact_key="task8d-brand",
            status="published", latest_version=1, activity_sequence=1, created_at=now, updated_at=now,
        )
        draft = ArtifactDraft(
            id=str(uuid4()), artifact_id=artifact.id, session_id=parent.session_id,
            owner_run_id=parent_run_id, current_revision=1, status="idle", review_count=0,
            revision_count=0, updated_at=now,
        )
        revision = ArtifactDraftRevision(
            id=str(uuid4()), draft_id=draft.id, artifact_id=artifact.id, run_id=parent_run_id,
            revision=1, schema_version="brand_report_v3", payload_json={}, evidence_refs_json=[],
            payload_hash="0" * 64, created_at=now,
        )
        version = AgentArtifactVersion(
            id=str(uuid4()), artifact_id=artifact.id, version=1, source_run_id=parent_run_id,
            source_draft_revision_id=revision.id, schema_version="brand_report_v3", payload_json={},
            evidence_refs_json=[], lineage_snapshot_json={}, validation_json={"valid": True},
            data_status="complete", created_at=now,
        )
        db.add_all([artifact, draft, revision, version])
        await db.commit()

    drilldown_run_id = await factory.create(drilldown, prior_run_id=parent_run_id)

    async with db_session_factory() as db:
        parent = await db.get(AgentRun, parent_run_id)
        drilldown_run = await db.get(AgentRun, drilldown_run_id)
        assert parent is not None and drilldown_run is not None
        message = await db.get(AgentMessage, drilldown_run.input_message_id)

    assert message is not None
    assert drilldown_run.user_id == parent.user_id
    assert drilldown_run.session_id == parent.session_id
    assert drilldown_run.prompt_snapshot_json["pi_runtime_poc"]["dependency"] == {
        "case_id": "brand-research-v1",
        "artifact_version_ids": [version.id],
    }
