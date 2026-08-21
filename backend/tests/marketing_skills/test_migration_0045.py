from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path


def test_marketing_skill_registry_migration_is_additive_and_follows_0044() -> None:
    migration = importlib.import_module("migrations.versions.0045_marketing_skill_registry")

    assert migration.revision == "0045_marketing_skill_registry"
    assert migration.down_revision == "0044_agent_run_loop_guard"
    assert "skill_revisions" in migration.__doc__
    assert "skill_activations" in migration.__doc__


def test_marketing_skill_registry_embeds_the_audited_baseline_tools_and_bodies() -> None:
    migration = importlib.import_module("migrations.versions.0048_marketing_skill_audited_baseline")

    rows = {row[0]: row for row in migration._BASELINE_SKILLS}
    assert len(rows) == 8
    assert rows["analysis-report"][3] == (
        "load_marketing_skill",
        "build_artifact_draft",
        "publish_artifacts",
    )
    assert "# 通用营销分析报告" in rows["analysis-report"][4]
    assert "fulfillment" in rows["analysis-report"][4]
    assert "read_artifact" in rows["artifact-drilldown"][3]
    assert all(len(row[4]) > 200 for row in rows.values())
    pack_root = Path(__file__).parents[2] / "app/marketing_capability_pack/packs/marketing-v2"
    manifest = json.loads((pack_root / "manifest.json").read_text())
    expected_tools = {item["name"]: tuple(item["required_tools"]) for item in manifest["skills"]}
    for name, _description, _contract, tools, body in migration._BASELINE_SKILLS:
        source = (pack_root / "skills" / name / "SKILL.md").read_text()
        assert body == source
        assert hashlib.sha256(body.encode("utf-8")).hexdigest() == next(
            item["digest"] for item in manifest["skills"] if item["name"] == name
        )
        assert tools == expected_tools[name]
