"""真实 Pi 对比入口默认跳过；仅 Task 9 的安全脚本显式开启。"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_SERVICES") != "1",
    reason="真实 Pi POC 仅由 scripts/run_pi_runtime_poc.sh 在 Gate A 执行",
)


def test_real_pi_poc_requires_exact_isolated_database() -> None:
    assert os.environ.get("APP_ENV") == "test"
    assert os.environ.get("MYSQL_DATABASE") == "kol_insight_pi_poc"
    assert os.environ.get("PI_RUNTIME_POC_ENABLED") == "true"
