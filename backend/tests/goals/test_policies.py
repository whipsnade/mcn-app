import pytest

from app.goals.policies import GoalPolicy, policy_for
from app.model.prompts import (
    AGENT_LOOP_PROMPT,
    BRAND_ANALYSIS_LOOP_PROMPT,
    CAMPAIGN_ANALYSIS_LOOP_PROMPT,
)


def test_kol_selection_policy_injects_contract_and_ingests() -> None:
    policy = policy_for("kol_selection")

    assert isinstance(policy, GoalPolicy)
    assert policy.goal_type == "kol_selection"
    assert policy.inject_export_contract is True
    assert policy.ingest_enabled is True
    assert policy.loop_system_prompt() == AGENT_LOOP_PROMPT.system


def test_brand_analysis_policy_uses_brand_prompt_without_kol_extras() -> None:
    policy = policy_for("brand_analysis")

    assert policy.inject_export_contract is False
    assert policy.ingest_enabled is False
    assert policy.loop_system_prompt() == BRAND_ANALYSIS_LOOP_PROMPT.system


def test_campaign_analysis_policy_uses_campaign_prompt_without_kol_extras() -> None:
    policy = policy_for("campaign_analysis")

    assert policy.inject_export_contract is False
    assert policy.ingest_enabled is False
    assert policy.loop_system_prompt() == CAMPAIGN_ANALYSIS_LOOP_PROMPT.system


def test_policy_for_unknown_goal_type_raises() -> None:
    with pytest.raises(ValueError):
        policy_for("unknown_type")
