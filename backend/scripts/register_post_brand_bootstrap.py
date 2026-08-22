"""注册 post-brand bootstrap bundle：计算 digest、写自校验字段并同步 manifest。

幂等：重复运行产生相同结果。输出只包含登记名与 digest，绝不打印 Skill 正文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path


def _normalize_content(content: str) -> str:
    return unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))


def canonical_skill_digest(content: str) -> str:
    return hashlib.sha256(_normalize_content(content).encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bundle_body_digest(bundle: dict) -> str:
    body = {key: value for key, value in bundle.items() if key != "bundle_digest"}
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def register(bundle_path: Path, manifest_path: Path) -> int:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if bundle.get("schema_version") != "post_brand_bootstrap_v1":
        print("schema_version_invalid", file=sys.stderr)
        return 1
    for revision in bundle.get("revisions", []):
        revision["content_digest"] = canonical_skill_digest(revision["content"])
    bundle["bundle_digest"] = _bundle_body_digest(bundle)
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # manifest 登记相对 pack 根的路径；digest 为最终 bundle 文件的原始字节
    # sha256（与 loader._read_checked 同口径）。
    try:
        relative = bundle_path.resolve().relative_to(manifest_path.resolve().parent)
        bundle_ref = str(relative)
    except ValueError:
        bundle_ref = str(bundle_path)
    file_digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    entry = {"name": bundle["name"], "path": bundle_ref, "digest": file_digest}
    entries = [
        item
        for item in manifest.get("bootstrap_bundles", [])
        if not (isinstance(item, dict) and item.get("name") == entry["name"])
    ]
    entries.append(entry)
    entries.sort(key=lambda item: item["name"])
    manifest["bootstrap_bundles"] = entries
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"registered name={entry['name']} file_digest={entry['digest']} bundle_digest={bundle['bundle_digest']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    bundle_path = Path(args.bundle)
    if not bundle_path.is_absolute():
        bundle_path = Path(__file__).resolve().parent.parent / args.bundle
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = Path(__file__).resolve().parent.parent / args.manifest
    return register(bundle_path, manifest_path)


if __name__ == "__main__":
    sys.exit(main())
