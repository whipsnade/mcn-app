"""post-brand bootstrap bundle 的加载与 digest 校验。

bundle 是版本化、只增不改的固化事实：不参与已创建 Run 的执行（running/
recovery/resume 不重新解析），仅供新环境 initializer 与管理 API 使用。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_CONTRACT_VERSIONS = frozenset({"direct_model_input_v1", "source_bound_input_v2"})
_DEFAULT_BUNDLE_NAME = "post-brand-default-v1"


class SkillBootstrapError(ValueError):
    """稳定错误码（消息即 code）。"""


@dataclass(frozen=True)
class BootstrapRevision:
    skill_name: str
    revision: int
    scope_key: str
    model_input_contract_version: str
    default_activation: bool
    candidate_activation: bool
    content: str
    content_digest: str
    source_revision_id: str | None = None
    parent_revision: int | None = None


@dataclass(frozen=True)
class PostBrandBootstrapBundle:
    name: str
    source_run_id: str
    source_manifest_digest: str
    revisions: tuple[BootstrapRevision, ...]
    bundle_digest: str

    @property
    def revisions_by_skill(self) -> dict[str, tuple[BootstrapRevision, ...]]:
        grouped: dict[str, list[BootstrapRevision]] = {}
        for item in self.revisions:
            grouped.setdefault(item.skill_name, []).append(item)
        for items in grouped.values():
            items.sort(key=lambda entry: entry.revision)
        return {name: tuple(items) for name, items in grouped.items()}


def _normalize_content(content: str) -> str:
    return unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))


def canonical_skill_digest(content: str) -> str:
    return hashlib.sha256(_normalize_content(content).encode("utf-8")).hexdigest()


def _bundle_body_digest(payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "bundle_digest"}
    rendered = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _packs_root() -> Path:
    from app.marketing_capability_pack import loader as _loader  # noqa: PLC0415

    return Path(_loader.__file__).resolve().parent / "packs"


def _parse_revision(raw: dict) -> BootstrapRevision:
    for key in (
        "skill_name",
        "revision",
        "scope_key",
        "model_input_contract_version",
        "default_activation",
        "candidate_activation",
        "content",
        "content_digest",
    ):
        if key not in raw:
            raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    if not isinstance(raw["revision"], int) or raw["revision"] < 1:
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    if raw["model_input_contract_version"] not in _ALLOWED_CONTRACT_VERSIONS:
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    if not isinstance(raw["default_activation"], bool) or not isinstance(
        raw["candidate_activation"], bool
    ):
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    if raw["default_activation"] and raw["candidate_activation"]:
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    return BootstrapRevision(
        skill_name=raw["skill_name"],
        revision=raw["revision"],
        scope_key=raw["scope_key"],
        model_input_contract_version=raw["model_input_contract_version"],
        default_activation=raw["default_activation"],
        candidate_activation=raw["candidate_activation"],
        content=raw["content"],
        content_digest=raw["content_digest"],
        source_revision_id=raw.get("source_revision_id"),
        parent_revision=raw.get("parent_revision"),
    )


def load_post_brand_bootstrap(name: str = _DEFAULT_BUNDLE_NAME) -> PostBrandBootstrapBundle:
    path = _packs_root() / "marketing-v2" / "bootstrap" / f"{name}.json"
    if not path.is_file():
        raise SkillBootstrapError("skill_bootstrap_bundle_not_found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid") from exc
    if payload.get("schema_version") != "post_brand_bootstrap_v1":
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    if payload.get("name") != name:
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    source = payload.get("source") or {}
    if not isinstance(source.get("run_id"), str) or not _SHA256.fullmatch(
        str(source.get("manifest_digest") or "")
    ):
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    revisions = tuple(_parse_revision(item) for item in payload.get("revisions", []))
    if not revisions:
        raise SkillBootstrapError("skill_bootstrap_bundle_invalid")
    bundle = PostBrandBootstrapBundle(
        name=name,
        source_run_id=source["run_id"],
        source_manifest_digest=source["manifest_digest"],
        revisions=revisions,
        bundle_digest=str(payload.get("bundle_digest") or ""),
    )
    validate_bootstrap_digest(bundle)
    return bundle


def validate_bootstrap_digest(bundle: PostBrandBootstrapBundle) -> None:
    """重算每条 Revision 正文 digest 与 bundle 自身 digest；不符即抛错。"""
    for revision in bundle.revisions:
        if canonical_skill_digest(revision.content) != revision.content_digest:
            raise SkillBootstrapError("skill_seed_digest_conflict")
    path = _packs_root() / "marketing-v2" / "bootstrap" / f"{bundle.name}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if _bundle_body_digest(payload) != bundle.bundle_digest:
        raise SkillBootstrapError("skill_seed_digest_conflict")
