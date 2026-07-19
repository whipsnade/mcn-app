from datetime import date

from app.orchestration.routing import extract_requested_period


def test_requested_period_defaults_to_last_three_months() -> None:
    period = extract_requested_period("分析美妆行业在社交媒体的讨论热度")

    assert period["unit"] == "month"
    assert period["value"] == 3
    assert period["end"] == date.today().isoformat()


def test_requested_period_parses_explicit_day_window() -> None:
    period = extract_requested_period("找最近30天活跃的达人")

    assert period["unit"] == "day"
    assert period["value"] == 30


def test_requested_period_converts_quarters_and_years_to_months() -> None:
    quarter = extract_requested_period("最近1季度的声量变化")
    year = extract_requested_period("近2年的趋势")

    assert (quarter["unit"], quarter["value"]) == ("month", 3)
    assert (year["unit"], year["value"]) == ("month", 24)
