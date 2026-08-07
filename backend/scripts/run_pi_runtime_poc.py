"""方案 A 真实对比 CLI；不回显密钥、DSN、Prompt 或原始供应商 payload。"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.db.session import SessionFactory
from app.mcp_gateway.service import refresh_approved_datatap_tools
from app.pi_runtime_poc.auth import PiPocSettingsGuard
from app.pi_runtime_poc.comparison import (
    CurrentRuntimeCaseExecutor,
    PiRuntimeCaseExecutor,
    PocCaseFactory,
    assess_gate_a,
    begin_round,
    build_real_pi_client_factory,
    load_cases,
    write_round_summary,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="all")
    parser.add_argument("--runtime", choices=("current", "pi", "both"), default="both")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    PiPocSettingsGuard.assert_safe(settings)
    root = Path(__file__).parents[2]
    cases = load_cases(root / "backend" / "fixtures" / "pi_runtime_poc" / "cases.json")
    selected = cases if args.case == "all" else tuple(case for case in cases if case.case_id == args.case)
    if not selected:
        raise ValueError("poc_case_not_found")
    round_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root = root / "outputs" / "pi-runtime-poc"
    round_dir = output_root / round_id
    factory = PocCaseFactory(
        SessionFactory,
        round_id=round_id,
        model_name=settings.tencent_plan_model,
        current_wallet_balance=10_000,
    )
    results = []
    # Current Runtime 也只使用主配置指向的真实 DataTap 工具目录；发现失败即让本轮失败，
    # 不以 fixture/mock 继续。
    await refresh_approved_datatap_tools()
    from app.main import create_agent_runtime

    current_runtime, _, _, _, _ = create_agent_runtime()
    current_executor = CurrentRuntimeCaseExecutor(
        factory=factory,
        executor=current_runtime,
        session_factory=SessionFactory,
        output_root=round_dir,
    )
    pi_executor = PiRuntimeCaseExecutor(
        factory=factory,
        session_factory=SessionFactory,
        settings=settings,
        client_factory=build_real_pi_client_factory(settings),
        output_root=round_dir,
    )
    # 本地配置和两个 executor 都构建成功前绝不创建 round；阻断不应伪装成真实轮次。
    round_dir = begin_round(output_root, round_id)
    for index, case in enumerate(selected):
        order = (current_executor, pi_executor) if index % 2 == 0 else (pi_executor, current_executor)
        for executor in order:
            if args.runtime == "current" and executor is pi_executor:
                continue
            if args.runtime == "pi" and executor is current_executor:
                continue
            results.append(await executor.execute(case))
    gate = assess_gate_a(selected, tuple(results))
    summary = write_round_summary(round_dir, tuple(results), gate)
    print(f"round={round_id} summary={summary} gate={gate['gate']}")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
