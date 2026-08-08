"""只读 Pi-only execution.json 并一次性生成 Gate A summary。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.pi_runtime_poc.comparison import (
    _SECRET_PATTERN,
    HumanReview,
    PocCaseResult,
    assess_gate_a,
    load_cases,
    write_round_summary,
)


def _read_results(path: Path) -> tuple[PocCaseResult, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("runtime") != "pi" or not isinstance(payload.get("results"), list):
        raise ValueError("poc_execution_manifest_invalid")
    return tuple(PocCaseResult(**item) for item in payload["results"])


def _secret_paths(round_dir: Path) -> list[str]:
    return [
        str(path.relative_to(round_dir))
        for path in sorted(round_dir.rglob("*.json"))
        if _SECRET_PATTERN.search(path.read_text(encoding="utf-8"))
    ]


def _output_root() -> Path:
    return Path(__file__).parents[2].resolve() / "outputs" / "pi-runtime-poc"


def finalize_round(round_dir: Path, fixture_path: Path, human_review_path: Path | None = None) -> Path:
    round_dir = round_dir.resolve()
    root = _output_root().resolve()
    if root not in round_dir.parents or (round_dir / "summary.json").exists():
        raise FileExistsError(round_dir / "summary.json")
    results = _read_results(round_dir / "execution.json")
    cases = load_cases(fixture_path)
    secret_paths = _secret_paths(round_dir)
    if secret_paths:
        raise ValueError("poc_output_contains_secret")
    review = (
        HumanReview.model_validate_json(human_review_path.read_text(encoding="utf-8"))
        if human_review_path is not None
        else None
    )
    gate = assess_gate_a(cases, results, review)
    gate["secret_paths"] = []
    return write_round_summary(round_dir, results, gate)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--human-review", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    fixture = Path(__file__).parents[1] / "fixtures" / "pi_runtime_poc" / "cases.json"
    print(finalize_round(args.round, fixture, args.human_review))
