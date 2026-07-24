import json
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.goals.evaluation import summarize_goal_planner_logs
from scripts import evaluate_goal_planner_shadow


_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _row(
    task_id: str | None,
    response: dict | None,
    status: str = "success",
    *,
    attempt: int = 1,
    current_message: str | None = None,
    log_id: str | None = None,
    duration_ms: int | None = 120,
):
    return SimpleNamespace(
        id=log_id or f"log-{task_id}-{status}-{attempt}",
        task_id=task_id,
        tags=["goal_planner:shadow", f"goal_planner:attempt:{attempt}"],
        messages=json.dumps(
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "current_message": current_message
                            or (
                                "分析喜茶 618 表现，并圈选下一轮达人"
                                if task_id == "task-1"
                                else "分析品牌表现"
                            ),
                            "session_context": {"active_brand": None},
                            "account_default_brand": None,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            ensure_ascii=False,
        ),
        status=status,
        error_code=None if status == "success" else "MODEL_PLAN_INVALID",
        response=json.dumps(response, ensure_ascii=False) if response is not None else None,
        duration_ms=duration_ms,
        created_at=None,
    )


def _clarify_response() -> dict:
    return {
        "action": "clarify",
        "active_brand": None,
        "brand_source": "none",
        "question": {"text": "请补充品牌", "options": []},
        "goals": [],
    }


def _campaign_then_selection_response() -> dict:
    return {
        "action": "execute",
        "active_brand": "喜茶",
        "brand_source": "explicit",
        "question": None,
        "goals": [
            {
                "sequence": 1,
                "goal_type": "campaign_analysis",
                "depends_on_sequence": None,
                "params": {
                    "brand": "喜茶",
                    "campaign": "618",
                    "period": None,
                    "platforms": [],
                    "requirement": "",
                },
                "request_evidence": "分析喜茶 618 表现",
            },
            {
                "sequence": 2,
                "goal_type": "kol_selection",
                "depends_on_sequence": 1,
                "params": {
                    "brand": "喜茶",
                    "campaign": "618",
                    "period": None,
                    "platforms": [],
                    "requirement": "",
                },
                "request_evidence": "圈选下一轮达人",
            },
        ],
    }


def test_summary_counts_actions_goal_types_brand_sources_and_failures() -> None:
    rows = [
        _row(
            "task-1",
            _campaign_then_selection_response(),
            attempt=2,
        ),
        _row(
            "task-1",
            _clarify_response(),
            attempt=1,
        ),
        _row("task-2", _clarify_response()),
        _row("task-3", None, status="invalid"),
    ]

    result = summarize_goal_planner_logs(rows)

    assert result["total"] == 3
    assert result["statuses"] == {"success": 2, "invalid": 1}
    assert result["actions"] == {"execute": 1, "clarify": 1}
    assert result["goal_types"] == {
        "campaign_analysis": 1,
        "kol_selection": 1,
    }
    assert result["brand_sources"] == {"explicit": 1, "none": 1}
    assert len(result["samples"]) == 3
    assert result["samples"][0]["current_message"] == (
        "分析喜茶 618 表现，并圈选下一轮达人"
    )


def test_summary_projects_contract_fields_and_redacts_sample_text() -> None:
    current_message = (
        "分析喜茶并圈选达人；api_key=sk-unit-test-key；"
        "token=unit-test-token；Authorization: Bearer unit-test-auth；"
        "secret=unit-test-secret；端点 https://provider.example.invalid/v1"
    )
    row = _row(
        "task-sensitive",
        {
            "action": "execute",
            "active_brand": "喜茶",
            "brand_source": "explicit",
            "question": {
                "text": "不要访问 https://question.example.invalid/private",
                "options": ["继续分析", "token=question-secret"],
                "prompt": "不应输出的问题 Prompt",
            },
            "goals": [
                {
                    "sequence": 1,
                    "goal_type": "kol_selection",
                    "depends_on_sequence": None,
                    "params": {
                        "brand": "喜茶",
                        "campaign": "618",
                        "period": {
                            "start": "2026-06-01",
                            "end": "2026-06-18",
                            "endpoint": "https://period.example.invalid",
                        },
                        "platforms": ["xiaohongshu"],
                        "requirement": "authorization=goal-secret",
                        "secret": "nested-secret",
                    },
                    "request_evidence": "分析喜茶并圈选达人，endpoint=internal-host",
                    "prompt": "不应输出的目标 Prompt",
                }
            ],
            "prompt": "不应输出的完整 Prompt",
            "secret": "response-secret",
            "endpoint": "https://response.example.invalid/v1",
        },
        status="invalid",
        current_message=current_message,
    )

    sample = summarize_goal_planner_logs([row])["samples"][0]
    response = sample["response"]

    assert set(response) == {
        "action",
        "goals",
        "active_brand",
        "brand_source",
        "question",
    }
    assert set(response["question"]) == {"text", "options"}
    assert set(response["goals"][0]) == {
        "sequence",
        "goal_type",
        "depends_on_sequence",
        "params",
        "request_evidence",
    }
    assert set(response["goals"][0]["params"]) == {
        "brand",
        "campaign",
        "period",
        "platforms",
        "requirement",
    }
    assert set(response["goals"][0]["params"]["period"]) == {"start", "end"}
    assert "分析喜茶并圈选达人" in sample["current_message"]
    assert "[REDACTED]" in sample["current_message"]

    encoded = json.dumps(sample, ensure_ascii=False)
    for forbidden in (
        "sk-unit-test-key",
        "unit-test-token",
        "unit-test-auth",
        "unit-test-secret",
        "provider.example.invalid",
        "question.example.invalid",
        "question-secret",
        "goal-secret",
        "nested-secret",
        "internal-host",
        "response-secret",
        "response.example.invalid",
        "不应输出",
    ):
        assert forbidden not in encoded


