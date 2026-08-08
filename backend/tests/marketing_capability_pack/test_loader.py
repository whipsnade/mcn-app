import hashlib
import json
from pathlib import Path

import pytest

from app.marketing_capability_pack.loader import CapabilityPackError, CapabilityPackLoader


def _write_pack(root: Path, *, include_secret: bool = False) -> Path:
    pack = root / "marketing-v1"
    (pack / "policies").mkdir(parents=True)
    (pack / "skills" / "brand-research-report").mkdir(parents=True)
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
        "artifact_contracts": [{"artifact_type": "brand_report_v3", "schema_version": "3"}],
        "builder_versions": {"brand_report_v3": "1.0.0"},
        "exporter_versions": {"brand_report_v3": "1.0.0"},
    }
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
