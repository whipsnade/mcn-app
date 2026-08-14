from dataclasses import dataclass


@dataclass(frozen=True)
class MarketingSkillSpec:
    name: str
    version: str
    path: str
    digest: str
    required_tools: tuple[str, ...]
    artifact_contract: str


@dataclass(frozen=True)
class CapabilityPackSnapshot:
    pack_name: str
    pack_version: str
    runtime_contract_version: str
    manifest_digest: str
    root_policy: str
    root_policy_digest: str
    skills: tuple[MarketingSkillSpec, ...]
    artifact_contracts: tuple[dict[str, str], ...]
    builder_versions: dict[str, str]
    exporter_versions: dict[str, str]


@dataclass(frozen=True)
class LoadedMarketingSkill:
    name: str
    version: str
    digest: str
    content: str
    required_tools: tuple[str, ...]
    artifact_contract: str
