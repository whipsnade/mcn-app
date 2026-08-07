"""方案 A 六场景对比的纯数据契约与 Gate 汇总。

真实 Runtime 调用由 CLI 注入执行器；本模块不读取凭证、不调用模型或 DataTap，且所有输出
均写入新的 round 目录，避免覆盖既有真实供应商结果。
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact
from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.executor import SESSION_ANALYST_PROFILE, AgentRunExecutor
from app.agent_runtime.models import AgentRun, AgentSession, AgentToolCall, EvidenceItem
from app.core.config import Settings
from app.identity.models import User
from app.pi_runtime_poc.rpc import PiRpcClient, PiRpcConfig
from app.pi_runtime_poc.runner import PiClientFactory, PiPocRunner

RuntimeName = Literal["current", "pi"]
_SECRET_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9._-]+|Bearer\s+\S+)", re.IGNORECASE)
_REPORT_BEHAVIOR = "report"
_DATATAP_SERVICE_SLUGS = frozenset(
    {
        "insight-cube-mcp",
        "social-grow-mcp",
        "social-grow-content-mcp",
        "bilibili-mcp",
    }
)


@dataclass(frozen=True)
class PocCase:
    case_id: str
    user_question: str
    date_anchor: str
    expected_behavior: Literal["report", "drilldown", "clarify", "refuse"]
    required_artifact_type: str | None


@dataclass(frozen=True)
class PocCaseResult:
    case_id: str
    runtime: RuntimeName
    run_id: str
    outcome: str
    artifact_versions: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    metrics: dict[str, float | int | bool | None]
    diagnostic_path: str
    hard_checks: dict[str, bool]


class RuntimeCaseExecutor(Protocol):
    async def execute(self, case: PocCase) -> PocCaseResult: ...


class PocCaseFactory:
    """为每个 runtime/case 建立独立、可审计且同内容的 POC Run。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, round_id: str) -> None:
        self._session_factory = session_factory
        self._round_id = round_id

    async def create(self, case: PocCase, runtime: RuntimeName) -> str:
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._session_factory() as db:
            user = User(
                id=str(uuid4()),
                nickname=f"pi-poc-{runtime}",
                role="user",
                status="active",
                industries=["美食"],
                created_at=now,
                updated_at=now,
            )
            session = AgentSession(
                id=str(uuid4()),
                user_id=user.id,
                title=f"Pi POC {case.case_id}",
                status="active",
                session_summary=f"date_anchor={case.date_anchor}; case_id={case.case_id}",
                summary_version=1,
                created_at=now,
                updated_at=now,
            )
            run = AgentRun(
                id=str(uuid4()),
                session_id=session.id,
                user_id=user.id,
                run_kind="user",
                visibility="user",
                profile_name=SESSION_ANALYST_PROFILE,
                profile_version="1",
                model="runtime-config-snapshot",
                status="queued",
                prompt_snapshot_json={
                    "pi_runtime_poc": {
                        "round_id": self._round_id,
                        "case_id": case.case_id,
                        "runtime": runtime,
                        "date_anchor": case.date_anchor,
                    }
                },
                created_at=now,
            )
            from app.agent_runtime.models import AgentMessage

            message = AgentMessage(
                id=str(uuid4()),
                session_id=session.id,
                run_id=run.id,
                role="user",
                content=case.user_question,
                sequence=1,
                created_at=now,
            )
            run.input_message_id = message.id
            db.add_all((user, session, run, message))
            await db.commit()
            return run.id


class CurrentRuntimeCaseExecutor:
    def __init__(
        self,
        *,
        factory: PocCaseFactory,
        executor: AgentRunExecutor,
        session_factory: async_sessionmaker[AsyncSession],
        output_root: Path,
    ) -> None:
        self._factory = factory
        self._executor = executor
        self._session_factory = session_factory
        self._output_root = output_root

    async def execute(self, case: PocCase) -> PocCaseResult:
        run_id = await self._factory.create(case, "current")
        outcome = await self._executor.process_run(run_id)
        result = await _collect_case_result(
            self._session_factory, case, "current", run_id, outcome, self._output_root
        )
        write_case_result(self._output_root, result)
        return result


