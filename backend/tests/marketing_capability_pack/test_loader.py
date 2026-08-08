import hashlib
import json
from pathlib import Path

import pytest

from app.marketing_capability_pack.loader import CapabilityPackError, CapabilityPackLoader


def _write_pack(root: Path, *, include_secret: bool = False) -> Path:
    pack = root / "marketing-v1"
    (pack / "policies").mkdir(parents=True)
    (pack / "skills" / "brand-research-report").mkdir(parents=True)
    (pack / "contracts").mkdir()
    root_policy = "只处理社媒营销；非营销请求必须拒答。"
    skill = "---\nname: brand-research-report\ndescription: 生成品牌报告。\n---\n\n完整技能正文。\n"
    (pack / "policies" / "root-policy.md").write_text(root_policy, encoding="utf-8")
    (pack / "skills" / "brand-research-report" / "SKILL.md").write_text(skill, encoding="utf-8")
    manifest = {
        "pack_name": "marketing",
        "pack_version": "1.0.0",
        "runtime_contract_version": "marketing_runtime_v1",
        "root_policy": {"path": "policies/root-policy.md", "digest": _digest(root_policy)},
        "skills": [{
            "name": "brand-research-report",
            "version": "1.0.0",
            "path": "skills/brand-research-report/SKILL.md",
            "digest": _digest(skill),
            "required_tools": ["build_brand_report_draft", "publish_artifacts"],
            "artifact_contract": "brand_report_v3",
        }],
        "artifact_contracts": [],
        "builder_versions": {"brand_report_v3": "1.0.0"},
        "exporter_versions": {"brand_report_v3": "1.0.0"},
    }
    for artifact_type in ("brand_report_v3", "campaign_report_v3", "kol_selection_v3"):
        content = json.dumps({"artifact_type": artifact_type, "schema_version": "3"})
        path = f"contracts/{artifact_type}.json"
        (pack / path).write_text(content, encoding="utf-8")
        manifest["artifact_contracts"].append(
            {"artifact_type": artifact_type, "schema_version": "3", "path": path, "digest": _digest(content)}
        )
    if include_secret:
        manifest["api_key"] = "must-not-load"
    (pack / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return pack


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_loader_returns_stable_snapshot_and_complete_skill_content(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    loader = CapabilityPackLoader(tmp_path)

    first = loader.load_manifest("marketing-v1")
    second = loader.load_manifest("marketing-v1")
    skill = loader.load_skill(first, "brand-research-report")

    assert first.manifest_digest == second.manifest_digest
    assert first.root_policy == "只处理社媒营销；非营销请求必须拒答。"
    assert skill.content.endswith("完整技能正文。\n")
    assert skill.required_tools == ("build_brand_report_draft", "publish_artifacts")


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/outside.md"])
def test_loader_rejects_manifest_path_escape(tmp_path: Path, path: str) -> None:
    pack = _write_pack(tmp_path)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["root_policy"]["path"] = path
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CapabilityPackError, match="pack_path_invalid"):
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")


def test_loader_rejects_symlink_and_manifest_secret(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path, include_secret=True)
    outside = tmp_path / "outside.md"
    outside.write_text("越界", encoding="utf-8")
    (pack / "policies" / "root-policy.md").unlink()
    (pack / "policies" / "root-policy.md").symlink_to(outside)

    with pytest.raises(CapabilityPackError, match="manifest_sensitive_field"):
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")

    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    manifest.pop("api_key")
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CapabilityPackError, match="pack_path_invalid"):
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")


def test_loader_rejects_skill_digest_mismatch(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    loader = CapabilityPackLoader(tmp_path)
    snapshot = loader.load_manifest("marketing-v1")
    path = tmp_path / "marketing-v1" / "skills" / "brand-research-report" / "SKILL.md"
    path.write_text("已被篡改", encoding="utf-8")

    with pytest.raises(CapabilityPackError, match="pack_digest_mismatch"):
        loader.load_skill(snapshot, "brand-research-report")


def test_repository_marketing_pack_declares_all_business_capabilities() -> None:
    packs_root = Path(__file__).parents[2] / "app" / "marketing_capability_pack" / "packs"
    snapshot = CapabilityPackLoader(packs_root).load_manifest("marketing-v1")

    assert {skill.name for skill in snapshot.skills} == {
        "social-marketing-analyst",
        "brand-research-report",
        "campaign-evaluation-report",
        "kol-selection-report",
        "artifact-drilldown",
        "marketing-strategy",
    }
    assert snapshot.runtime_contract_version == "marketing_runtime_v1"


@pytest.mark.parametrize("field", ["api_key", "token", "password", "secret", "endpoint", "dsn"])
def test_loader_rejects_nested_sensitive_manifest_fields(tmp_path: Path, field: str) -> None:
    pack = _write_pack(tmp_path)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["root_policy"]["nested"] = {"configuration": {field: "not-a-credential"}}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CapabilityPackError, match="manifest_sensitive_field"):
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")


