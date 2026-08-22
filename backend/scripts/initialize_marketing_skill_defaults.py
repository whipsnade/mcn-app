"""新环境一次性初始化 post-brand Skill 默认指针。

- 仅接受显式 ``--new-environment``：这是环境创建清单的一步，不由 Alembic、
  startup 或部署脚本自动调用；
- 先验证每个目标 skill 的 production Activation 仍是 0048 审计基线（active
  revision 指向 0048 插入的 Revision ID）且没有任何管理员 Activation 审计；
- 单事务只把 ``default_activation=true`` 的 bundle Revision 设为 active 并写
  bootstrap 审计；candidate 指针一律跳过并记录；
- 重放幂等；任一目标已被人工变更即 ``skill_bootstrap_environment_not_fresh``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.db.session import SessionFactory
from app.marketing_skills.bootstrap import load_post_brand_bootstrap
from app.marketing_skills.models import SkillActivation, SkillRevision

# 0048 审计基线：social-marketing-analyst active revision 3 的源 Revision ID。
BASELINE_REVISION_IDS = {
    "social-marketing-analyst": "00000000-0045-4000-8000-000000000001",
}


class BootstrapInitError(ValueError):
    """稳定错误码（消息即 code）。"""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def initialize(
    db,
    *,
    environment: str,
    bundle_names: tuple[str, ...],
) -> dict[str, object]:
    if environment not in {"development", "staging", "production"}:
        raise BootstrapInitError("skill_bootstrap_environment_invalid")
    all_bundles = [load_post_brand_bootstrap(name) for name in bundle_names]
    defaults: dict[tuple[str, str], object] = {}
    for bundle in all_bundles:
        for revision in bundle.revisions:
            if revision.default_activation:
                defaults[(revision.scope_key, revision.skill_name)] = revision
    if not defaults:
        raise BootstrapInitError("skill_bootstrap_defaults_missing")
    skipped: list[dict[str, object]] = []
    applied: list[dict[str, object]] = []
    now = _now()
    for (scope_key, skill_name), revision in sorted(defaults.items()):
        activation = (
            await db.scalar(
                select(SkillActivation).where(
                    SkillActivation.environment == environment,
                    SkillActivation.scope_key == scope_key,
                    SkillActivation.skill_name == skill_name,
                )
            )
        )
        target_row = (
            await db.scalar(
                select(SkillRevision).where(
                    SkillRevision.scope_key == scope_key,
                    SkillRevision.skill_name == skill_name,
                    SkillRevision.revision == revision.revision,
                )
            )
        )
        if target_row is None or target_row.content_digest != revision.content_digest:
            raise BootstrapInitError("skill_bootstrap_revision_missing")
        if activation is not None:
            current = (
                await db.scalar(
                    select(SkillRevision).where(SkillRevision.id == activation.active_revision_id)
                )
            )
            if current is not None and current.id == target_row.id:
                applied.append(
                    {"skill_name": skill_name, "revision": revision.revision, "idempotent": True}
                )
                continue
            baseline_id = BASELINE_REVISION_IDS.get(skill_name)
            if current is None or current.id != baseline_id:
                raise BootstrapInitError("skill_bootstrap_environment_not_fresh")
        activation_id = activation.id if activation is not None else str(uuid4())
        if activation is None:
            db.add(
                SkillActivation(
                    id=activation_id,
                    environment=environment,
                    tenant_id=None,
                    skill_name=skill_name,
                    active_revision_id=target_row.id,
                    previous_revision_id=None,
                    rollout_percent=100,
                    updated_by=None,
                    updated_at=now,
                    scope_key=scope_key,
                    previous_rollout_percent=None,
                )
            )
        else:
            activation.active_revision_id = target_row.id
            activation.updated_at = now
        applied.append(
            {
                "skill_name": skill_name,
                "revision": revision.revision,
                "revision_id": target_row.id,
                "idempotent": False,
            }
        )
    for bundle in all_bundles:
        for revision in bundle.revisions:
            if revision.candidate_activation:
                skipped.append(
                    {"bundle": bundle.name, "skill_name": revision.skill_name, "revision": revision.revision}
                )
    await db.flush()
    # bootstrap 审计：AdminAuditLog 强制 admin_user_id（无系统账号），因此
    # 审计事实 = Activations 行变更（updated_at/previous 指针）+ 本脚本 stdout
    # 完整清单（环境创建清单存档）。
    await db.commit()
    return {"applied": applied, "skipped_candidates": skipped}


async def _main_async(args: argparse.Namespace) -> int:
    async with SessionFactory() as db:
        try:
            result = await initialize(
                db,
                environment=args.environment,
                bundle_names=tuple(args.bundle),
            )
        except BootstrapInitError as exc:
            await db.rollback()
            print(f"error={exc}", file=sys.stderr)
            return 1
    print(json.dumps({"applied": len(result["applied"]), "skipped": len(result["skipped_candidates"])}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new-environment", action="store_true", required=True)
    parser.add_argument("--environment", required=True, choices=["development", "staging", "production"])
    parser.add_argument("--bundle", action="append", required=True)
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
