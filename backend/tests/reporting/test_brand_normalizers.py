import json
from datetime import datetime

from app.reporting.normalizers import normalize_brand_evidence
from app.reporting.schemas import ToolEvidence


def test_brand_tool_result_projects_volume_sentiment_and_platform() -> None:
    evidence = ToolEvidence(
        "datatap.insight.query.analysis.v1",
        {
            "result": json.dumps(
                {"data": [{"平台": "小红书", "月份": "2026-06", "声量": 12, "情感指数": 0.8}]},
                ensure_ascii=False,
            )
        },
        "call-1",
        datetime(2026, 7, 17),
    )

    rows = normalize_brand_evidence((evidence,))

    assert len(rows) == 1
    assert rows[0].platform == "xiaohongshu"
    assert rows[0].analytics_fields["brand_mentions"] == 12
    assert rows[0].analytics_fields["published_at"] == "2026-06"
    assert rows[0].analytics_fields["sentiment_index"] == 0.8


def test_invalid_or_empty_brand_result_is_not_exposed_as_raw_data() -> None:
    evidence = ToolEvidence(
        "datatap.insight.social.statistic.trend.v1",
        {"result": "not-json"},
        "call-2",
        datetime(2026, 7, 17),
    )

    rows = normalize_brand_evidence((evidence,))

    assert rows == ()
