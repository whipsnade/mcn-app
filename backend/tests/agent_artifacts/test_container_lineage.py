"""容器嵌套 Evidence 的 lineage source_path 回归（H1，第四轮 UAT 根因）。

真实 DataTap Evidence 常把行列表嵌在容器键下：``kol_xiaohongshu_search``
的「KOL 列表」、``{"result": "<json 字符串>"}`` 包装、rows/list 容器等。
行提取（``extract_rows``）已识别这些容器，但 builder 生成 lineage
``source_path`` 时必须携带完整基准路径（容器键 + 行下标 + 字段名），否则
``submit_review`` 的 lineage 校验报 ``evidence_source_path_not_found``
（如 ``/52/粉丝数`` 缺少 ``/KOL 列表`` 前缀）。

本文件断言：对 ``extract_rows`` 支持的所有容器形态，五个 builder 产出的
每个 Evidence ``source_path`` 都能在对应 Evidence raw payload 中解析，
且容器嵌套 Evidence 能端到端走完 build → ``validate_and_freeze_lineage``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_artifacts.builders.brand import build_brand_report_draft
from app.agent_artifacts.builders.campaign import build_campaign_report_draft
from app.agent_artifacts.builders.common import DraftBuildError
from app.agent_artifacts.builders.kol_detail import build_kol_detail_draft
from app.agent_artifacts.builders.kol_selection import build_kol_selection_draft
from app.agent_artifacts.builders.raw_rows import extract_rows
from app.agent_artifacts.lineage import (
    DbLineageLoader,
    LineageOwner,
    resolve_pointer,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.models import ArtifactDraftRevision
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentRunAttempt,
    AgentStep,
    AgentToolCall,
)
from app.agent_runtime.tools.builders import (
    BuildKolDetailDraftTool,
    BuildKolSelectionDraftTool,
)
from app.agent_runtime.tools.contracts import ToolContext

LIGHT_CTX = ToolContext(
    user_id="u-1",
    session_id="s-1",
    run_id="r-1",
    profile_name="session_analyst_v1",
)

KOL_SCOPE = {
    "category": "美食",
    "platforms": ["小红书"],
    "audience": {"regions": ["上海"], "age_ranges": ["18-24"], "interests": ["美食"]},
    "filters": {},
}

BRAND_SCOPE = {
    "brand": "瑞幸咖啡",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu"],
    "keywords": ["瑞幸"],
    "comparison_mode": "none",
}

CAMPAIGN_SCOPE = {
    "brand": "瑞幸咖啡",
    "campaign": "生椰拿铁上新",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu"],
    "keywords": ["生椰拿铁"],
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _xhs_search_payload() -> dict[str, Any]:
    """真实 kol_xiaohongshu_search 形态：中文容器键「KOL 列表」+ 分页信息。"""
    return {
        "KOL 列表": [
            {
                "账号ID (kwUid)": "5ff26b060000000001003a8d",
                "平台": "xiaohongshu",
                "昵称": "哈尔滨日报冰城+",
                "头像": "https://example.com/avatar.webp",
                "主页": "https://www.xiaohongshu.com/user/profile/5ff26b060000000001003a8d",
                "粉丝数": 773129,
                "有效粉丝数": 617697,
                "有效粉丝率": 0.799,
                "平均互动": 2503.76,
                "平均点赞": 2046.54,
                "平均评论": 112.15,
                "平均转发": 228.15,
                "周粉丝增长率": 0.0156,
                "预估报价-图文": 105979.0,
            },
            {
                "账号ID (kwUid)": "63365127000000001901cbe3",
                "平台": "xiaohongshu",
                "昵称": "封面新闻",
                "粉丝数": 1323695,
                "平均互动": 2271.82,
            },
        ],
        "分页信息": {"页码": 1, "总数": 100},
    }


def _kol_detail_dict() -> dict[str, Any]:
    return {
        "identity": {
            "nickname": "达人1",
            "avatar_url": "https://example.com/a.png",
            "homepage_url": "https://example.com/h",
            "bio": "美食博主",
            "verification": True,
            "region": "上海",
        },
        "metrics": {
            "followers": 500_000,
            "following": 100,
            "posts": 200,
            "likes": 50_000,
            "active_followers": 300_000,
            "active_follower_rate": 0.6,
            "growth_rate": 0.3,
            "engagement_total": 100,
            "avg_engagement": 1.0,
        },
        "audience": {
            "gender_distribution": [{"key": "女", "label": "女", "value": 60, "share": 0.6}],
            "age_distribution": [{"key": "18-24", "label": "18-24", "value": 40, "share": 0.4}],
            "region_distribution": [{"key": "上海", "label": "上海", "value": 50, "share": 0.5}],
            "interest_distribution": [{"key": "美食", "label": "美食", "value": 80, "share": 0.8}],
        },
        "trend": [{"date": "2026-07-01", "followers": 500_000, "engagement": 100, "posts": 2}],
        "latest_posts": [
            {
                "post_id": "p1",
                "title": "测评",
                "url": "https://example.com/p1",
                "published_at": "2026-07-02T10:00:00",
                "likes": 10,
                "comments": 2,
                "shares": 1,
                "engagement": 13,
            }
        ],
    }


def _assert_refs_resolve(refs: list[dict[str, Any]], payloads: dict[str, Any]) -> None:
    """refs 中每个 Evidence source_path 必须在对应 raw payload 中可解析。"""
    assert refs
    for ref in refs:
        for source in ref["sources"]:
            if source.get("source_type") != "evidence":
                continue
            payload = payloads[source["evidence_id"]]
            resolve_pointer(payload, source["source_path"])  # 失败即 PointerError


# ---------------------------------------------------------------------------
# 1. extract_rows：所有容器形态的 source_path 都必须可解析（横向审计基座）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_first_path"),
    [
        # 顶层数组。
        ([{"粉丝数": 100}, {"粉丝数": 200}], "/0"),
        # rows/list 等已知容器键。
        ({"rows": [{"粉丝数": 100}]}, "/rows/0"),
        ({"list": [{"粉丝数": 100}]}, "/list/0"),
        # 中文容器键（kol_xiaohongshu_search 真实形态）。
        ({"KOL 列表": [{"粉丝数": 100}], "分页信息": {"页码": 1}}, "/KOL 列表/0"),
        # RFC 6901 转义：容器键含 '/' 与 '~'。
        ({"列表/明细~v1": [{"粉丝数": 100}]}, "/列表~1明细~0v1/0"),
        # {"result": "<json 字符串>"} 包装：指针无法下钻字符串，粗粒度指向整个串。
        ({"result": json.dumps([{"粉丝数": 100}])}, "/result"),
        ({"result": json.dumps({"rows": [{"粉丝数": 100}]})}, "/result"),
        # 单行 dict：source_path 指向首个键（min_length=1 约束），字段基准为根。
        ({"声量": 100, "互动数": 50}, "/声量"),
    ],
)
def test_extract_rows_source_paths_resolve(payload: Any, expected_first_path: str) -> None:
    refs = extract_rows("ev-1", payload)
    assert refs
    assert refs[0].source_path == expected_first_path
    for ref in refs:
        resolve_pointer(payload, ref.source_path)
        if ref.field_base:
            resolve_pointer(payload, ref.field_base)


def test_extract_rows_single_dict_field_base_is_root() -> None:
    """单行 dict 兜底：字段路径基准必须是根（''），否则 base+字段 不可解析。"""
    refs = extract_rows("ev-1", {"声量": 100, "互动数": 50})
    assert len(refs) == 1
    assert refs[0].field_base == ""


# ---------------------------------------------------------------------------
# 2. kol_selection builder：lineage 携带容器基准路径
# ---------------------------------------------------------------------------


async def test_kol_selection_lineage_paths_carry_container_prefix() -> None:
    """容器嵌套 Evidence：builder 的每个 source_path 都必须可解析。

    修复前 builder 按顶层数组发射 ``/{index}/{字段}``（缺容器前缀），
    在 ``{"KOL 列表": [...]}`` payload 上不可解析（UAT 报错根因）。
    """
    payload = _xhs_search_payload()
    refs = extract_rows("ev-xhs", payload)
    build = await build_kol_selection_draft(
        scope=KOL_SCOPE,
        evidence_id="ev-xhs",
        items=[ref.row for ref in refs],
        row_source_paths=[ref.field_base for ref in refs],
        context=LIGHT_CTX,
    )
    _assert_refs_resolve(build.evidence_refs, {"ev-xhs": payload})
    paths = [
        source["source_path"]
        for ref in build.evidence_refs
        for source in ref["sources"]
        if source.get("source_type") == "evidence"
    ]
    assert any(path.startswith("/KOL 列表/") for path in paths)


async def test_kol_selection_lineage_result_wrapper_coarse_path() -> None:
    """``{"result": "<json>"}`` 包装：所有 source_path 粗粒度指向 /result（可解析）。"""
    inner = {
        "rows": [
            {
                "platform": "小红书",
                "kol_uid": "1",
                "nickname": "达人1",
                "followers": 500_000,
                "engagement_total": 100,
                "score_inputs": {"content_score": 90},
            }
        ]
    }
    payload = {"result": json.dumps(inner, ensure_ascii=False)}
    refs = extract_rows("ev-wrap", payload)
    build = await build_kol_selection_draft(
        scope=KOL_SCOPE,
        evidence_id="ev-wrap",
        items=[ref.row for ref in refs],
        row_source_paths=[ref.field_base for ref in refs],
        context=LIGHT_CTX,
    )
    _assert_refs_resolve(build.evidence_refs, {"ev-wrap": payload})


async def test_kol_selection_row_source_paths_length_mismatch_rejected() -> None:
    """row_source_paths 与 items 长度不一致：fail-fast，不静默错位。"""
    with pytest.raises(DraftBuildError):
        await build_kol_selection_draft(
            scope=KOL_SCOPE,
            evidence_id="ev-1",
            items=[{"platform": "小红书", "kol_uid": "1", "nickname": "a"}],
            row_source_paths=["/0", "/1"],
            context=LIGHT_CTX,
        )


# ---------------------------------------------------------------------------
# 3. kol_detail builder：{"result": "<json>"} 包装的 source_path 可解析
# ---------------------------------------------------------------------------


async def test_kol_detail_result_wrapper_lineage_resolves() -> None:
    """详情 Evidence 为 result 字符串包装时，lineage 粗粒度指向 /result。

    修复前 builder 按顶层对象发射 ``/metrics/followers`` 等路径，在
    ``{"result": "<json>"}`` payload 上不可解析。
    """
    payload = {"result": json.dumps(_kol_detail_dict(), ensure_ascii=False)}
    build = build_kol_detail_draft(
        platform="xiaohongshu",
        kol_uid="1",
        detail=_kol_detail_dict(),
        evidence_id="ev-detail",
        cache_state={
            "hit": False,
            "fetched_at": "2026-08-01T00:00:00",
            "expires_at": "2026-08-02T00:00:00",
        },
        source_base="/result",
    )
    _assert_refs_resolve(build.evidence_refs, {"ev-detail": payload})


def test_kol_detail_top_level_paths_unchanged() -> None:
    """顶层对象详情 Evidence：字段级路径保持 ``/metrics/followers`` 形态。"""
    detail = _kol_detail_dict()
    build = build_kol_detail_draft(
        platform="xiaohongshu",
        kol_uid="1",
        detail=detail,
        evidence_id="ev-detail",
        cache_state={
            "hit": False,
            "fetched_at": "2026-08-01T00:00:00",
            "expires_at": "2026-08-02T00:00:00",
        },
    )
    _assert_refs_resolve(build.evidence_refs, {"ev-detail": detail})
    assert any(
        source["source_path"] == "/metrics/followers"
        for ref in build.evidence_refs
        for source in ref["sources"]
    )


# ---------------------------------------------------------------------------
# 4. brand / campaign builder：容器嵌套 Evidence 的 lineage 可解析（横向审计）
# ---------------------------------------------------------------------------


def _brand_container_evidence() -> dict[str, list[tuple[str, Any]]]:
    """brand 各章节 Evidence 换成真实容器嵌套形态。"""
    return {
        "overview_current": [
            ("ev-overview", {"rows": [{"平台": "小红书", "声量": 100, "互动数": 1000, "发帖数": 80}]})
        ],
        "sentiment": [
            (
                "ev-sentiment",
                {
                    "情感列表": [
                        {"平台": "小红书", "情感": "正面", "声量": 60},
                        {"平台": "小红书", "情感": "负面", "声量": 10},
                    ]
                },
            )
        ],
        "daily_trend": [
            (
                "ev-trend",
                {
                    "result": json.dumps(
                        [{"日期": "2026-07-01", "平台": "小红书", "声量": 10, "互动数": 100}],
                        ensure_ascii=False,
                    )
                },
            )
        ],
        "top_posts": [
            (
                "ev-posts",
                {
                    "笔记列表": [
                        {
                            "平台": "小红书",
                            "帖子ID": "p1",
                            "标题": "测评",
                            "发布时间": "2026-07-05 10:00:00",
                            "点赞数": 100,
                            "互动数": 125,
                            "帖子链接": "https://example.com/p1",
                        }
                    ]
                },
            )
        ],
    }


def test_brand_builder_container_evidence_lineage_resolves() -> None:
    evidence = _brand_container_evidence()
    build = build_brand_report_draft(scope=BRAND_SCOPE, evidence=evidence)
    payloads = {
        evidence_id: payload for pairs in evidence.values() for evidence_id, payload in pairs
    }
    _assert_refs_resolve(build.evidence_refs, payloads)


def test_campaign_builder_container_evidence_lineage_resolves() -> None:
    evidence = {
        "posts": [
            (
                "ev-posts",
                {
                    "帖子列表": [
                        {
                            "平台": "小红书",
                            "帖子ID": "p1",
                            "标题": "测评",
                            "用户ID": "u1",
                            "发布时间": "2026-07-03 09:00:00",
                            "点赞数": 100,
                            "评论数": 10,
                            "互动数": 115,
                            "情感": "正面",
                            "帖子链接": "https://example.com/p1",
                        }
                    ]
                },
            )
        ],
        "sentiment": [
            (
                "ev-sentiment",
                {
                    "result": json.dumps(
                        [{"平台": "小红书", "情感": "正面", "声量": 60}], ensure_ascii=False
                    )
                },
            )
        ],
    }
    build = build_campaign_report_draft(scope=CAMPAIGN_SCOPE, evidence=evidence)
    payloads = {
        evidence_id: payload for pairs in evidence.values() for evidence_id, payload in pairs
    }
    _assert_refs_resolve(build.evidence_refs, payloads)


# ---------------------------------------------------------------------------
# 5. 端到端回归：容器嵌套 Evidence 走完 build → validate_and_freeze_lineage
# ---------------------------------------------------------------------------


async def _make_step(db_session, run_id: str) -> AgentStep:
    now = _now()
    attempt = AgentRunAttempt(id=str(uuid4()), run_id=run_id, attempt=1, started_at=now)
    db_session.add(attempt)
    await db_session.flush()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run_id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="running",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    return step


async def _write_evidence(db_session, *, session_id: str, run_id: str, step_id: str, payload: Any) -> str:
    now = _now()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run_id,
        step_id=step_id,
        logical_call_id=f"call-{uuid4()}",
        service="mcp",
        internal_tool_name="kol_xiaohongshu_search",
        arguments_json={},
        arguments_hash="h",
        status="settled",
        points_reserved=10,
        points_settled=10,
        started_at=now,
        completed_at=now,
    )
    db_session.add(call)
    await db_session.flush()
    item = await EvidenceWriter(db_session).write(
        session_id=session_id,
        run_id=run_id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="kol 搜索",
        scope_json=None,
        period_json=None,
        raw_payload=payload,
    )
    return item.id


async def _latest_revision(db_session, revision_id: str) -> ArtifactDraftRevision:
    revision = await db_session.scalar(
        select(ArtifactDraftRevision).where(ArtifactDraftRevision.id == revision_id)
    )
    assert revision is not None
    return revision


async def test_kol_selection_container_evidence_end_to_end_lineage_freezes(
    db_session, user_factory, session_factory, run_factory
) -> None:
    """中文容器键 Evidence 走完 Builder 工具 + lineage 冻结校验（submit_review 同款）。

    修复前在 ``validate_and_freeze_lineage`` 抛
    ``evidence_source_path_not_found: missing key '52'``。
    """
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    step = await _make_step(db_session, run.id)
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_xhs_search_payload(),
    )
    ctx = ToolContext(
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile_name="session_analyst_v1",
        step_id=step.id,
    )

    result = await BuildKolSelectionDraftTool(db_session).execute(
        ctx, {"scope": KOL_SCOPE, "evidence_id": evidence_id}
    )
    assert result.status == "success", result.safe_summary
    revision = await _latest_revision(db_session, json.loads(result.safe_summary)["revision_id"])

    frozen = await validate_and_freeze_lineage(
        payload=revision.payload_json,
        refs=revision.evidence_refs_json,
        owner=LineageOwner(user_id=user.id, session_id=session.id),
        loader=DbLineageLoader(db_session),
    )
    assert frozen.refs


async def test_kol_detail_result_wrapper_end_to_end_lineage_freezes(
    db_session, user_factory, session_factory, run_factory
) -> None:
    """``{"result": "<json>"}`` 包装的详情 Evidence 走完 Builder 工具 + lineage 冻结。"""
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    step = await _make_step(db_session, run.id)
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload={"result": json.dumps(_kol_detail_dict(), ensure_ascii=False)},
    )
    ctx = ToolContext(
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile_name="session_analyst_v1",
        step_id=step.id,
    )

    result = await BuildKolDetailDraftTool(db_session).execute(
        ctx,
        {
            "platform": "xiaohongshu",
            "kol_uid": "1",
            "evidence_id": evidence_id,
            "cache_state": {
                "hit": False,
                "fetched_at": "2026-08-01T00:00:00",
                "expires_at": "2026-08-02T00:00:00",
            },
        },
    )
    assert result.status == "success", result.safe_summary
    revision = await _latest_revision(db_session, json.loads(result.safe_summary)["revision_id"])

    frozen = await validate_and_freeze_lineage(
        payload=revision.payload_json,
        refs=revision.evidence_refs_json,
        owner=LineageOwner(user_id=user.id, session_id=session.id),
        loader=DbLineageLoader(db_session),
    )
    assert frozen.refs
