from types import SimpleNamespace

from app.reporting.service import ReportingService


def _candidate(index: int):
    platform = "xiaohongshu" if index % 2 == 0 else "douyin"
    kol = SimpleNamespace(platform_account_id=f"account-{index}")
    row = SimpleNamespace(platform=platform)
    score = SimpleNamespace(
        total=100 - index,
        dimensions={
            "audience": SimpleNamespace(raw_score=80 - index),
            "engagement": SimpleNamespace(raw_score=70 - index),
        },
    )
    return kol, None, row, score


def test_combined_platform_pool_is_ranked_then_limited_to_final_top10() -> None:
    selector = getattr(ReportingService, "_select_top_candidates", None)

    assert callable(selector)
    selected = selector([_candidate(index) for index in range(12)])

    assert len(selected) == 10
    assert [item[3].total for item in selected] == list(range(100, 90, -1))
    assert {item[2].platform for item in selected} == {"xiaohongshu", "douyin"}
