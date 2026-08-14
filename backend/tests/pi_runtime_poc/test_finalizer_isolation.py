"""Task 6：finalizer 进程隔离与 append-once 门禁。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_finalizer_source_has_no_runtime_or_infrastructure_imports() -> None:
    source = (Path(__file__).parents[2] / "scripts" / "finalize_pi_runtime_poc.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("Settings", "sqlalchemy", "FastAPI", "export_artifact", "AgentRun"):
        assert forbidden not in source


def test_gate_imports_in_clean_subprocess_without_settings_or_database() -> None:
    backend = Path(__file__).parents[2]
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(backend)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.pi_runtime_poc.gate import HARD_CHECKS; print(len(HARD_CHECKS))",
        ],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10"


def test_finalizer_writes_summary_once_and_rejects_existing_target(tmp_path: Path) -> None:
    from app.pi_runtime_poc.gate import write_summary_append_once

    target = tmp_path / "summary.json"
    payload = {"gate": "PASS", "hard_checks": {"x": True}}
    assert write_summary_append_once(target, payload) == target
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    with pytest.raises(FileExistsError):
        write_summary_append_once(target, payload)
