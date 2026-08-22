"""只从版本化 Pack 读取 manifest 与专项 Skill，拒绝任意文件访问。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import (
    BootstrapBundleSpec,
    CapabilityPackSnapshot,
    LoadedMarketingSkill,
    MarketingSkillSpec,
)

_PACK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:secret|token|api[_-]?key|endpoint|dsn|password)", re.IGNORECASE
)
_SENSITIVE_CONTENT_PATTERNS = (
    re.compile(r"\bsk(?:-proj)?-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"[?&](?:token|api[_-]?key|key)=[^&\s]{1,}", re.IGNORECASE),
    re.compile(
        r"\b(?:mysql(?:\+[A-Za-z0-9_-]+)?|postgres(?:ql)?|mongodb|redis)://[^\s/@:]+:[^\s/@]+@",
        re.IGNORECASE,
    ),
)
_BASE_CONTRACT_TYPES = frozenset(
    {"brand_report_v3", "campaign_report_v3", "kol_selection_v3"}
)
MARKETING_RUNTIME_CONTRACT_VERSION = "marketing_runtime_v1"
_MANIFEST_KEYS = {
    "pack_name",
    "pack_version",
    "runtime_contract_version",
    "root_policy",
    "skills",
    "artifact_contracts",
    "builder_versions",
    "exporter_versions",
    "bootstrap_bundles",
}


class CapabilityPackError(ValueError):
    pass


class CapabilityPackLoader:
    def __init__(self, packs_root: Path) -> None:
        self._packs_root = packs_root.resolve(strict=True)

    def load_manifest(self, pack_name: str) -> CapabilityPackSnapshot:
        root = self._pack_root(pack_name)
        manifest_path = self._safe_path(root, "manifest.json")
        payload = self._load_json(manifest_path)
        self._validate_manifest_shape(payload)
        if payload["pack_name"] != "marketing":
            raise CapabilityPackError("pack_name_invalid")
        if payload["runtime_contract_version"] != MARKETING_RUNTIME_CONTRACT_VERSION:
            raise CapabilityPackError("manifest_runtime_contract_unsupported")
        pack_version = self._required_text(payload, "pack_version")
        root_policy = payload["root_policy"]
        if not isinstance(root_policy, dict):
            raise CapabilityPackError("manifest_root_policy_invalid")
        policy_path, policy_digest = self._entry_path_and_digest(root_policy)
        policy = self._read_checked(root, policy_path, policy_digest)
        skills = tuple(self._skill_spec(root, item) for item in payload["skills"])
        if len({skill.name for skill in skills}) != len(skills):
            raise CapabilityPackError("manifest_skill_duplicate")
        return CapabilityPackSnapshot(
            pack_name=payload["pack_name"],
            pack_version=pack_version,
            runtime_contract_version=self._required_text(payload, "runtime_contract_version"),
            manifest_digest=_digest_json(payload),
            root_policy=policy,
            root_policy_digest=policy_digest,
            skills=skills,
            artifact_contracts=self._contracts(
                root, payload["artifact_contracts"], pack_version=pack_version
            ),
            builder_versions=self._string_map(payload["builder_versions"]),
            exporter_versions=self._string_map(payload["exporter_versions"]),
            bootstrap_bundles=self._bootstrap_bundles(root, payload.get("bootstrap_bundles", [])),
        )

    def _bootstrap_bundles(
        self, root: Path, entries: list[dict[str, Any]]
    ) -> tuple[BootstrapBundleSpec, ...]:
        bundles: list[BootstrapBundleSpec] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry.keys()) != {"name", "path", "digest"}:
                raise CapabilityPackError("manifest_bootstrap_bundle_invalid")
            name = entry["name"]
            path = entry["path"]
            digest = entry["digest"]
            if (
                not isinstance(name, str)
                or not isinstance(path, str)
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
                or name in seen
            ):
                raise CapabilityPackError("manifest_bootstrap_bundle_invalid")
            seen.add(name)
            # bundle 文件必须存在且内容 digest 与登记一致（read checked 复用
            # 既有安全路径解析，禁止任意文件访问）。
            self._read_checked(root, path, digest)
            bundles.append(BootstrapBundleSpec(name=name, path=path, digest=digest))
        return tuple(bundles)

    def load_skill(
        self,
        snapshot: CapabilityPackSnapshot,
        skill_name: str,
        requested_version: str | None = None,
    ) -> LoadedMarketingSkill:
        spec = next((item for item in snapshot.skills if item.name == skill_name), None)
        if spec is None or (requested_version is not None and requested_version != spec.version):
            raise CapabilityPackError("marketing_skill_not_enabled")
        root = self._pack_root_for_snapshot(snapshot)
        return LoadedMarketingSkill(
            name=spec.name,
            version=spec.version,
            digest=spec.digest,
            content=self._read_checked(root, spec.path, spec.digest),
            required_tools=spec.required_tools,
            artifact_contract=spec.artifact_contract,
        )

    def _pack_root_for_snapshot(self, snapshot: CapabilityPackSnapshot) -> Path:
        for path in self._packs_root.iterdir():
            if path.is_dir() and not path.is_symlink():
                manifest = path / "manifest.json"
                if manifest.is_file() and _digest_json(self._load_json(manifest)) == snapshot.manifest_digest:
                    return path.resolve(strict=True)
        raise CapabilityPackError("pack_snapshot_not_found")

    def _pack_root(self, pack_name: str) -> Path:
        if not _PACK_NAME.fullmatch(pack_name):
            raise CapabilityPackError("pack_path_invalid")
        return self._safe_path(self._packs_root, pack_name, directory=True)

    @staticmethod
    def _safe_path(root: Path, relative: str, *, directory: bool = False) -> Path:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise CapabilityPackError("pack_path_invalid")
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
        except (FileNotFoundError, ValueError):
            raise CapabilityPackError("pack_path_invalid") from None
        if candidate.is_symlink() or (directory and not resolved.is_dir()) or (not directory and not resolved.is_file()):
            raise CapabilityPackError("pack_path_invalid")
        return resolved

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CapabilityPackError("manifest_invalid") from exc
        if not isinstance(value, dict):
            raise CapabilityPackError("manifest_invalid")
        return value

    @staticmethod
    def _validate_manifest_shape(payload: dict[str, Any]) -> None:
        if _contains_sensitive_key(payload):
            raise CapabilityPackError("manifest_sensitive_field")
        if _contains_sensitive_value(payload):
            raise CapabilityPackError("manifest_sensitive_content")
        if not set(payload) <= _MANIFEST_KEYS or not {"skills", "artifact_contracts"} <= set(payload):
            raise CapabilityPackError("manifest_fields_invalid")
        if "bootstrap_bundles" in payload and not isinstance(payload["bootstrap_bundles"], list):
            raise CapabilityPackError("manifest_fields_invalid")
        for key in ("skills", "artifact_contracts"):
            if not isinstance(payload[key], list):
                raise CapabilityPackError("manifest_fields_invalid")
        for key in ("builder_versions", "exporter_versions"):
            if not isinstance(payload[key], dict):
                raise CapabilityPackError("manifest_fields_invalid")

    def _skill_spec(self, root: Path, value: Any) -> MarketingSkillSpec:
        if not isinstance(value, dict) or set(value) != {
            "name", "version", "path", "digest", "required_tools", "artifact_contract"
        }:
            raise CapabilityPackError("manifest_skill_invalid")
        path, digest = self._entry_path_and_digest(value)
        self._read_checked(root, path, digest)
        tools = value["required_tools"]
        if not isinstance(tools, list) or not all(isinstance(tool, str) and tool for tool in tools):
            raise CapabilityPackError("manifest_skill_invalid")
        return MarketingSkillSpec(
            name=self._required_text(value, "name"),
            version=self._required_text(value, "version"),
            path=path,
            digest=digest,
            required_tools=tuple(tools),
            artifact_contract=self._required_text(value, "artifact_contract"),
        )

    def _contracts(self, root: Path, values: list[Any], *, pack_version: str) -> tuple[dict[str, str], ...]:
        expected_types = _BASE_CONTRACT_TYPES | (
            {"analysis_report_v1"} if pack_version == "1.1.0" else set()
        )
        if len(values) != len(expected_types):
            raise CapabilityPackError("manifest_contracts_invalid")
        contracts: list[dict[str, str]] = []
        for value in values:
            if not isinstance(value, dict) or set(value) != {"artifact_type", "schema_version", "path", "digest"}:
                raise CapabilityPackError("manifest_contracts_invalid")
            path, digest = self._entry_path_and_digest(value)
            content = self._read_checked(root, path, digest)
            try:
                contract = json.loads(content)
            except json.JSONDecodeError as exc:
                raise CapabilityPackError("manifest_contract_invalid") from exc
            if not isinstance(contract, dict) or contract.get("artifact_type") != value["artifact_type"] or contract.get("schema_version") != value["schema_version"]:
                raise CapabilityPackError("manifest_contract_invalid")
            contracts.append({"artifact_type": value["artifact_type"], "schema_version": value["schema_version"], "digest": digest, "content": content})
        if {item["artifact_type"] for item in contracts} != expected_types:
            raise CapabilityPackError("manifest_contracts_invalid")
        return tuple(contracts)

    @staticmethod
    def _entry_path_and_digest(value: dict[str, Any]) -> tuple[str, str]:
        path = value.get("path")
        digest = value.get("digest")
        if not isinstance(path, str) or not _SHA256.fullmatch(digest if isinstance(digest, str) else ""):
            raise CapabilityPackError("manifest_digest_invalid")
        return path, digest

    @staticmethod
    def _required_text(value: dict[str, Any], key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result.strip():
            raise CapabilityPackError("manifest_fields_invalid")
        return result

    @staticmethod
    def _string_map(value: Any) -> dict[str, str]:
        if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
            raise CapabilityPackError("manifest_fields_invalid")
        return dict(value)

    def _read_checked(self, root: Path, relative: str, digest: str) -> str:
        content = self._safe_path(root, relative).read_text(encoding="utf-8")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
            raise CapabilityPackError("pack_digest_mismatch")
        if any(pattern.search(content) for pattern in _SENSITIVE_CONTENT_PATTERNS):
            raise CapabilityPackError("pack_sensitive_content")
        return content


def _digest_json(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and _SENSITIVE_KEY.search(key)) or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _contains_sensitive_value(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SENSITIVE_CONTENT_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_sensitive_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_sensitive_value(item) for item in value)
    return False
