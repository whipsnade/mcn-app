"""runtime_config service 对 v2 manifest 合同版本映射的导入与执行回归。

锁定 2026-08-22 CI 三红之一：service.py 使用
``manifest_artifact_input_contract_versions`` 但 import 块遗漏导致 F821/NameError。
本用例走真实 snapshot 解析路径：测试库迁移（0045-0050）已为全部 8 个 skill 提供了
production Activation 与 Revision，v2 manifest 场景下服务端链路必须无 NameError
并产出正确的 ``artifact_input_contract_versions``。
"""

from __future__ import annotations

import pytest

from app.marketing_capability_pack.runtime import build_marketing_run_capability
from app.marketing_skills.snapshot import (
    SkillSnapshotService,
    manifest_artifact_input_contract_versions,
)


async def test_post_brand_v2_manifest_contract_map_executes(db_session, user_factory) -> None:
    user = await user_factory()
    from sqlalchemy import select

    from app.tenancy.models import TenantMembership

    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    base = build_marketing_run_capability()
    capability = await SkillSnapshotService.resolve_for_new_run(
        db_session,
        tenant_id=membership.tenant_id,
        base_capability=base,
        environment="production",
        require_database_entries=False,
    )
    manifest = SkillSnapshotService.manifest_from_capability(capability)
    contract_map = manifest_artifact_input_contract_versions(manifest)
    assert isinstance(contract_map, dict)
    if manifest.schema_version == "skill_manifest_v2":
        # social-marketing-analyst（marketing_root_v1，rev3 迁移行为 v1 合同）
        assert contract_map.get("marketing_root_v1") == "direct_model_input_v1"
