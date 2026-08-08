"""Pi-only 六场景 Gate A 执行入口；不回显密钥、Prompt 或供应商原始响应。"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.db.session import SessionFactory
from app.pi_runtime_poc.auth import PiPocSettingsGuard
from app.pi_runtime_poc.comparison import (
    PiRuntimeCaseExecutor,
    PocCase,
    PocCaseFactory,
    PocCaseResult,
    RuntimeCaseExecutor,
    begin_round,
    build_real_pi_client_factory,
    load_cases,
    make_non_executed_result,
    write_case_result,
    write_execution_manifest,
)

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("all",), default="all")
    parser.add_argument("--runtime", choices=("pi",), default="pi")
    return parser.parse_args()


async def run_selected_cases(
    cases: tuple[PocCase, ...],
    executor: RuntimeCaseExecutor,
    round_dir: Path,
) -> tuple[PocCaseResult, ...]:
    """隔离执行每个案例；品牌失败只使依赖钻取跳过。"""
    results: list[PocCaseResult] = []
    published_runs: dict[str, str] = {}
    for case in cases:
        prior_run_id = published_runs.get(case.depends_on_case_id or "")
        if case.depends_on_case_id is not None and prior_run_id is None:
            result = make_non_executed_result(
                round_dir,
                case,
                status="skipped_dependency",
                error_code="poc_dependency_artifact_unavailable",
            )
        else:
            try:
                result = await executor.execute(case, prior_run_id=prior_run_id)
            except Exception:
                logger.exception("pi_poc_case_failed case_id=%s", case.case_id)
                result = make_non_executed_result(
                    round_dir,
                    case,
                    status="failed",
                    error_code="poc_case_execution_failed",
                )
        write_case_result(round_dir, result)
        results.append(result)
        if result.run_id is not None and result.artifact_versions:
            published_runs[case.case_id] = result.run_id
    return tuple(results)


async def main() -> int:
    parse_args()
    settings = get_settings()
    PiPocSettingsGuard.assert_safe(settings)
    root = Path(__file__).parents[2]
    cases = load_cases(root / "backend" / "fixtures" / "pi_runtime_poc" / "cases.json")
    round_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root = root / "outputs" / "pi-runtime-poc"
    factory = PocCaseFactory(
        SessionFactory,
        round_id=round_id,
        model_name=settings.tencent_plan_model,
    )
    pi_executor = PiRuntimeCaseExecutor(
        factory=factory,
        session_factory=SessionFactory,
        settings=settings,
        client_factory=build_real_pi_client_factory(settings),
        output_root=output_root / round_id,
    )
    # 所有本地配置和 executor 构造成功后才创建不可变 round 目录。
    round_dir = begin_round(output_root, round_id)
    results: tuple[PocCaseResult, ...] = ()
    try:
        results = await run_selected_cases(cases, pi_executor, round_dir)
    finally:
        by_case = {result.case_id: result for result in results}
        completed: list[PocCaseResult] = []
        for case in cases:
            result = by_case.get(case.case_id)
            if result is None:
                result = make_non_executed_result(
                    round_dir,
                    case,
                    status="not_run",
                    error_code="poc_round_aborted_before_case",
                )
                write_case_result(round_dir, result)
            completed.append(result)
        execution = write_execution_manifest(round_dir, cases, tuple(completed))
    print(f"round={round_id} execution={execution}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