def test_summary_redacts_escaped_json_credentials_from_projected_text() -> None:
    credentials = json.dumps(
        {
            "password": 'password-prefix"password-tail\\password-end',
            "token": 'token-prefix"token-tail\\token-end',
            "api_key": 'key-prefix"key-tail\\key-end',
        },
        ensure_ascii=False,
    )
    row = _row(
        "task-escaped-sensitive",
        {
            "action": "execute",
            "goals": [
                {
                    "sequence": 1,
                    "goal_type": "brand_analysis",
                    "params": {
                        "brand": "喜茶",
                        "campaign": None,
                        "period": None,
                        "platforms": [],
                        "requirement": credentials,
                    },
                    "request_evidence": "分析喜茶",
                }
            ],
        },
        status="invalid",
        current_message=f"普通业务文本；调试载荷 {credentials}",
    )

    sample = summarize_goal_planner_logs([row])["samples"][0]
    encoded = json.dumps(sample, ensure_ascii=False)

    for leaked_suffix in (
        "password-tail",
        "password-end",
        "token-tail",
        "token-end",
        "key-tail",
        "key-end",
    ):
        assert leaked_suffix not in encoded
    assert "普通业务文本" in encoded


def test_task_and_log_grouping_namespaces_do_not_collide() -> None:
    rows = [
        _row(
            "shared-id",
            _clarify_response(),
            log_id="task-log",
        ),
        _row(
            None,
            _clarify_response(),
            log_id="shared-id",
        ),
    ]

    result = summarize_goal_planner_logs(rows)

    assert result["total"] == 2
    assert result["actions"] == {"clarify": 2}


def test_summary_revalidates_final_semantics_and_sums_all_attempt_duration() -> None:
    current_message = "分析活动中哪些达人表现最好"
    rows = [
        _row(
            "task-semantic-failure",
            {
                "action": "execute",
                "active_brand": None,
                "brand_source": "none",
                "question": None,
                "goals": [
                    {
                        "sequence": 1,
                        "goal_type": "kol_selection",
                        "depends_on_sequence": None,
                        "params": {
                            "brand": None,
                            "campaign": None,
                            "period": None,
                            "platforms": [],
                            "requirement": "",
                        },
                        "request_evidence": "达人表现",
                    }
                ],
            },
            attempt=2,
            current_message=current_message,
            duration_ms=240,
        ),
        _row(
            "task-semantic-failure",
            _clarify_response(),
            attempt=1,
            current_message=current_message,
            duration_ms=160,
        ),
    ]

    result = summarize_goal_planner_logs(rows)

    assert result["total"] == 1
    assert result["statuses"] == {"invalid": 1}
    assert result["actions"] == {}
    assert result["average_duration_ms"] == 400
    assert result["samples"][0]["status"] == "invalid"
    assert result["samples"][0]["error_code"] == "selection_intent_not_explicit"


def test_summary_applies_limit_after_task_grouping_and_keeps_unscoped_logs_separate() -> None:
    rows = [
        _row("new-task", _clarify_response(), attempt=2, duration_ms=200),
        _row("new-task", _clarify_response(), attempt=1, duration_ms=100),
        _row(None, _clarify_response(), log_id="unscoped-1", duration_ms=50),
        _row(None, _clarify_response(), log_id="unscoped-2", duration_ms=70),
        _row("old-task", _clarify_response(), duration_ms=90),
    ]

    result = summarize_goal_planner_logs(rows, limit=3)

    assert result["total"] == 3
    assert [sample["log_id"] for sample in result["samples"]] == [
        "log-new-task-success-2",
        "unscoped-1",
        "unscoped-2",
    ]
    assert result["average_duration_ms"] == round((300 + 50 + 70) / 3)


@pytest.mark.asyncio
async def test_cli_reads_two_attempts_per_task_with_stable_order_before_limit(
    monkeypatch,
    capsys,
) -> None:
    observed: dict[str, object] = {}
    rows = [
        _row("new-task", _clarify_response(), attempt=2, duration_ms=220),
        _row("new-task", _clarify_response(), attempt=1, duration_ms=110),
        _row("second-task", _clarify_response(), attempt=1, duration_ms=200),
    ]

    class ScalarsResult:
        def all(self):
            return rows

    class FakeDb:
        async def scalars(self, statement):
            observed["limit"] = statement._limit_clause.value
            observed["query"] = str(statement)
            return ScalarsResult()

    @asynccontextmanager
    async def fake_session_factory():
        yield FakeDb()

    monkeypatch.setattr(
        evaluate_goal_planner_shadow,
        "SessionFactory",
        fake_session_factory,
    )

    await evaluate_goal_planner_shadow.run(2)

    result = json.loads(capsys.readouterr().out)
    assert observed["limit"] == 4
    assert "model_prompt_logs.created_at DESC, model_prompt_logs.id DESC" in str(
        observed["query"]
    )
    assert result["total"] == 2
    assert result["average_duration_ms"] == round((330 + 200) / 2)


def test_cli_help_runs_as_documented_from_backend_root() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_goal_planner_shadow.py",
            "--help",
        ],
        cwd=_BACKEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--limit" in completed.stdout
