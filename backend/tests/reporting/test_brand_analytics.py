from app.reporting.brand_analytics import aggregate_brand_analytics


def test_brand_analytics_aggregates_platform_volume_and_sentiment_trend() -> None:
    result = aggregate_brand_analytics(
        [
            {
                "platform": "xiaohongshu",
                "platform_account_id": "brand",
                "analytics_fields": {
                    "brand_mentions": 12,
                    "published_at": "2026-06",
                    "sentiment_index": 0.8,
                },
            },
            {
                "platform": "douyin",
                "platform_account_id": "brand",
                "analytics_fields": {
                    "brand_mentions": 8,
                    "published_at": "2026-06",
                    "sentiment_index": 0.2,
                },
            },
        ]
    )

    assert result["overview"]["brand_volume"]["value"] == 20
    assert result["volume_trend"] == [
        {"period": "2026-06", "value": 20, "unit": "条", "platforms": ["douyin", "xiaohongshu"]}
    ]
    assert result["sentiment_trend"] == [
        {"period": "2026-06", "value": 0.5, "unit": "指数", "platforms": ["douyin", "xiaohongshu"]}
    ]


def test_empty_brand_analytics_is_explicitly_unavailable() -> None:
    result = aggregate_brand_analytics(())

    assert result["overview"]["brand_volume"]["available"] is False
    assert result["overview"]["brand_volume"]["value"] is None
    assert result["volume_trend"] == []
    assert result["data_availability"]["available"] is False
