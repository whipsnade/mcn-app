from datetime import UTC, datetime
from typing import Any

from app.selection.contract import (
    EXPORT_FIELD_CONTRACT_VERSION,
    build_export_field_contract,
)
from app.workspace.models import WorkspaceSession


def _workspace(**overrides: Any) -> WorkspaceSession:
    now = datetime.now(UTC).replace(tzinfo=None)
    fields: dict[str, Any] = {
        "id": "session-1",
        "user_id": "user-1",
        "title": "圈选会话",
        "brand": "测试品牌",
        "campaign_name": None,
        "status": "active",
        "platforms": ["xiaohongshu", "douyin"],
        "category": "餐饮",
        "target_audience": "25-34岁",
        "budget_min": None,
        "budget_max": None,
        "filters_snapshot": {"target_fan_locations": ["杭州"]},
        "is_starred": False,
        "last_accessed_at": now,
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return WorkspaceSession(**fields)


def test_version_is_kol_excel_v2() -> None:
    assert EXPORT_FIELD_CONTRACT_VERSION == "kol_excel_v2"
    contract = build_export_field_contract(_workspace())
    assert contract.version == "kol_excel_v2"


def test_labels_follow_session_profile() -> None:
    contract = build_export_field_contract(_workspace())

    assert contract.labels["industry_interest"] == "餐饮兴趣"
    assert contract.labels["target_region"] == "杭州粉丝"
    assert contract.labels["target_age"] == "25-34岁"
    assert "餐饮兴趣占比" in contract.required_field_names
    assert "杭州粉丝占比" in contract.required_field_names
    assert "25-34岁占比" in contract.required_field_names


def test_missing_profile_falls_back_to_generic_labels() -> None:
    contract = build_export_field_contract(
        _workspace(category=None, target_audience="", filters_snapshot={})
    )

    assert contract.labels["industry_interest"] == "行业兴趣"
    assert contract.labels["target_region"] == "目标地区粉丝"
    assert contract.labels["target_age"] == "目标年龄段"


def test_required_fields_use_profile_url_not_score() -> None:
    contract = build_export_field_contract(_workspace())

    assert "profile_url" in contract.required_field_names
    assert "score" not in contract.required_field_names


def test_notes_keep_no_fabrication_rules() -> None:
    contract = build_export_field_contract(_workspace())

    assert "不得编造缺失数据" in contract.notes
    assert "缺失字段显示数据缺失" in contract.notes
    assert "每个选中平台必须执行检索" in contract.notes
    assert "抖音平台口径按可获得的获赞、评论、收藏等指标计算" in contract.notes


def test_brainstorm_profile_region_wins_over_legacy_locations() -> None:
    contract = build_export_field_contract(
        _workspace(
            filters_snapshot={
                "brainstorm_profile": {"region": "上海"},
                "target_fan_locations": ["杭州"],
            }
        )
    )

    assert contract.labels["target_region"] == "上海粉丝"
    assert "上海粉丝占比" in contract.required_field_names


def test_blank_region_falls_back_to_legacy_locations_list() -> None:
    contract = build_export_field_contract(
        _workspace(
            filters_snapshot={
                "brainstorm_profile": {"region": "  "},
                "target_fan_locations": ["杭州", "上海"],
            }
        )
    )

    assert contract.labels["target_region"] == "杭州,上海粉丝"


def test_none_filters_snapshot_uses_generic_fallbacks() -> None:
    contract = build_export_field_contract(
        _workspace(filters_snapshot=None, category=None, target_audience="")
    )

    assert contract.labels["target_region"] == "目标地区粉丝"
    assert contract.labels["industry_interest"] == "行业兴趣"
    assert contract.labels["target_age"] == "目标年龄段"


def test_bare_string_locations_is_not_a_list_and_falls_back() -> None:
    contract = build_export_field_contract(
        _workspace(filters_snapshot={"target_fan_locations": "杭州"})
    )

    assert contract.labels["target_region"] == "目标地区粉丝"


def test_blank_audience_and_category_fall_back() -> None:
    contract = build_export_field_contract(_workspace(target_audience="   ", category="  "))

    assert contract.labels["target_age"] == "目标年龄段"
    assert contract.labels["industry_interest"] == "行业兴趣"


def test_long_audience_is_truncated_to_twenty_chars() -> None:
    audience = "25-34岁女性，一二线城市，白领人群，精致妈妈"
    contract = build_export_field_contract(_workspace(target_audience=audience))

    assert contract.labels["target_age"] == audience[:20]
    assert len(contract.labels["target_age"]) == 20
