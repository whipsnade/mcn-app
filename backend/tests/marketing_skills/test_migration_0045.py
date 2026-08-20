from __future__ import annotations

import importlib


def test_marketing_skill_registry_migration_is_additive_and_follows_0044() -> None:
    migration = importlib.import_module("migrations.versions.0045_marketing_skill_registry")

    assert migration.revision == "0045_marketing_skill_registry"
    assert migration.down_revision == "0044_agent_run_loop_guard"
    assert "skill_revisions" in migration.__doc__
    assert "skill_activations" in migration.__doc__
