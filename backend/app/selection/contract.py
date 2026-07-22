"""KOL 圈选 Excel 导出字段契约：随会话画像动态生成，注入 agent 循环上下文。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.workspace.models import WorkspaceSession

EXPORT_FIELD_CONTRACT_VERSION = "kol_excel_v2"


class ExportFieldContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    required_field_names: tuple[str, ...]
    labels: dict[str, str] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()


def build_export_field_contract(workspace: WorkspaceSession) -> ExportFieldContract:
    filters = workspace.filters_snapshot or {}
    # 地区标签读取顺序：brainstorm 画像 region → 旧键 target_fan_locations（list join）→ 兜底。
    profile = filters.get("brainstorm_profile") or {}
    region = ""
    if isinstance(profile, dict):
        region = str(profile.get("region") or "").strip()
    if not region:
        locations = filters.get("target_fan_locations", [])
        if isinstance(locations, list):
            region = ",".join(str(value) for value in locations)
    location_label = region or "目标地区"
    # target_audience/category 是自由文本：strip 后空串走兜底；年龄段截断 20 字符，
    # 避免整段受众描述被当成导出字段名。
    age_label = (workspace.target_audience or "").strip()[:20] or "目标年龄段"
    industry_label = (workspace.category or "").strip() or "行业"
    return ExportFieldContract(
        version=EXPORT_FIELD_CONTRACT_VERSION,
        required_field_names=(
            "platform",
            "nickname",
            "profile_url",
            "followers",
            "city",
            f"{industry_label}兴趣占比",
            f"{location_label}粉丝占比",
            f"{age_label}占比",
            "engagement_rate",
            "active_follower_rate",
            "content_tags",
        ),
        labels={
            "platform": "平台",
            "nickname": "昵称",
            "profile_url": "主页链接",
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


__all__ = [
    "EXPORT_FIELD_CONTRACT_VERSION",
    "ExportFieldContract",
    "build_export_field_contract",
]
