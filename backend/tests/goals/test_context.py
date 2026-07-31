from contextlib import asynccontextmanager

import pytest

from app.artifacts.service import ArtifactService
from app.goals.context import GoalPlannerContextBuilder
from app.identity.brand_profiles import BrandProfileService
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import MetricGridBlock, MetricItem, ReportDocument
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.workspace.schemas import MessageCreate, SessionCreate
from app.workspace.service import WorkspaceService


def _document(title: str) -> ReportDocument:
    return ReportDocument(
        title=title,
        conclusion="结论。",
        blocks=[MetricGridBlock(items=[MetricItem(label="总声量", value=1200)])],
    )


@pytest.mark.asyncio
async def test_context_uses_trigger_message_session_brand_and_user_scoped_exemplars(
    db_session,
    user_factory,
    monkeypatch,
) -> None:
    user = await user_factory()
    exemplar_query: dict[str, object] = {}

    async def scoped_exemplars(db, *, purpose, tags, user_id, limit=2):
        exemplar_query.update(
            {
                "db": db,
                "purpose": purpose,
                "tags": tags,
                "user_id": user_id,
                "limit": limit,
            }
        )
        return [{"excerpt": "anonymous"}]

    monkeypatch.setattr(
        "app.goals.context.find_success_exemplars",
        scoped_exemplars,
    )
    workspace = await WorkspaceService(db_session).create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )
    task = await TaskService(db_session).create(
        user.id,
        workspace.id,
        TaskCreate(content="分析 618 活动表现"),
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    context = await GoalPlannerContextBuilder(borrowed_session).build(task.id)

    assert context.task_id == task.id
    assert context.current_message == "分析 618 活动表现"
    assert context.session_context["active_brand"] == "喜茶"
    assert context.session_context["category"] == "茶饮"
    assert context.account_default_brand is None
    assert context.allowed_goal_types == (
        "brand_analysis",
        "campaign_analysis",
        "kol_selection",
    )
    assert context.recent_messages[-1].content == "分析 618 活动表现"
    assert context.exemplars == ({"excerpt": "anonymous"},)
    assert exemplar_query["purpose"] == "goal_planner"
    assert exemplar_query["tags"] == ["goal_planner:shadow"]
    assert exemplar_query["user_id"] == user.id


@pytest.mark.asyncio
async def test_context_excludes_messages_after_trigger(db_session, user_factory) -> None:
    user = await user_factory()
    workspace_service = WorkspaceService(db_session)
    workspace = await workspace_service.create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )
    task = await TaskService(db_session).create(
        user.id,
        workspace.id,
        TaskCreate(content="分析 618 活动表现"),
    )
    await workspace_service.append_message(
        user.id,
        workspace.id,
        MessageCreate(content="这条消息发生在任务触发之后"),
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    context = await GoalPlannerContextBuilder(borrowed_session).build(task.id)

    assert [message.content for message in context.recent_messages] == ["分析 618 活动表现"]


@pytest.mark.asyncio
async def test_context_carries_account_default_brand_from_profiles(
    db_session, user_factory
) -> None:
    """account_default_brand 从 user_brand_profiles 读；未设置时为 None。"""
    user = await user_factory()
    await BrandProfileService(db_session).set_default_brand(user.id, "海底捞")
    workspace = await WorkspaceService(db_session).create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )
    task = await TaskService(db_session).create(
        user.id,
        workspace.id,
        TaskCreate(content="分析 618 活动表现"),
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    context = await GoalPlannerContextBuilder(borrowed_session).build(task.id)

    assert context.account_default_brand == "海底捞"
    # 会话 active_brand 优先级不受影响，仍取会话品牌。
    assert context.session_context["active_brand"] == "喜茶"


@pytest.mark.asyncio
async def test_build_for_message_assembles_context_without_task(
    db_session, user_factory
) -> None:
    """build_for_message 不依赖 task：当前消息用入参（可能尚未落库），尾部补进 recent。"""
    user = await user_factory()
    await BrandProfileService(db_session).set_default_brand(user.id, "海底捞")
    workspace_service = WorkspaceService(db_session)
    workspace = await workspace_service.create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )
    await workspace_service.append_message(
        user.id, workspace.id, MessageCreate(content="先看看历史消息")
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    context = await GoalPlannerContextBuilder(borrowed_session).build_for_message(
        user.id, workspace.id, "分析一下 618 活动"
    )

    assert context.user_id == user.id
    assert context.session_id == workspace.id
    assert context.task_id == ""
    assert context.current_message == "分析一下 618 活动"
    # 尾部是未落库的当前消息（role=user），历史消息在前。
    assert context.recent_messages[-1].content == "分析一下 618 活动"
    assert context.recent_messages[-1].role == "user"
    assert context.recent_messages[-2].content == "先看看历史消息"
    assert context.session_context["active_brand"] == "喜茶"
    assert context.session_context["category"] == "茶饮"
    assert context.account_default_brand == "海底捞"
    assert context.artifact_summaries == ()


@pytest.mark.asyncio
async def test_build_for_message_passthrough_available_tools(db_session, user_factory) -> None:
    """available_tools 由调用方（enforce 入口）注入并原样透传；缺省为空 tuple。"""
    user = await user_factory()
    workspace = await WorkspaceService(db_session).create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    tools = (
        {
            "internal_name": "social_statistic_trend",
            "description": "社媒趋势统计",
            "required_params": ["datasource", "name"],
        },
    )
    context = await GoalPlannerContextBuilder(borrowed_session).build_for_message(
        user.id, workspace.id, "分析喜茶声量", available_tools=tools
    )
    assert context.available_tools == tools

    default_context = await GoalPlannerContextBuilder(borrowed_session).build_for_message(
        user.id, workspace.id, "分析喜茶声量"
    )
    assert default_context.available_tools == ()


@pytest.mark.asyncio
async def test_build_for_message_rejects_foreign_session(db_session, user_factory) -> None:
    user = await user_factory()
    other = await user_factory()
    workspace = await WorkspaceService(db_session).create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    with pytest.raises(LookupError, match="session_not_found"):
        await GoalPlannerContextBuilder(borrowed_session).build_for_message(
            other.id, workspace.id, "越权消息"
        )


@pytest.mark.asyncio
async def test_context_injects_artifact_summaries(db_session, user_factory) -> None:
    """artifact_summaries：每 module 最新 completed artifact 的紧凑投影；无产物为 ()。"""
    user = await user_factory()
    workspace = await WorkspaceService(db_session).create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )
    artifacts = ArtifactService(db_session)
    report = await AnalysisReportService(db_session).build_session_report(
        user_id=user.id,
        session_id=workspace.id,
        document=_document("品牌分析v1"),
        report_type="brand_analysis",
        scope={"brand": "喜茶"},
    )
    await artifacts.register_artifact(
        user_id=user.id,
        session_id=workspace.id,
        artifact_key="goal:g1:brand_report",
        artifact_type="brand_report",
        title="喜茶品牌声量分析",
        version=1,
        status="completed",
        report_id=report.id,
        scope={"brand": "喜茶"},
    )
    # failed artifact 不进入摘要。
    await artifacts.register_artifact(
        user_id=user.id,
        session_id=workspace.id,
        artifact_key="goal:g2:campaign_report",
        artifact_type="campaign_report",
        title="活动复盘报告",
        version=1,
        status="failed",
        error_code="no_evidence_collected",
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    context = await GoalPlannerContextBuilder(borrowed_session).build_for_message(
        user.id, workspace.id, "继续分析"
    )

    assert len(context.artifact_summaries) == 1
    summary = context.artifact_summaries[0]
    assert summary["module_key"] == "brand"
    assert summary["artifact_type"] == "brand_report"
    assert summary["title"] == "喜茶品牌声量分析"
    assert summary["version"] == 1
    assert summary["scope"] == {"brand": "喜茶"}
    assert summary["created_at"]


@pytest.mark.asyncio
async def test_context_artifact_summaries_empty_without_artifacts(
    db_session, user_factory
) -> None:
    user = await user_factory()
    workspace = await WorkspaceService(db_session).create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    context = await GoalPlannerContextBuilder(borrowed_session).build_for_message(
        user.id, workspace.id, "继续分析"
    )

    assert context.artifact_summaries == ()
