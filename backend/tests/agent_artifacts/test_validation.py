"""Artifact 强类型 payload 校验边界测试（v3 加固 §2.4/§2.5/§5.6 / A5）。

覆盖：
1. ``ArtifactPayloadValidator``：schema_version 映射唯一 Pydantic 类型；
   module/schema_version/artifact_type 固定组合；key 所需 business fields
   非空（拒绝裸 key）；标准化 ``model_dump(mode="json")``；
2. §2.5 反向聚合：必需章节必须在 availability 中；complete 当且仅当全部
   必需章节 complete；restricted 当且仅当至少一个必需章节 partial/
   unavailable 且有覆盖 limitation；
3. 服务接入：create/update Draft 校验并保存标准化形态，失败抛
   ``ArtifactPayloadInvalid`` 且不落任何行；工具层映射为结构化
   ``artifact_payload_invalid`` ToolResult（不是异常/500）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.agent_artifacts.models import AgentArtifact, ArtifactDraft, ArtifactDraftRevision
from app.agent_artifacts.payloads import BrandReportV3
from app.agent_artifacts.service import ArtifactPayloadInvalid, ArtifactService
from app.agent_artifacts.validation import ArtifactPayloadValidator
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.tools.artifacts import CreateDraftTool, UpdateDraftTool
from app.agent_runtime.tools.contracts import ToolContext
from app.agent_runtime.tools.registry import ToolRegistry

from tests.agent_artifacts.payload_fixtures import brand_payload, insight_payload


def _restricted_brand_payload() -> dict:
    """overview（必需章节）partial + 覆盖 limitation 的合法 restricted brand。"""
    payload = brand_payload()
    payload["data_status"] = "restricted"
    payload["availability"]["overview"] = {"status": "partial", "reason_codes": ["data_partial"]}
    payload["limitations"] = [
        {
            "code": "L_VOLUME",
            "message": "声量数据部分缺失",
            "affected_paths": ["data.overview"],
        }
    ]
    return payload


# --------------------------------------------------------------------------- #
# Validator：固定组合 / business fields / 标准化
# --------------------------------------------------------------------------- #


def test_validator_accepts_valid_payload_and_normalizes() -> None:
    normalized = ArtifactPayloadValidator.validate_new_draft(
        module="insight",
        schema_version="insight_board_v1",
        artifact_type="insight_board_v1",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "为什么涨"},
        payload=insight_payload(),
    )
    # 标准化 model_dump(mode="json")：默认值填充、tuple→list、JSON 安全。
    assert normalized["schema_version"] == "insight_board_v1"
    assert normalized["scope"]["platforms"] == []
    assert normalized["scope"]["brand"] is None
    assert isinstance(normalized["data"], list)
    assert normalized["data_status"] == "complete"


def test_validator_rejects_unknown_module() -> None:
    with pytest.raises(ArtifactPayloadInvalid) as excinfo:
        ArtifactPayloadValidator.validate_new_draft(
            module="unknown-module",
            schema_version="insight_board_v1",
            artifact_type="insight_board_v1",
            business_fields={},
            payload=insight_payload(),
        )
    assert excinfo.value.code == "artifact_payload_invalid"


def test_validator_rejects_module_schema_mismatch() -> None:
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module="brand",
            schema_version="kol_selection_v3",
            artifact_type="kol_selection_v3",
            business_fields={"brand": "瑞幸"},
            payload=brand_payload(),
        )


def test_validator_rejects_artifact_type_schema_mismatch() -> None:
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module="brand",
            schema_version="brand_report_v3",
            artifact_type="brand_report_v2",
            business_fields={"brand": "瑞幸"},
            payload=brand_payload(),
        )


@pytest.mark.parametrize(
    ("module", "business_fields"),
    [
        ("brand", {}),
        ("brand", {"brand": "   "}),
        ("brand", {"brand": None}),
        ("campaign", {"brand": "瑞幸"}),
        ("campaign", {"brand": "瑞幸", "campaign": ""}),
        ("kol-selection", {"scope": None}),
        ("kol-selection", {"scope": {}}),
        ("kol-analysis", {"selection_artifact_id": ""}),
        ("kol-detail", {"platform": "xiaohongshu"}),
        ("kol-detail", {"platform": "", "kol_uid": "u1"}),
        ("insight", {"parent_artifact_version_id": "pv-1"}),
        ("insight", {"parent_artifact_version_id": "pv-1", "question": " "}),
    ],
)
def test_validator_rejects_naked_key_business_fields(module: str, business_fields: dict) -> None:
    """key 所需 business fields 为空必须拒绝——不允许 ``brand:`` 这类裸 key 落库。"""
    from app.agent_artifacts.validation import SCHEMA_VERSION_BY_MODULE

    schema = SCHEMA_VERSION_BY_MODULE[module]
    with pytest.raises(ArtifactPayloadInvalid) as excinfo:
        ArtifactPayloadValidator.validate_new_draft(
            module=module,
            schema_version=schema,
            artifact_type=schema,
            business_fields=business_fields,
            payload={},
        )
    assert excinfo.value.code == "artifact_payload_invalid"


def test_validator_rejects_invalid_payload_with_details() -> None:
    bad = insight_payload()
    bad["data"] = [{"block_type": "markdown", "title": "x"}]  # content 缺失
    with pytest.raises(ArtifactPayloadInvalid) as excinfo:
        ArtifactPayloadValidator.validate_new_draft(
            module="insight",
            schema_version="insight_board_v1",
            artifact_type="insight_board_v1",
            business_fields={"parent_artifact_version_id": "pv-1", "question": "为什么"},
            payload=bad,
        )
    assert excinfo.value.errors  # 结构化错误明细，供回喂模型


# --------------------------------------------------------------------------- #
# §2.5 反向聚合（经 payload 类型本身生效，Validator 复用）
# --------------------------------------------------------------------------- #


def test_restricted_requires_at_least_one_restricted_required_section() -> None:
    payload = brand_payload()
    payload["data_status"] = "restricted"
    payload["limitations"] = [
        {"code": "L", "message": "受限", "affected_paths": ["data.overview"]}
    ]
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module="brand",
            schema_version="brand_report_v3",
            artifact_type="brand_report_v3",
            business_fields={"brand": "瑞幸"},
            payload=payload,
        )


def test_required_sections_must_exist_in_availability() -> None:
    payload = brand_payload()
    del payload["availability"]["topics"]  # 必需章节缺失
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module="brand",
            schema_version="brand_report_v3",
            artifact_type="brand_report_v3",
            business_fields={"brand": "瑞幸"},
            payload=payload,
        )


def test_restricted_requires_covering_limitation() -> None:
    payload = _restricted_brand_payload()
    payload["limitations"] = [
        {"code": "L", "message": "只覆盖话题", "affected_paths": ["data.topics"]}
    ]
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module="brand",
            schema_version="brand_report_v3",
            artifact_type="brand_report_v3",
            business_fields={"brand": "瑞幸"},
            payload=payload,
        )


def test_restricted_with_partial_required_section_and_limitation_accepted() -> None:
    normalized = ArtifactPayloadValidator.validate_new_draft(
        module="brand",
        schema_version="brand_report_v3",
        artifact_type="brand_report_v3",
        business_fields={"brand": "瑞幸"},
        payload=_restricted_brand_payload(),
    )
    assert normalized["data_status"] == "restricted"


def test_complete_rejects_partial_required_section() -> None:
    payload = brand_payload()
    payload["availability"]["topics"] = {"status": "partial", "reason_codes": ["x"]}
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module="brand",
            schema_version="brand_report_v3",
            artifact_type="brand_report_v3",
            business_fields={"brand": "瑞幸"},
            payload=payload,
        )


def test_payload_types_enforce_reverse_aggregation_directly() -> None:
    """反向聚合是 payload 契约本身：直接 model_validate 同样拒绝。"""
    from pydantic import ValidationError

    payload = brand_payload()
    payload["data_status"] = "restricted"
    payload["limitations"] = [{"code": "L", "message": "受限", "affected_paths": []}]
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(payload)


# --------------------------------------------------------------------------- #
# 服务接入：create/update Draft 校验 + 标准化存储
# --------------------------------------------------------------------------- #


async def _setup(db_session, user_factory, session_factory, run_factory):
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    return user, session, run, ArtifactService(db_session)


async def test_create_draft_stores_normalized_payload(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user, session, run, service = await _setup(
        db_session, user_factory, session_factory, run_factory
    )
    raw = insight_payload()
    raw["scope"] = {"summary": "钻取"}  # 未填默认值：标准化后应补齐
    _, _, revision = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "为什么"},
        schema_version="insight_board_v1",
        payload=raw,
        evidence_refs=[],
        artifact_type="insight_board_v1",
    )
    assert revision.payload_json == insight_payload()
    assert revision.payload_json["scope"]["platforms"] == []


async def test_create_draft_rejects_invalid_payload_and_writes_nothing(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user, session, run, service = await _setup(
        db_session, user_factory, session_factory, run_factory
    )
    with pytest.raises(ArtifactPayloadInvalid) as excinfo:
        await service.create_or_get_draft(
            session_id=session.id,
            user_id=user.id,
            run_id=run.id,
            module="brand",
            business_fields={"brand": "瑞幸"},
            schema_version="brand_report_v3",
            payload={"data": {"overview": {"total_volume": 100}}},
            evidence_refs=[],
            artifact_type="brand_report_v3",
        )
    assert excinfo.value.code == "artifact_payload_invalid"
    # 不落任何行
    assert (await db_session.scalar(select(func.count(AgentArtifact.id)))) == 0
    assert (await db_session.scalar(select(func.count(ArtifactDraft.id)))) == 0
    assert (await db_session.scalar(select(func.count(ArtifactDraftRevision.id)))) == 0


async def test_create_draft_rejects_naked_brand_key(
    db_session, user_factory, session_factory, run_factory
) -> None:
    """brand 为空不得生成 ``brand:`` 裸 key（§2.4）。"""
    user, session, run, service = await _setup(
        db_session, user_factory, session_factory, run_factory
    )
    with pytest.raises(ArtifactPayloadInvalid):
        await service.create_or_get_draft(
            session_id=session.id,
            user_id=user.id,
            run_id=run.id,
            module="brand",
            business_fields={"brand": "  "},
            schema_version="brand_report_v3",
            payload=brand_payload(),
            evidence_refs=[],
            artifact_type="brand_report_v3",
        )


async def test_update_draft_validates_and_normalizes(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user, session, run, service = await _setup(
        db_session, user_factory, session_factory, run_factory
    )
    _, draft, _ = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "为什么"},
        schema_version="insight_board_v1",
        payload=insight_payload(),
        evidence_refs=[],
        artifact_type="insight_board_v1",
    )
    with pytest.raises(ArtifactPayloadInvalid):
        await service.update_draft(
            run_id=run.id,
            draft_id=draft.id,
            payload={"data": [{"block_type": "markdown", "title": "x"}]},
            evidence_refs=[],
        )
    # 合法更新成功并保存标准化形态
    _, revision = await service.update_draft(
        run_id=run.id,
        draft_id=draft.id,
        payload=insight_payload(title="修订"),
        evidence_refs=[],
    )
    assert revision.payload_json["title"] == "修订"
    assert revision.revision == 2


# --------------------------------------------------------------------------- #
# 工具层：结构化回喂 artifact_payload_invalid，不抛异常
# --------------------------------------------------------------------------- #


async def test_create_draft_tool_returns_structured_invalid_result(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user, session, run, _ = await _setup(
        db_session, user_factory, session_factory, run_factory
    )
    registry = ToolRegistry()
    registry.register(CreateDraftTool(db_session), category="artifact")
    # H2/H5 起六类强类型 schema 直写都在更前置的 typed_artifact_requires_builder
    # 护栏被拦（见 tests/agent_runtime/tools/test_artifacts.py）；本用例用未注册
    # 的 module 继续覆盖 ArtifactPayloadInvalid → 结构化回喂路径。
    result = await registry.execute(
        internal_name="create_draft",
        arguments={
            "module": "legacy_freeform",
            "schema_version": "legacy_freeform_v1",
            "artifact_type": "legacy_freeform_v1",
            "business_fields": {},
            "payload": {"bogus": True},
            "evidence_refs": [],
        },
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile=get_profile("session_analyst_v1"),
    )
    assert result.status == "failed"
    assert result.error_type == "artifact_payload_invalid"


async def test_update_draft_tool_returns_structured_invalid_result(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user, session, run, _service = await _setup(
        db_session, user_factory, session_factory, run_factory
    )
    # H5 起 insight 等六类强类型 Draft 的 update_draft 直写被前置护栏拦截；
    # 用未注册 module 的 ORM 行继续覆盖 ArtifactPayloadInvalid → 结构化回喂路径。
    from datetime import UTC, datetime
    from uuid import uuid4

    now = datetime.now(UTC).replace(tzinfo=None)
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        module="legacy_freeform",
        artifact_type="legacy_freeform_v1",
        artifact_key=f"legacy-{uuid4()}",
        status="draft",
        latest_version=0,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    draft = ArtifactDraft(
        id=str(uuid4()),
        artifact_id=artifact.id,
        session_id=session.id,
        owner_run_id=run.id,
        current_revision=0,
        status="drafting",
        review_count=0,
        revision_count=0,
        updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()

    tool = UpdateDraftTool(db_session)
    result = await tool.execute(
        ToolContext(
            user_id=user.id,
            session_id=session.id,
            run_id=run.id,
            profile_name="session_analyst_v1",
        ),
        {"draft_id": draft.id, "payload": {"bogus": True}, "evidence_refs": []},
    )
    assert result.status == "failed"
    assert result.error_type == "artifact_payload_invalid"
