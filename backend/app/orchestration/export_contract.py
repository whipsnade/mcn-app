from __future__ import annotations

from app.orchestration.schemas import ExportFieldContract, SessionBrief


EXPORT_FIELD_CONTRACT_VERSION = "kol_excel_v1"


def build_export_field_contract(brief: SessionBrief) -> ExportFieldContract:
    locations = brief.filters.get("target_fan_locations", [])
    location_label = ",".join(str(value) for value in locations) if isinstance(locations, list) else ""
    location_label = location_label or "目标地区"
    age_label = brief.target_audience or "目标年龄段"
    industry_label = brief.category or "行业"
    return ExportFieldContract(
        version=EXPORT_FIELD_CONTRACT_VERSION,
        required_field_names=(
            "platform",
            "nickname",
            "followers",
            "city",
            f"{industry_label}兴趣占比",
            f"{location_label}粉丝占比",
            f"{age_label}占比",
            "engagement_rate",
            "active_follower_rate",
            "content_tags",
            "score",
        ),
        labels={
            "platform": "平台",
            "nickname": "昵称",
            "followers": "粉丝数",
            "city": "城市",
            "industry_interest": f"{industry_label}兴趣",
            "target_region": f"{location_label}粉丝",
            "target_age": age_label,
        },
        notes=(
            "不得编造缺失数据",
            "缺失字段显示数据缺失",
            "每个选中平台必须执行检索",
            "抖音平台口径按可获得的获赞、评论、收藏等指标计算",
        ),
    )


__all__ = ["EXPORT_FIELD_CONTRACT_VERSION", "build_export_field_contract"]
