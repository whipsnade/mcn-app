import json
from types import SimpleNamespace

from app.goals.evaluation import summarize_goal_planner_logs


def _row(
    task_id: str,
    response: dict | None,
    status: str = "success",
    *,
    attempt: int = 1,
):
    return SimpleNamespace(
        id=f"log-{task_id}-{status}-{attempt}",
        task_id=task_id,
        tags=["goal_planner:shadow", f"goal_planner:attempt:{attempt}"],
        messages=json.dumps(
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "current_message": (
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
