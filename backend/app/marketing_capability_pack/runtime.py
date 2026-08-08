"""营销 Run 的纯值快照与受控 Skill 载入，不依赖 Pi、DB 或 Settings。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from .loader import CapabilityPackLoader


class MarketingSkillSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    digest: str
    content: str
    required_tools: tuple[str, ...]
    artifact_contract: str

    @model_validator(mode="after")
    def verify_digest(self) -> MarketingSkillSnapshot:
        if _digest(self.content) != self.digest:
            raise ValueError("marketing_skill_digest_mismatch")
        return self


class MarketingRunCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_name: str
    pack_version: str
    manifest_digest: str
    runtime_contract_version: str
    root_policy: str
    root_policy_digest: str
    skills: tuple[MarketingSkillSnapshot, ...]
    artifact_contracts: tuple[dict[str, str], ...]
    builder_versions: dict[str, str]
    exporter_versions: dict[str, str]
    model_version: str | None = None
    data_gateway_version: str = "datatap_gateway_v1"

    @model_validator(mode="after")
    def verify_root_policy(self) -> MarketingRunCapability:
        if _digest(self.root_policy) != self.root_policy_digest:
            raise ValueError("marketing_root_policy_digest_mismatch")
        if len({skill.name for skill in self.skills}) != len(self.skills):
            raise ValueError("marketing_skill_duplicate")
        return self

    @property
    def enabled_skills(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)

    def load_skill(self, skill_name: str, requested_version: str | None = None) -> dict[str, object]:
        skill = next((item for item in self.skills if item.name == skill_name), None)
        if skill is None or (requested_version is not None and requested_version != skill.version):
            raise ValueError("marketing_skill_not_enabled")
        return {
            "name": skill.name,
            "version": skill.version,
            "digest": skill.digest,
            "content": skill.content,
            "required_tools": list(skill.required_tools),
            "artifact_contract": skill.artifact_contract,
        }


def build_marketing_run_capability(
    *, model_version: str | None = None, packs_root: Path | None = None
) -> MarketingRunCapability:
    root = packs_root or Path(__file__).with_name("packs")
    loader = CapabilityPackLoader(root)
    snapshot = loader.load_manifest("marketing-v1")
    skills = tuple(
        MarketingSkillSnapshot(
            name=spec.name,
            version=spec.version,
            digest=spec.digest,
            content=loader.load_skill(snapshot, spec.name).content,
            required_tools=spec.required_tools,
            artifact_contract=spec.artifact_contract,
        )
        for spec in snapshot.skills
    )
    return MarketingRunCapability(
        pack_name=snapshot.pack_name,
        pack_version=snapshot.pack_version,
        manifest_digest=snapshot.manifest_digest,
        runtime_contract_version=snapshot.runtime_contract_version,
        root_policy=snapshot.root_policy,
        root_policy_digest=snapshot.root_policy_digest,
        skills=skills,
        artifact_contracts=snapshot.artifact_contracts,
        builder_versions=snapshot.builder_versions,
        exporter_versions=snapshot.exporter_versions,
        model_version=model_version,
    )


def render_marketing_system_context(
    capability: MarketingRunCapability, run_context: dict[str, object]
) -> str:
    return "\n\n".join(
        (
            "[MARKETING_ROOT_POLICY]\n" + capability.root_policy,
            "[MARKETING_RUN_CONTEXT]\n"
            + json.dumps(run_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
