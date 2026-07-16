from types import SimpleNamespace

from app.mcp_gateway.contracts import DataTapService
from app.orchestration.context import ContextBuilder
from app.orchestration.export_contract import (
    EXPORT_FIELD_CONTRACT_VERSION,
    build_export_field_contract,
)
from app.orchestration.schemas import SessionBrief


def make_brief() -> SessionBrief:
    return SessionBrief(
        session_id="session-1",
        brand="测试品牌",
        campaign_name=None,
        platforms=("xiaohongshu", "douyin"),
        category="美妆",
        target_audience="20-30女性",
        budget_min=None,
        budget_max=None,
        filters={"target_fan_locations": ["浙江", "湖州"]},
    )


def test_contract_labels_industry_and_platform_fields() -> None:
    contract = build_export_field_contract(make_brief())

    assert contract.version == EXPORT_FIELD_CONTRACT_VERSION
    assert contract.required_field_names[0] == "platform"
    assert "美妆兴趣占比" in contract.required_field_names
    assert any("抖音平台口径" in note for note in contract.notes)


class FakeWorkspace:
    async def get_owned_session(self, user_id: str, session_id: str):
        return SimpleNamespace(
            id=session_id,
            brand="测试品牌",
            campaign_name=None,
            platforms=["xiaohongshu", "douyin"],
            category="美妆",
            target_audience="20-30女性",
            budget_min=None,
            budget_max=None,
            filters_snapshot={"target_fan_locations": ["浙江", "湖州"]},
        )

    async def list_messages(self, user_id: str, session_id: str):
        return [SimpleNamespace(role="user", content="找达人", sequence=1)]


class FakeRegistry:
    async def list_enabled(self):
        return (
            SimpleNamespace(
                catalog_id="catalog-xhs",
                internal_name="datatap.xiaohongshu.kol.search.v1",
                service=DataTapService.INSIGHT_CUBE,
                input_schema={"type": "object"},
            ),
            SimpleNamespace(
                catalog_id="catalog-douyin",
                internal_name="datatap.douyin.kol.search.v1",
                service=DataTapService.INSIGHT_CUBE,
                input_schema={"type": "object"},
            ),
        )


class FakePermissions:
    async def list_enabled_channels(self, user_id: str):
        return ("xiaohongshu", "douyin")


class FakeReporting:
    async def context_summary(self, session_id: str):
        return {}


async def test_planner_context_serializes_contract_with_all_tools() -> None:
    context = await ContextBuilder(
        workspace=FakeWorkspace(),
        registry=FakeRegistry(),
        permissions=FakePermissions(),
        reporting=FakeReporting(),
    ).build("user-1", "session-1")

    payload = context.model_dump(mode="json")

    assert payload["export_contract"]["version"] == EXPORT_FIELD_CONTRACT_VERSION
    assert {item["internal_name"] for item in payload["tools"]} == {
        "datatap.xiaohongshu.kol.search.v1",
        "datatap.douyin.kol.search.v1",
    }