class PiRuntimeCaseExecutor:
    def __init__(
        self,
        *,
        factory: PocCaseFactory,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        client_factory: PiClientFactory,
        output_root: Path,
        worker_id: str = "pi-poc",
    ) -> None:
        self._factory = factory
        self._session_factory = session_factory
        self._settings = settings
        self._client_factory = client_factory
        self._output_root = output_root
        self._worker_id = worker_id

    async def execute(self, case: PocCase) -> PocCaseResult:
        run_id = await self._factory.create(case, "pi")
        async with self._session_factory() as db:
            runner = PiPocRunner(
                db=db,
                events=AgentEventStream(db, AgentEventBroker()),
                settings=self._settings,
                worker_id=self._worker_id,
                client_factory=self._client_factory,
            )
            outcome = await runner.run(run_id)
        result = await _collect_case_result(
            self._session_factory, case, "pi", run_id, outcome, self._output_root
        )
        write_case_result(self._output_root, result)
        return result


def load_cases(path: Path) -> tuple[PocCase, ...]:
    """加载版本化、无供应商结果的 fixture。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("poc_cases_must_be_array")
    cases = tuple(PocCase(**item) for item in raw)
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("poc_case_id_duplicate")
    return cases


def assess_gate_a(cases: tuple[PocCase, ...], results: tuple[PocCaseResult, ...]) -> dict[str, object]:
    """以硬门槛优先计算 Gate A；人工可读性仍留给 Task 9 人工填写。"""
    by_case: dict[str, dict[RuntimeName, PocCaseResult]] = {}
    for result in results:
        by_case.setdefault(result.case_id, {})[result.runtime] = result

    hard_check_values: dict[str, list[bool]] = {}
    for result in results:
        for key, value in result.hard_checks.items():
            hard_check_values.setdefault(key, []).append(value)
    hard_checks = {key: bool(values) and all(values) for key, values in hard_check_values.items()}

    behavior_checks: list[bool] = []
    report_checks: list[bool] = []
    for case in cases:
        pair = by_case.get(case.case_id, {})
        if set(pair) != {"current", "pi"}:
            behavior_checks.append(False)
            if case.expected_behavior == _REPORT_BEHAVIOR:
                report_checks.append(False)
            continue
        for result in pair.values():
            if case.expected_behavior == "clarify":
                behavior_checks.append(
                    result.outcome == "clarification_requested" and not result.artifact_versions
                )
            elif case.expected_behavior == "refuse":
                behavior_checks.append(result.outcome == "completed" and not result.artifact_versions)
            else:
                behavior_checks.append(result.outcome in {"completed", "completed_with_warnings"})
        if case.expected_behavior == _REPORT_BEHAVIOR:
            report_checks.append(all(bool(result.artifact_versions) for result in pair.values()))

    for key, value in {
        "three_reports_published": len(report_checks) == 3 and all(report_checks),
        "behavior_correct": bool(behavior_checks) and all(behavior_checks),
    }.items():
        hard_checks[key] = value

    coverage_not_lower = all(
        pair["pi"].metrics.get("coverage", 0) >= pair["current"].metrics.get("coverage", 0)
        for pair in by_case.values()
        if set(pair) == {"current", "pi"}
    ) and len(by_case) == len(cases)
    hard_checks["coverage_not_lower"] = coverage_not_lower

    improved = 0
    for metric in ("mcp_parameter_validity", "artifact_completeness", "human_readability"):
        current_values = [
            pair["current"].metrics.get(metric)
            for pair in by_case.values()
            if set(pair) == {"current", "pi"}
        ]
        pi_values = [
            pair["pi"].metrics.get(metric)
            for pair in by_case.values()
            if set(pair) == {"current", "pi"}
        ]
        if (
            current_values
            and all(isinstance(value, (int, float)) for value in current_values)
            and all(isinstance(value, (int, float)) for value in pi_values)
            and sum(pi_values) / len(pi_values) > sum(current_values) / len(current_values)
        ):
            improved += 1
    passed = bool(hard_checks) and all(hard_checks.values()) and improved >= 2
    return {"gate": "PASS" if passed else "FAIL", "hard_checks": hard_checks, "improved_metric_count": improved}


def write_append_only_round(
    output_root: Path, round_id: str, results: tuple[PocCaseResult, ...]
) -> Path:
    """写入新 round 的安全汇总；重复 round 或疑似密钥一律拒绝。"""
    if not round_id or Path(round_id).name != round_id:
        raise ValueError("invalid_poc_round_id")
    round_dir = output_root / round_id
    if round_dir.exists():
        raise FileExistsError(round_dir)
    payload = {"round_id": round_id, "results": [asdict(result) for result in results]}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if _SECRET_PATTERN.search(serialized):
        raise ValueError("poc_output_contains_secret")
    round_dir.mkdir(parents=True, exist_ok=False)
    summary = round_dir / "summary.json"
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def begin_round(output_root: Path, round_id: str) -> Path:
    """建立唯一真实轮次目录；已有目录一律不复用或覆盖。"""
    if not round_id or Path(round_id).name != round_id:
        raise ValueError("invalid_poc_round_id")
    round_dir = output_root / round_id
    round_dir.mkdir(parents=True, exist_ok=False)
    return round_dir


def write_round_summary(round_dir: Path, results: tuple[PocCaseResult, ...], gate: dict[str, object]) -> Path:
    """写入单轮汇总；只允许在已由 ``begin_round`` 建立的新轮次中执行一次。"""
    summary = round_dir / "summary.json"
    if not round_dir.is_dir() or summary.exists():
        raise FileExistsError(summary)
    payload = {"round_id": round_dir.name, "results": [asdict(result) for result in results], "gate": gate}
    _write_safe_json(summary, payload)
    return summary


def write_case_result(round_dir: Path, result: PocCaseResult) -> Path:
    path = round_dir / result.case_id / f"{result.runtime}.json"
    if not round_dir.is_dir() or path.exists():
        raise FileExistsError(path)
    _write_safe_json(path, asdict(result))
    return path


def _write_safe_json(path: Path, payload: object) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if _SECRET_PATTERN.search(serialized):
        raise ValueError("poc_output_contains_secret")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_real_pi_client_factory(settings: Settings) -> Callable[[AgentRun, str], object]:
    """构造真实 Pi 子进程工厂，仅传入主 Runtime 的同一模型和 DataTap 配置。

    调用者必须先以真实配置初始化 ``Settings``，并将数据库显式覆写为 POC 库。
    任一缺失项直接失败，不以 mock 或替代模型继续。
    """
    if set(settings.datatap_mcp_urls) != _DATATAP_SERVICE_SLUGS:
        raise RuntimeError("pi_poc_datatap_endpoint_mapping_required")
    thinking = settings.tencent_plan_reasoning_effort
    if thinking is None:
        raise RuntimeError("pi_poc_same_thinking_required")
    root = Path(__file__).parents[3]
    pi_root = root / "pi-runtime"
    executable = pi_root / "node_modules" / ".bin" / "pi"
    extension = pi_root / "src" / "extensions" / "poc-runtime.ts"
    skills = tuple(str(path) for path in sorted((pi_root / "skills").glob("*/SKILL.md")))
    if not executable.is_file() or not extension.is_file() or len(skills) != 6:
        raise RuntimeError("pi_poc_runtime_resources_missing")
    provider = "kol_insight_pi_poc"
    models_json = json.dumps(
        {
            "providers": {
                provider: {
                    "baseUrl": str(settings.tencent_plan_base_url),
                    "apiKey": "$TENCENT_PLAN_API_KEY",
                    "authHeader": True,
                    "api": "openai-completions",
                    "models": [
                        {
                            "id": settings.tencent_plan_model,
                            "reasoning": True,
                            "thinkingLevelMap": {thinking: thinking},
                            "input": ["text"],
                            "compat": {
                                "supportsReasoningEffort": True,
                                "thinkingFormat": "reasoning_effort",
                            },
                        }
                    ],
                }
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    def factory(run: AgentRun, token: str) -> object:
        environment = {
            "TENCENT_PLAN_API_KEY": settings.tencent_plan_api_key.get_secret_value(),
            "DATATAP_MCP_TOKEN": settings.datatap_mcp_token.get_secret_value(),
            "DATATAP_MCP_ENDPOINTS_JSON": json.dumps(
                {slug: str(settings.datatap_mcp_urls[slug]) for slug in sorted(_DATATAP_SERVICE_SLUGS)},
                separators=(",", ":"),
            ),
            "PI_RUNTIME_POC_BASE_URL": str(settings.pi_runtime_poc_base_url),
            "PI_RUNTIME_POC_RUN_ID": run.id,
            "PI_RUNTIME_POC_TOKEN": token,
        }
        return PiRpcClient.start(
            PiRpcConfig(
                executable=str(executable),
                extensions=(str(extension),),
                skills=skills,
                timeout_seconds=settings.pi_runtime_poc_run_timeout_seconds,
                environment=environment,
                agent_files={"models.json": models_json},
                provider=provider,
                model=settings.tencent_plan_model,
                thinking=thinking,
                cwd=str(pi_root),
            )
        )

    return factory


async def _collect_case_result(
    session_factory: async_sessionmaker[AsyncSession],
    case: PocCase,
    runtime: RuntimeName,
    run_id: str,
    outcome: object,
    output_root: Path,
) -> PocCaseResult:
    """从 POC DB 唯一事实来源读取结果；不读取 Pi stdout 或供应商原文。"""
    async with session_factory() as db:
        run = await db.get(AgentRun, run_id)
        if run is None:
            raise LookupError("poc_run_not_found")
        version_rows = (
            await db.execute(
                select(AgentArtifactVersion, AgentArtifact)
                .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
                .where(AgentArtifactVersion.source_run_id == run_id)
            )
        ).all()
        versions = tuple(version.id for version, _artifact in version_rows)
        evidence = tuple(
            (
                await db.scalars(
                    select(EvidenceItem.id).where(EvidenceItem.run_id == run_id)
                )
            ).all()
        )
        call_total = await db.scalar(
            select(func.count()).select_from(AgentToolCall).where(AgentToolCall.run_id == run_id)
        )
        call_settled = await db.scalar(
            select(func.count())
            .select_from(AgentToolCall)
            .where(AgentToolCall.run_id == run_id, AgentToolCall.status == "settled")
        )
        value = outcome.value if hasattr(outcome, "value") else outcome
        outcome_text = str(value if value is not None else run.status)
        report_case = case.expected_behavior == _REPORT_BEHAVIOR
        expected_versions = (
            version_rows
            if case.required_artifact_type is None
            else [row for row in version_rows if row[1].artifact_type == case.required_artifact_type]
        )
        excel_openable = all(_is_openable_excel(version) for version, _artifact in expected_versions)
        hard_checks = {
            "no_secret": True,
            "lineage_complete": all(
                isinstance(version.lineage_snapshot_json, dict)
                and isinstance(version.validation_json, dict)
                and version.validation_json.get("valid") is True
                for version, _artifact in version_rows
            ),
            "expected_artifact": (not report_case) or bool(expected_versions),
            "excel_openable": (not report_case) or (bool(expected_versions) and excel_openable),
            "bi_payload_same_version": (not report_case)
            or all(isinstance(version.payload_json, dict) for version, _artifact in expected_versions),
        }
        return PocCaseResult(
            case_id=case.case_id,
            runtime=runtime,
            run_id=run_id,
            outcome=outcome_text,
            artifact_versions=versions,
            evidence_ids=evidence,
            metrics={
                "coverage": len(evidence),
                "mcp_parameter_validity": (call_settled or 0) / (call_total or 1),
                "artifact_completeness": int(bool(versions)) if report_case else 1,
                "human_readability": None,
            },
            diagnostic_path=str(output_root / case.case_id / f"{runtime}.json"),
            hard_checks=hard_checks,
        )


def _is_openable_excel(version: AgentArtifactVersion) -> bool:
    """只做 openpyxl 的结构验证，且只从该不可变 Version 渲染。"""
    try:
        workbook = load_workbook(BytesIO(export_artifact(version)), data_only=False)
    except (ArtifactExportUnsupported, OSError, ValueError, TypeError):
        return False
    return bool(workbook.sheetnames)


__all__ = [
    "CurrentRuntimeCaseExecutor",
    "PiRuntimeCaseExecutor",
    "PocCase",
    "PocCaseFactory",
    "PocCaseResult",
    "RuntimeCaseExecutor",
    "RuntimeName",
    "assess_gate_a",
    "begin_round",
    "build_real_pi_client_factory",
    "load_cases",
    "write_append_only_round",
    "write_case_result",
    "write_round_summary",
]
