from types import SimpleNamespace

from app.reporting.service import ReportingService


def _candidate(index: int):
    kol = SimpleNamespace(platform_account_id=f"account-{index}")
    row = SimpleNamespace(platform="xiaohongshu" if index % 2 == 0 else "douyin")
    score = SimpleNamespace(
        total=100 - index,
        dimensions={
            "audience": SimpleNamespace(raw_score=80 - index),
            "engagement": SimpleNamespace(raw_score=70 - index),
        },
    )
    return kol, None, row, score


def test_rank_candidate_draft_keeps_full_pool_and_shortlist_marks_top10() -> None:
    ranked = ReportingService._rank_candidates([_candidate(index) for index in range(12)])

    assert len(ranked) == 12
    assert [item[3].total for item in ranked] == list(range(100, 88, -1))
    assert [index < 10 for index, _item in enumerate(ranked)] == [True] * 10 + [False] * 2
