"""只读取 Pi execution manifest 并一次性写入本地 Gate summary。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from app.pi_runtime_poc.gate import finalize_execution, write_summary_append_once

_SECRET_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9._-]+|Bearer\s+\S+)", re.IGNORECASE)


def _read_execution(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("runtime") != "pi" or not isinstance(payload.get("results"), list):
        raise ValueError("poc_execution_manifest_invalid")
    return payload


def _read_results(path: Path) -> tuple[dict[str, Any], ...]:
    return tuple(_read_execution(path)["results"])


def _secret_paths(round_dir: Path) -> list[str]:
    paths: list[str] = []
    for path in sorted(round_dir.rglob("*.json")):
        if _SECRET_PATTERN.search(path.read_text(encoding="utf-8")):
            paths.append(str(path.relative_to(round_dir)))
    return paths


def _output_root() -> Path:
    return Path(__file__).parents[2].resolve() / "outputs" / "pi-runtime-poc"


def finalize_round(round_dir: Path, fixture_path: Path, human_review_path: Path | None = None) -> Path:
    round_dir = round_dir.resolve()
    root = _output_root().resolve()
    summary_path = round_dir / "summary.json"
    if root not in round_dir.parents or summary_path.exists():
        raise FileExistsError(summary_path)

    execution = _read_execution(round_dir / "execution.json")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(fixture, list):
        raise TypeError("poc_cases_must_be_array")
    secret_paths = _secret_paths(round_dir)
    if secret_paths:
        raise ValueError("poc_output_contains_secret")
    review = (
        json.loads(human_review_path.read_text(encoding="utf-8"))
        if human_review_path is not None
        else None
    )
    summary = finalize_execution(execution, fixture, review)
    payload = {
        "round_id": round_dir.name,
        "results": execution["results"],
        "gate": {
            "gate": summary.gate,
            "hard_checks": summary.hard_checks,
            "secret_paths": [],
        },
    }
    return write_summary_append_once(summary_path, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--human-review", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    fixture = Path(__file__).parents[1] / "fixtures" / "pi_runtime_poc" / "cases.json"
    print(finalize_round(args.round, fixture, args.human_review))
