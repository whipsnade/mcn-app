"""LoadMarketingSkillTool 契约增强测试（提交 2：把精确模型输入契约交给模型）。

覆盖：
- 已注册 artifact_contract 的 skill 返回 ``model_input_contract``，且
  ``model_input_schema`` 与 DTO 类 ``model_json_schema()`` 完全相等
  （single-source drift test，不手写第二份 schema）；
- ``concise_example`` 是合法模型输入且不含服务器字段；
- 返回内容不泄漏敏感键（secret/token/api_key/dsn/password，递归检查）；
- 未启用 skill → ``marketing_skill_not_enabled``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.model_inputs import (
    MODEL_INPUT_BY_ARTIFACT_TYPE,
    SERVER_OWNED_PAYLOAD_KEYS,
)
from app.agent_artifacts.model_inputs.brand import BrandReportV3Input
from app.agent_runtime.models import AgentRun, AgentSession
from app.agent_runtime.tools.contracts import ToolContext
from app.agent_runtime.tools.pi_internal_tools import LoadMarketingSkillTool
from app.marketing_capability_pack.runtime import build_marketing_run_capability

#: 返回内容中禁止出现的敏感键（大小写不敏感递归检查）。
_SENSITIVE_KEYS = ("secret", "token", "api_key", "dsn", "password")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sensitive(value, path: str = "") -> str | None:
    """递归扫描 dict/list 键与字符串值，命中敏感特征返回路径。"""
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(needle in lowered for needle in _SENSITIVE_KEYS):
                return f"{path}/{key}"
            hit = _sensitive(item, f"{path}/{key}")
            if hit is not None:
                return hit
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hit = _sensitive(item, f"{path}/{index}")
            if hit is not None:
                return hit
    elif isinstance(value, str):
        lowered = value.lower()
        for needle in _SENSITIVE_KEYS:
            if needle in lowered:
                return f"{path} (value contains {needle!r})"
    return None


async def _seed_run(
    db_session: AsyncSession, user_id: str, session_id: str, *, skill_name: str
) -> AgentRun:
    now = _now()
    capability = build_marketing_run_capability().model_dump(mode="json")
    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        runtime_backend="pi",
        status="running",
        runtime_config_snapshot_json={"capability_pack": capability},
        created_at=now,
        started_at=now,
        run_kind="user",
    )
    db_session.add(run)
    await db_session.flush()
    return run


async def _make_session(db_session: AsyncSession, user_id: str) -> AgentSession:
    now = _now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user_id,
        title="pi internal tools",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    return session


def _ctx(user_id: str, session_id: str, run_id: str) -> ToolContext:
    return ToolContext(
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        profile_name="session_analyst_v1",
    )


@pytest.mark.asyncio
async def test_load_marketing_skill_includes_exact_model_input_schema(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _seed_run(db_session, user.id, session.id, skill_name="brand-research-report")

    result = await LoadMarketingSkillTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        {"skill_name": "brand-research-report"},
    )

    assert result.status == "success"
    payload = json.loads(result.safe_summary)
    assert payload["artifact_contract"] == "brand_report_v3"
    contract = payload["model_input_contract"]
    assert contract["artifact_type"] == "brand_report_v3"
    assert contract["input_schema_version"] == "direct_model_input_v1"
    # 单一事实源：schema 完全来自 DTO，不手写第二份。
    assert contract["model_input_schema"] == BrandReportV3Input.model_json_schema()
    assert contract["required_tools"] == ["build_artifact_draft", "publish_artifacts"]


@pytest.mark.asyncio
async def test_load_marketing_skill_concise_example_is_valid_model_input(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _seed_run(db_session, user.id, session.id, skill_name="brand-research-report")

    result = await LoadMarketingSkillTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        {"skill_name": "brand-research-report"},
    )

    payload = json.loads(result.safe_summary)
    example = payload["model_input_contract"]["concise_example"]
    BrandReportV3Input.model_validate(example)
    assert not (set(example) & SERVER_OWNED_PAYLOAD_KEYS)


@pytest.mark.asyncio
async def test_load_marketing_skill_does_not_leak_sensitive_or_identity(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _seed_run(db_session, user.id, session.id, skill_name="brand-research-report")

    result = await LoadMarketingSkillTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        {"skill_name": "brand-research-report"},
    )

    assert result.status == "success"
    hit = _sensitive(json.loads(result.safe_summary))
    assert hit is None, f"敏感特征泄漏于返回内容: {hit}"
    # 数据库内部身份（run/session/user id）不得出现在返回中。
    rendered = result.safe_summary
    assert run.id not in rendered
    assert session.id not in rendered
    assert user.id not in rendered


@pytest.mark.asyncio
async def test_load_marketing_skill_rejects_disabled_skill(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _seed_run(db_session, user.id, session.id, skill_name="no-such-skill")

    result = await LoadMarketingSkillTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        {"skill_name": "no-such-skill"},
    )

    assert result.status == "failed"
    assert result.error_type == "marketing_skill_not_enabled"


@pytest.mark.parametrize(
    ("skill_name", "artifact_type"),
    [
        ("campaign-evaluation-report", "campaign_report_v3"),
        ("kol-selection-report", "kol_selection_v3"),
        ("artifact-drilldown", "insight_board_v1"),
    ],
)
@pytest.mark.asyncio
async def test_load_marketing_skill_contract_for_each_registered_artifact_type(
    db_session, user_factory, skill_name: str, artifact_type: str
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _seed_run(db_session, user.id, session.id, skill_name=skill_name)

    result = await LoadMarketingSkillTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        {"skill_name": skill_name},
    )

    assert result.status == "success"
    payload = json.loads(result.safe_summary)
    contract = payload["model_input_contract"]
    dto = MODEL_INPUT_BY_ARTIFACT_TYPE[artifact_type]
    assert contract["model_input_schema"] == dto.model_json_schema()
    example = contract["concise_example"]
    dto.model_validate(example)
    assert not (set(example) & SERVER_OWNED_PAYLOAD_KEYS)
