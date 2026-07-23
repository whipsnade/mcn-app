import json
from types import SimpleNamespace

from app.goals.evaluation import summarize_goal_planner_logs


def _row(
    task_id: str | None,
    response: dict | None,
    status: str = "success",
    *,
    attempt: int = 1,
    current_message: str | None = None,
    log_id: str | None = None,
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
                            )
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
        duration_ms=120,
        created_at=None,
    )


def test_summary_counts_actions_goal_types_brand_sources_and_failures() -> None:
    rows = [
        _row(
            "task-1",
            {
                "action": "execute",
                "brand_source": "explicit",
                "goals": [
                    {"goal_type": "campaign_analysis"},
                    {"goal_type": "kol_selection"},
                ],
            },
            attempt=2,
        ),
        _row(
            "task-1",
            {
                "action": "execute",
                "brand_source": "explicit",
                "goals": [{"goal_type": "kol_selection"}],
            },
            attempt=1,
        ),
        _row(
            "task-2",
            {
                "action": "clarify",
                "brand_source": "none",
                "goals": [],
            }
        ),
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


def test_task_and_log_grouping_namespaces_do_not_collide() -> None:
    rows = [
        _row(
            "shared-id",
            {"action": "execute", "goals": [{"goal_type": "kol_selection"}]},
            log_id="task-log",
        ),
        _row(
            None,
            {"action": "clarify", "goals": []},
            log_id="shared-id",
        ),
    ]

    result = summarize_goal_planner_logs(rows)

    assert result["total"] == 2
    assert result["actions"] == {"execute": 1, "clarify": 1}
