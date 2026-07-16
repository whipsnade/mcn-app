import json
from types import SimpleNamespace

from app.mcp_gateway.contracts import DataTapService
from app.model.contracts import StructuredResult
from app.orchestration.analytics_contract import ANALYTICS_FIELD_CONTRACT_VERSION
from app.orchestration.context import ContextBuilder
from app.orchestration.export_contract import (
    EXPORT_FIELD_CONTRACT_VERSION,
    build_export_field_contract,
)
from app.orchestration.schemas import SessionBrief
from app.orchestration.planner import Planner
from app.orchestration.schemas import ToolPlan


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
                input_schema={
                    "type": "object",
                    "properties": {"request": {}},
                    "additionalProperties": False,
                },
            ),
            SimpleNamespace(
                catalog_id="catalog-douyin",
                internal_name="datatap.douyin.kol.search.v1",
                service=DataTapService.INSIGHT_CUBE,
                input_schema={
                    "type": "object",
                    "properties": {"request": {}},
                    "additionalProperties": False,
                },
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
    assert payload["analytics_contract"]["version"] == ANALYTICS_FIELD_CONTRACT_VERSION
    assert set(payload["analytics_contract"]["field_names"]) >= {
        "brand_mentions",
        "exposure",
        "interactions",
        "published_at",
        "sentiment_counts",
        "hot_words",
        "audience_age",
        "audience_gender",
        "audience_regions",
    }
    assert any("每个平台" in note for note in payload["analytics_contract"]["notes"])
    assert any("缺失" in note and "猜测" in note for note in payload["analytics_contract"]["notes"])
    assert {item["internal_name"] for item in payload["tools"]} == {
        "datatap.xiaohongshu.kol.search.v1",
        "datatap.douyin.kol.search.v1",
    }


class CapturingModel:
    def __init__(self) -> None:
        self.request = None

    async def complete_json(self, request):
        self.request = request
        return StructuredResult(
            value=ToolPlan.model_validate(
                {
                    "objective": "覆盖所有选中平台并收集 Excel 与 BI 字段",
                    "steps": [
                        {
                            "id": "step_1",
                            "internal_tool_name": "datatap.xiaohongshu.kol.search.v1",
                            "arguments": {},
                            "evidence_goal": "小红书候选字段",
                        },
                        {
                            "id": "step_2",
                            "internal_tool_name": "datatap.douyin.kol.search.v1",
                            "arguments": {},
                            "evidence_goal": "抖音候选字段",
                        },
                    ],
                }
            ),
            usage=None,
            request_id="request-1",
            regeneration_count=0,
        )


async def test_planner_model_request_contains_both_field_contracts() -> None:
    context = await ContextBuilder(
        workspace=FakeWorkspace(),
        registry=FakeRegistry(),
        permissions=FakePermissions(),
        reporting=FakeReporting(),
    ).build("user-1", "session-1")
    model = CapturingModel()

    await Planner(model=model).plan(context)  # type: ignore[arg-type]

    assert model.request is not None
    payload = json.loads(model.request.messages[1].content)
    assert payload["export_contract"]["version"] == EXPORT_FIELD_CONTRACT_VERSION
    assert payload["analytics_contract"]["version"] == ANALYTICS_FIELD_CONTRACT_VERSION
