"""post-brand 固化：从已持久化 Run Snapshot + SkillRevision 生成无秘密、digest 绑定的固化输入。

只读校验历史成功 Run 的 ``skill_manifest`` 与数据库 ``SkillRevision``：候选扫描
不输出正文；导出必须由显式 source map（``revision_id + scope_key``）逐 entry
选定，绝不用当前 Activation 或 ``created_at`` 猜历史 scope。Task 2 的 bootstrap
bundle 生成器直接消费 ``PostBrandSkillSnapshotExport.model_dump(mode="json")``。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun
from app.marketing_skills.models import SkillRevision
from app.marketing_skills.snapshot import SkillManifest
from app.marketing_skills.validation import canonical_skill_digest

GLOBAL_SCOPE_KEY = "__global__"

_RUN_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,36}$")

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9._-]+"),
    re.compile(r"\b(mysql|postgres(ql)?|redis|amqp)://[^\s\"']+"),
    re.compile(r"://[^/\s:@]+:[^@\s/]+@"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class SkillSeedError(ValueError):
    """固化导出的稳定错误码（消息即 code，供 CLI 与测试断言）。"""


class SkillRevisionSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision_id: str
    scope_key: str


class SkillSourceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision_id: str
    scope_key: str


class SkillSourceEntryCandidates(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    revision: int
    content_digest: str
    candidates: tuple[SkillSourceCandidate, ...]


class PostBrandSkillSourceCandidates(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    entries: tuple[SkillSourceEntryCandidates, ...]


class PromotedSkillEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision_id: str
    scope_key: str
    skill_name: str
    revision: int
    content: str
    content_digest: str
    required_tools: tuple[str, ...]
    artifact_contract: str | None


class PostBrandSkillSnapshotExport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["post_brand_skill_snapshot_v1"]
    run_id: str
    manifest_digest: str
    entries: dict[str, PromotedSkillEntry]


def _assert_secret_free(value: object) -> None:
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                raise SkillSeedError("skill_seed_secret_detected")
        return
    if isinstance(value, dict):
        for item in value.values():
            _assert_secret_free(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_secret_free(item)


async def _load_unique_run(db: AsyncSession, *, run_prefix: str) -> AgentRun:
    if not _RUN_PREFIX_PATTERN.fullmatch(run_prefix):
        raise SkillSeedError("skill_seed_source_missing")
    run_prefix_pattern = f"{run_prefix}%"
    runs = (
        await db.scalars(
            select(AgentRun).where(AgentRun.id.like(run_prefix_pattern)).order_by(AgentRun.id)
        )
    ).all()
    if not runs:
        raise SkillSeedError("skill_seed_source_missing")
    if len(runs) > 1:
        raise SkillSeedError("skill_seed_source_ambiguous")
    return runs[0]


def _manifest_of(run: AgentRun) -> SkillManifest:
    snapshot = run.runtime_config_snapshot_json or {}
    raw_manifest = snapshot.get("skill_manifest")
    if not isinstance(raw_manifest, dict):
        raise SkillSeedError("skill_seed_source_missing")
    try:
        return SkillManifest.model_validate(raw_manifest)
    except ValueError as exc:
        raise SkillSeedError("skill_seed_digest_mismatch") from exc


def _visible_scope_filter(run: AgentRun):
    return or_(
        SkillRevision.tenant_id.is_(None),
        SkillRevision.tenant_id == run.tenant_id,
    )


async def list_post_brand_skill_source_candidates(
    db: AsyncSession, *, run_prefix: str
) -> PostBrandSkillSourceCandidates:
    """列出 manifest 每个 entry 的精确匹配候选（无正文）。

    只列出 ``name + revision + digest + canonical content`` 全部一致且 scope 对
    Run 可见（global 或 Run tenant）的 Revision ID/scope；多于一个候选时由调用
    方显式选择，本函数不做任何默认取舍。
    """
    run = await _load_unique_run(db, run_prefix=run_prefix)
    manifest = _manifest_of(run)
    entries: list[SkillSourceEntryCandidates] = []
    for entry in sorted(manifest.entries, key=lambda item: item.name):
        rows = (
            await db.scalars(
                select(SkillRevision)
                .where(
                    SkillRevision.skill_name == entry.name,
                    SkillRevision.revision == entry.revision,
                    SkillRevision.content_digest == entry.content_digest,
                    SkillRevision.content == entry.content,
                    _visible_scope_filter(run),
                )
                .order_by(SkillRevision.id)
            )
        ).all()
        entries.append(
            SkillSourceEntryCandidates(
                name=entry.name,
                revision=entry.revision,
                content_digest=entry.content_digest,
                candidates=tuple(
                    SkillSourceCandidate(revision_id=row.id, scope_key=row.scope_key)
                    for row in rows
                ),
            )
        )
    return PostBrandSkillSourceCandidates(run_id=run.id, entries=tuple(entries))


async def load_post_brand_skill_snapshot(
    db: AsyncSession, *, run_prefix: str, source_map: Mapping[str, SkillRevisionSource]
) -> PostBrandSkillSnapshotExport:
    """按显式 source_map 逐 entry 读取数据库 Revision 并导出固化 fixture。

    key 集合必须与 manifest entry 集合完全相等；每个 entry 只按 map 的
    Revision ID 读取 DB，并核对 map scope、Run tenant 可见性、正文与 digest；
    输出前递归扫描 secret/DSN/Bearer 模式 fail-closed。
    """
    run = await _load_unique_run(db, run_prefix=run_prefix)
    manifest = _manifest_of(run)
    manifest_names = {entry.name for entry in manifest.entries}
    if set(source_map.keys()) != manifest_names:
        raise SkillSeedError("skill_seed_revision_mismatch")

    promoted: dict[str, PromotedSkillEntry] = {}
    for entry in sorted(manifest.entries, key=lambda item: item.name):
        source = source_map[entry.name]
        row = await db.get(SkillRevision, source.revision_id)
        if row is None:
            raise SkillSeedError("skill_seed_source_missing")
        if row.skill_name != entry.name or row.revision != entry.revision:
            raise SkillSeedError("skill_seed_revision_mismatch")
        if row.scope_key != source.scope_key:
            raise SkillSeedError("skill_seed_revision_mismatch")
        if not (row.tenant_id is None or row.tenant_id == run.tenant_id):
            raise SkillSeedError("skill_seed_revision_mismatch")
        if canonical_skill_digest(row.content) != row.content_digest:
            raise SkillSeedError("skill_seed_digest_mismatch")
        if row.content_digest != entry.content_digest or row.content != entry.content:
            raise SkillSeedError("skill_seed_digest_mismatch")
        # 同 scope 出现多份完全相同身份属数据库异常状态：map 无法在本 scope 内
        # 唯一确定事实来源。
        twins = (
            await db.scalars(
                select(SkillRevision.id).where(
                    SkillRevision.skill_name == entry.name,
                    SkillRevision.revision == entry.revision,
                    SkillRevision.content_digest == entry.content_digest,
                    SkillRevision.content == entry.content,
                    SkillRevision.scope_key == source.scope_key,
                    _visible_scope_filter(run),
                )
            )
        ).all()
        if len(twins) > 1:
            raise SkillSeedError("skill_seed_revision_scope_ambiguous")
        promoted[entry.name] = PromotedSkillEntry(
            revision_id=row.id,
            scope_key=row.scope_key,
            skill_name=row.skill_name,
            revision=row.revision,
            content=row.content,
            content_digest=row.content_digest,
            required_tools=tuple(row.required_tools or ()),
            artifact_contract=row.artifact_contract,
        )

    export = PostBrandSkillSnapshotExport(
        schema_version="post_brand_skill_snapshot_v1",
        run_id=run.id,
        manifest_digest=manifest.manifest_digest,
        entries=promoted,
    )
    _assert_secret_free(export.model_dump(mode="json"))
    return export
