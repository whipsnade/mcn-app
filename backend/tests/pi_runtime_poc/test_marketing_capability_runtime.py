import json

import pytest

from app.marketing_capability_pack.runtime import (
    MarketingRunCapability,
    build_marketing_run_capability,
    render_marketing_system_prompt,
)
from app.pi_runtime_poc.internal_tools import PI_POC_ALLOWED_TOOLS


def test_marketing_run_capability_forces_full_root_policy_into_system_prompt() -> None:
    capability = build_marketing_run_capability()
    prompt = render_marketing_system_prompt(capability)

    assert capability.root_policy in prompt
    assert "非营销主题必须使用固定范围回复" in prompt
    assert "root_policy" not in capability.enabled_skills


def test_system_prompt_contains_only_root_policy_and_safe_skill_directory() -> None:
    capability = build_marketing_run_capability()
    prompt = render_marketing_system_prompt(capability)
    directory = json.loads(prompt.split("[AVAILABLE_MARKETING_SKILLS]\n", 1)[1])

    assert capability.root_policy in prompt
    assert all(set(item) == {"name", "description", "version", "artifact_contract"} for item in directory)
    assert all(skill.content not in prompt for skill in capability.skills)


def test_capability_loads_only_enabled_skill_and_is_idempotent() -> None:
    capability = build_marketing_run_capability()

    first = capability.load_skill("brand-research-report")
    second = capability.load_skill("brand-research-report", "1.1.0")

    assert first == second
    assert first["name"] == "brand-research-report"
    assert first["content"].startswith("---")
    assert set(first) == {"name", "version", "digest", "content", "required_tools", "artifact_contract"}
    with pytest.raises(ValueError, match="marketing_skill_not_enabled"):
        capability.load_skill("../root-policy")
    with pytest.raises(ValueError, match="marketing_skill_not_enabled"):
        capability.load_skill("brand-research-report", "9.9.9")


def test_capability_snapshot_is_json_safe_and_has_all_version_facts() -> None:
    capability = build_marketing_run_capability()
    snapshot = capability.model_dump()

    assert json.loads(json.dumps(snapshot))["pack_version"] == "1.1.0"
    assert snapshot["builder_versions"]["brand_report_v3"] == "1.1.0"
    assert snapshot["exporter_versions"]["kol_selection_v3"] == "1.1.0"
    assert not {"read", "bash", "edit", "write", "grep", "find", "ls"} & PI_POC_ALLOWED_TOOLS
    assert "load_marketing_skill" in PI_POC_ALLOWED_TOOLS


def test_snapshot_rejects_cross_run_or_tampered_skill_digest() -> None:
    capability = MarketingRunCapability.model_validate(build_marketing_run_capability().model_dump())
    tampered = capability.model_dump()
    tampered["skills"][0]["digest"] = "0" * 64

    with pytest.raises(ValueError, match="marketing_skill_digest_mismatch"):
        MarketingRunCapability.model_validate(tampered)


def test_snapshot_rejects_unsupported_runtime_contract() -> None:
    tampered = build_marketing_run_capability().model_dump()
    tampered["runtime_contract_version"] = "marketing_runtime_v0"

    with pytest.raises(ValueError, match="marketing_runtime_contract_unsupported"):
        MarketingRunCapability.model_validate(tampered)
