from __future__ import annotations

from app.orchestration.schemas import AnalyticsFieldContract


ANALYTICS_FIELD_CONTRACT_VERSION = "bi_analytics_v1"


def build_analytics_field_contract() -> AnalyticsFieldContract:
    return AnalyticsFieldContract(
        version=ANALYTICS_FIELD_CONTRACT_VERSION,
        field_names=(
            "brand_mentions",
            "exposure",
            "interactions",
            "published_at",
            "sentiment_counts",
            "hot_words",
            "audience_age",
            "audience_gender",
            "audience_regions",
        ),
        labels={
            "brand_mentions": "品牌提及量",
            "exposure": "曝光量",
            "interactions": "互动量",
            "published_at": "发布时间",
            "sentiment_counts": "情感倾向计数",
            "hot_words": "热点词",
            "audience_age": "受众年龄分布",
            "audience_gender": "受众性别分布",
            "audience_regions": "受众地域分布",
        },
        notes=(
            "每个平台都应尽量获取这些 BI 字段",
            "无法获取的字段必须标记为缺失，不得猜测或编造",
            "只使用已审核工具返回且可安全规范化的数据",
        ),
    )


__all__ = ["ANALYTICS_FIELD_CONTRACT_VERSION", "build_analytics_field_contract"]