@pytest.mark.parametrize(
    ("location", "credential"),
    [
        ("root_policy", "sk-abcdefghijk"),
        ("skill", "sk-proj-abcdefghijk"),
        ("contract", "Bearer abcdefghijk"),
        ("root_policy", "https://example.invalid/v1?token=abcdefgh"),
        ("skill", "postgresql://user:password@db.example.invalid:5432/app"),
        ("contract", "mysql+asyncmy://user:password@db.example.invalid/app"),
    ],
)
def test_loader_rejects_credential_patterns_without_echoing_content(
    tmp_path: Path, location: str, credential: str
) -> None:
    pack = _write_pack(tmp_path)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if location == "root_policy":
        path = pack / "policies" / "root-policy.md"
        content = f"ordinary description\n{credential}\n"
        manifest["root_policy"]["digest"] = _digest(content)
    elif location == "skill":
        path = pack / "skills" / "brand-research-report" / "SKILL.md"
        content = path.read_text(encoding="utf-8") + credential + "\n"
        manifest["skills"][0]["digest"] = _digest(content)
    else:
        path = pack / "contracts" / "brand_report_v3.json"
        content = json.dumps(
            {"artifact_type": "brand_report_v3", "schema_version": "3", "note": credential}
        )
        manifest["artifact_contracts"][0]["digest"] = _digest(content)
    path.write_text(content, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CapabilityPackError, match="pack_sensitive_content") as error:
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")
    assert credential not in str(error.value)


def test_loader_permits_ordinary_token_and_endpoint_words(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path)
    policy_path = pack / "policies" / "root-policy.md"
    policy = "此说明中的 token 和 endpoint 只是普通术语，不包含凭证。"
    policy_path.write_text(policy, encoding="utf-8")
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["root_policy"]["digest"] = _digest(policy)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert CapabilityPackLoader(tmp_path).load_manifest("marketing-v1").root_policy == policy


@pytest.mark.parametrize(
    ("field", "credential"),
    [
        ("builder_versions", "sk-abcdefghijk"),
        ("pack_version", "postgresql://user:password@db.example.invalid:5432/app"),
        ("exporter_versions", "Bearer abcdefghijk"),
        ("pack_version", "https://example.invalid/v1?token=abcdefgh"),
    ],
)
def test_loader_rejects_credentials_in_manifest_values_without_echoing_content(
    tmp_path: Path, field: str, credential: str
) -> None:
    pack = _write_pack(tmp_path)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if field in {"builder_versions", "exporter_versions"}:
        manifest[field]["brand_report_v3"] = credential
    else:
        manifest[field] = credential
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CapabilityPackError, match="manifest_sensitive_content") as error:
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")
    assert credential not in str(error.value)


def test_loader_permits_ordinary_token_and_endpoint_in_manifest_value(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["builder_versions"]["brand_report_v3"] = "token 与 endpoint 是普通说明文字"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert CapabilityPackLoader(tmp_path).load_manifest("marketing-v1").builder_versions[
        "brand_report_v3"
    ] == "token 与 endpoint 是普通说明文字"


def test_loader_rejects_unsupported_runtime_contract(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_contract_version"] = "marketing_runtime_v0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CapabilityPackError, match="manifest_runtime_contract_unsupported"):
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")


def test_loader_rejects_missing_duplicate_or_wrong_contracts(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_contracts"] = manifest["artifact_contracts"][:2]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CapabilityPackError, match="manifest_contracts_invalid"):
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")

    manifest["artifact_contracts"] = [
        *manifest["artifact_contracts"],
        manifest["artifact_contracts"][0],
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CapabilityPackError, match="manifest_contracts_invalid"):
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")


def test_loader_rejects_contract_path_digest_and_schema_mismatch(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_contracts"][0]["path"] = "../outside.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CapabilityPackError, match="pack_path_invalid"):
        CapabilityPackLoader(tmp_path).load_manifest("marketing-v1")

    pack = _write_pack(tmp_path / "digest")
    contract = pack / "contracts" / "brand_report_v3.json"
    contract.write_text('{"artifact_type":"brand_report_v3","schema_version":"9"}', encoding="utf-8")
    with pytest.raises(CapabilityPackError, match="pack_digest_mismatch"):
        CapabilityPackLoader(tmp_path / "digest").load_manifest("marketing-v1")

    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content = contract.read_text(encoding="utf-8")
    manifest["artifact_contracts"][0]["digest"] = _digest(content)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CapabilityPackError, match="manifest_contract_invalid"):
        CapabilityPackLoader(tmp_path / "digest").load_manifest("marketing-v1")
