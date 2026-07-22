from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import reduce
import hashlib
import json
import re
from typing import Any
import unicodedata

from app.selection.schemas import (
    EvidencePriority,
    NormalizedBrandEvidence,
    NormalizedKolEvidence,
    ToolEvidence,
)


class UnknownEvidenceToolError(ValueError):
    pass


Adapter = Callable[[ToolEvidence], tuple[NormalizedKolEvidence, ...]]
_DROP = object()
_SENSITIVE_STORAGE_KEYS = {
    "authorization",
    "token",
    "api_key",
    "credential",
    "secret",
    "url",
    "endpoint",
    "host",
}
_SENSITIVE_STORAGE_KEY_PARTS = set(_SENSITIVE_STORAGE_KEYS)
_FORBIDDEN_VALUE_TERMS = (
    "datatap.deepminer.com.cn",
    "api.lkeap.cloud.tencent.com",
    "google-trends-mcp",
    "zhihu-mcp",
    "toutiao-mcp",
    "baidu-index-mcp",
)
_TEXT_SECRET_PATTERN = re.compile(
    r"(?<![a-z0-9_])(?:authorization|bearer|api[ _-]?key|token|credentials?|secret)"
    r"(?![a-z0-9_])",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE_PATTERN = re.compile(
    r"^(?:sk-[a-z0-9_-]{16,}|[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+)$",
    re.IGNORECASE,
)
_INTERNAL_PATH_PATTERN = re.compile(
    r"(?:^|[/\\])(?:api|internal)(?:[/\\]|$)",
    re.IGNORECASE,
)
_URI_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"^(?:/|\\\\|[a-z]:[/\\])",
    re.IGNORECASE,
)
_MAX_NUMERIC_TEXT_LENGTH = 64
_MAX_DECIMAL_ADJUSTED_EXPONENT = 18
_MAX_GENERAL_NUMBER = Decimal("1000000000000000")
_MAX_ANALYTICS_NUMBER = Decimal("1000000000000000")


def normalize_tool_evidence(evidence: Iterable[ToolEvidence]) -> tuple[NormalizedKolEvidence, ...]:
    """只接受评审过的内部工具名，按稳定平台身份合并同一达人。"""
    grouped: dict[tuple[str, str], list[NormalizedKolEvidence]] = {}
    for item in evidence:
        adapter = _ADAPTERS.get(item.internal_tool_name)
        if adapter is None:
            if item.internal_tool_name in _NON_KOL_EVIDENCE_TOOLS:
                continue
            raise UnknownEvidenceToolError(f"unknown_evidence_tool:{item.internal_tool_name}")
        for normalized in adapter(item):
            key = (normalized.platform, normalized.platform_account_id)
            grouped.setdefault(key, []).append(normalized)
    return tuple(_merge_many(grouped[key]) for key in sorted(grouped))


_BRAND_EVIDENCE_TOOLS = {
    "datatap.insight.query.analysis.v1",
    "datatap.insight.social.statistic.trend.v1",
    "datatap.insight.social.statistic.user.profile.v1",
    "datatap.insight.social.statistic.hot.user.v1",
    "datatap.insight.social.statistic.overview.v1",
    "datatap.insight.social.statistic.hot.topic.v1",
    "datatap.insight.social.statistic.category.rank.v1",
    "datatap.insight.social.statistic.brand.activity.v1",
    "datatap.insight.query.raw.posts.v1",
    "datatap.insight.query.rank.list.v1",
    "datatap.insight.analysis.target.search.v1",
}
# insight 侧按用户维度返回 per-user 行的工具：行即达人证据，注册 KOL 适配器
# （query.analysis / hot.user 的大盘形态行无 用户昵称，适配器返回空）。
_INSIGHT_USER_STATS_TOOLS = {
    "datatap.insight.query.analysis.v1",
    "datatap.insight.social.statistic.hot.user.v1",
}
_NON_KOL_EVIDENCE_TOOLS = (_BRAND_EVIDENCE_TOOLS - _INSIGHT_USER_STATS_TOOLS) | {
    "datatap.insight.match.best.tag.v1",
    "datatap.social.grow.kol.match.mentions.tag.v1",
    "datatap.social.grow.kol.class.tag.dictionary.v1",
    # B站服务工具暂无候选归一化适配器：pipeline 忽略其证据，避免整任务失败。
    "datatap.bilibili.general.search.v1",
    "datatap.bilibili.search.user.v1",
    "datatap.bilibili.video.danmaku.v1",
    "datatap.bilibili.precise.results.v1",
}
_BRAND_PLATFORM_ALIASES = {
    "小红书": "xiaohongshu",
    "xiaohongshu": "xiaohongshu",
    "抖音": "douyin",
    "douyin": "douyin",
    "微博": "weibo",
    "weibo": "weibo",
    "微信": "wechat",
    "wechat": "wechat",
    "b站": "bilibili",
    "哔哩哔哩": "bilibili",
    "bilibili": "bilibili",
}
_BRAND_PLATFORM_KEYS = ("平台", "platform", "media", "媒介", "datasource", "数据源")
_BRAND_PERIOD_KEYS = (
    "日期",
    "时间",
    "月份",
    "月份",
    "月",
    "周期",
    "date",
    "period",
    "published_at",
)
_BRAND_FIELD_ALIASES = {
    "brand_mentions": ("品牌声量", "声量", "品牌提及量", "品牌提及数", "brand_mentions", "volume", "发帖数", "帖子数"),
    "exposure": ("曝光量", "曝光数", "曝光", "exposure", "阅读数", "播放量"),
    "interactions": ("互动数", "互动量", "互动", "interactions", "点赞数", "评论数"),
    "sentiment_index": ("情感指数", "情感得分", "sentiment_index", "sentiment_score"),
    "sentiment_counts": ("情感统计", "情感分布", "舆情分布", "sentiment_counts", "sentiment"),
    "hot_words": ("热词", "热点词", "hot_words", "keywords"),
    "audience_age": ("年龄分布", "受众年龄分布", "audience_age"),
    "audience_gender": ("性别分布", "受众性别分布", "audience_gender"),
    "audience_regions": ("地域分布", "受众地域分布", "audience_regions"),
}


def normalize_brand_evidence(
    evidence: Iterable[ToolEvidence],
) -> tuple[NormalizedBrandEvidence, ...]:
    """将品牌统计工具的 JSON 字符串结果投影为 BI 白名单字段。"""
    result: list[NormalizedBrandEvidence] = []
    for item in evidence:
        if item.internal_tool_name not in _BRAND_EVIDENCE_TOOLS:
            continue
        raw = item.payload.get("result")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        for row in _brand_rows(raw):
            normalized = _normalize_brand_row(item, row)
            if normalized is not None:
                result.append(normalized)
    return tuple(result)


def _brand_rows(value: Any) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if any(alias in node for aliases in _BRAND_FIELD_ALIASES.values() for alias in aliases):
            rows.append(node)
        for key in ("data", "items", "rows", "list", "records", "trend", "result"):
            child = node.get(key)
            if child is not node:
                visit(child)

    visit(value)
    return tuple(rows)


def _brand_first(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in row and _has_value(row[alias]):
            return row[alias]
    return None


def _brand_platform(row: dict[str, Any]) -> str:
    value = _brand_first(row, _BRAND_PLATFORM_KEYS)
    text = str(value or "全平台").strip().casefold()
    for alias, platform in _BRAND_PLATFORM_ALIASES.items():
        if alias in text:
            return platform
    return "all"


def _normalize_brand_row(item: ToolEvidence, row: dict[str, Any]) -> NormalizedBrandEvidence | None:
    fields: dict[str, Any] = {}
    period = _brand_first(row, _BRAND_PERIOD_KEYS)
    if period not in (None, ""):
        fields["published_at"] = str(period)
    for name, aliases in _BRAND_FIELD_ALIASES.items():
        value = _brand_first(row, aliases)
        if value in (None, ""):
            continue
        try:
            if name in {"brand_mentions", "exposure", "interactions"}:
                fields[name] = _non_negative_number(value)
            elif name == "sentiment_index":
                parsed = float(value)
                if -100 <= parsed <= 100:
                    fields[name] = parsed
            elif name == "sentiment_counts":
                fields[name] = _sentiment_counts(value)
            elif name == "hot_words":
                fields[name] = _hot_words(value)
            elif name.startswith("audience_"):
                fields[name] = _distribution(value)
        except (TypeError, ValueError, InvalidOperation):
            continue
    if not fields:
        return None
    return NormalizedBrandEvidence(
        tool_name=item.internal_tool_name,
        platform=_brand_platform(row),
        period=str(period) if period not in (None, "") else None,
        analytics_fields=fields,
        evidence_references=(item.source_call_id,) if item.source_call_id else (),
        collected_at=item.collected_at,
    )


def redact_evidence_for_storage(value: Any) -> Any:
    """递归移除密钥、连接端点和已禁用服务的证据内容。"""
    cleaned = _redact(value)
    return {} if cleaned is _DROP else cleaned


def _normalized_storage_key(key: str) -> str:
    normalized = unicodedata.normalize("NFKC", key)
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    return re.sub(r"[^a-z0-9]+", "_", snake_case.casefold()).strip("_")


def _is_sensitive_storage_key(key: str) -> bool:
    normalized = _normalized_storage_key(key)
    return normalized in _SENSITIVE_STORAGE_KEYS or any(
        part in normalized for part in _SENSITIVE_STORAGE_KEY_PARTS
    )


def _contains_text_secret(value: str) -> bool:
    return _TEXT_SECRET_PATTERN.search(unicodedata.normalize("NFKC", value)) is not None


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _is_sensitive_storage_key(key):
                continue
            cleaned = _redact(item)
            if cleaned is not _DROP:
                result[key] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        return [cleaned for item in value if (cleaned := _redact(item)) is not _DROP]
    if isinstance(value, str):
        lowered = unicodedata.normalize("NFKC", value).casefold()
        if (
            "http://" in lowered
            or "https://" in lowered
            or any(term in lowered for term in _FORBIDDEN_VALUE_TERMS)
            or _contains_text_secret(value)
        ):
            return _DROP
    return value


def _creator_search_adapter(item: ToolEvidence) -> tuple[NormalizedKolEvidence, ...]:
    candidates = item.payload.get("candidates", [item.payload])
    if not isinstance(candidates, list):
        raise ValueError("invalid_creator_search_evidence")
    return tuple(_normalize_candidate(item, candidate) for candidate in candidates)


def _datatap_xiaohongshu_search_adapter(
    item: ToolEvidence,
) -> tuple[NormalizedKolEvidence, ...]:
    """将评审过的小红书 KOL 列表结果映射为内部统一证据。

    DataTap 将列表序列化在 ``result`` 字符串中；只提取用于候选比对的
    公开指标，避免把达人简介、联系方式或头像等原始字段写入候选快照。
    """
    raw_result = item.payload.get("result")
    if not isinstance(raw_result, str):
        raise ValueError("invalid_datatap_xiaohongshu_result")
    try:
        payload = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_datatap_xiaohongshu_result") from exc
    candidates = payload.get("KOL 列表") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("invalid_datatap_xiaohongshu_candidates")
    normalized: list[NormalizedKolEvidence] = []
    for candidate in candidates:
        try:
            normalized.append(_normalize_datatap_xiaohongshu_candidate(item, candidate))
        except (TypeError, ValueError):
            # 单行坏数据（缺身份、评分越界等）不应拖垮整页达人。
            continue
    return tuple(normalized)


def _normalize_datatap_xiaohongshu_candidate(
    item: ToolEvidence, candidate: Any
) -> NormalizedKolEvidence:
    if not isinstance(candidate, dict):
        raise ValueError("invalid_datatap_xiaohongshu_candidate")
    account_id = _required_string(candidate, "账号ID (kwUid)")
    platform = _optional_string(candidate.get("平台")) or "xiaohongshu"
    if platform != "xiaohongshu":
        raise ValueError("invalid_datatap_xiaohongshu_platform")
    engagement_rate = _first_percentage(
        candidate,
        "互动率-图文笔记",
        "互动率-视频笔记",
    )
    quoted_price = _first_currency(
        candidate,
        "预估报价-视频",
        "预估报价-图文",
        "官方报价-视频",
        "官方报价-图文",
    )
    risk_flags: list[dict[str, Any]] = []
    if candidate.get("近30天是否有发文") is False:
        risk_flags.append({"type": "inactive_last_30_days"})
    if candidate.get("是否活跃") is False:
        risk_flags.append({"type": "activity_flag_false"})
    fields = {
        "nickname": _optional_string(candidate.get("昵称")),
        "normalized_profile_url": _optional_string(candidate.get("主页")),
        "followers": _unit_integer(candidate.get("粉丝数")),
        "engagement_rate": engagement_rate,
        "quoted_price_cny": quoted_price,
        "content_score": _score(candidate.get("综合评分")),
        "audience_score": _percentage(candidate.get("有效粉丝率")),
        "engagement_score": engagement_rate,
        "budget_score": None,
        "growth_score": None,
        "brand_safety_score": None,
    }
    missing = tuple(name for name, value in fields.items() if value is None)
    return _with_provenance(
        NormalizedKolEvidence(
            platform=platform,
            platform_account_id=account_id,
            risk_flags=tuple(redact_evidence_for_storage(risk_flags)),
            collected_at=item.collected_at,
            evidence_references=(item.source_call_id,) if item.source_call_id else (),
            missing_fields=missing,
            export_fields=_extract_export_fields(candidate),
            analytics_fields=_extract_analytics_fields(candidate),
            **fields,
        )
    )


def _datatap_douyin_search_adapter(item: ToolEvidence) -> tuple[NormalizedKolEvidence, ...]:
    raw_result = item.payload.get("result")
    if not isinstance(raw_result, str):
        raise ValueError("invalid_datatap_douyin_result")
    try:
        payload = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_datatap_douyin_result") from exc
    candidates = payload.get("KOL 列表") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("invalid_datatap_douyin_candidates")
    normalized: list[NormalizedKolEvidence] = []
    for candidate in candidates:
        try:
            normalized.append(_normalize_datatap_douyin_candidate(item, candidate))
        except (TypeError, ValueError):
            # 单行坏数据（缺身份、评分越界等）不应拖垮整页达人。
            continue
    return tuple(normalized)


_DATATAP_PLATFORM_BY_TOOL = {
    "datatap.social.grow.kol.bilibili.search.v1": "bilibili",
    "datatap.social.grow.kol.weibo.search.v1": "weibo",
    "datatap.social.grow.kol.wechat.search.v1": "wechat",
    "datatap.social.grow.kol.detail.v1": None,
}
_GENERIC_ACCOUNT_ID_KEYS = (
    "账号ID (kwUid)",
    "账号ID",
    "达人ID",
    "account_id",
    "user_id",
    "author_id",
    "sec_uid",
    "mid",
    "uid",
    "账号",
    "id",
)
_GENERIC_NICKNAME_KEYS = ("昵称", "达人昵称", "达人名称", "name", "nickname")
_GENERIC_PROFILE_KEYS = ("主页", "达人主页", "主页链接", "profile_url")
_GENERIC_FOLLOWER_KEYS = ("粉丝数", "粉丝数量", "粉丝", "followers")
_GENERIC_PLATFORM_ALIASES = {
    "小红书": "xiaohongshu",
    "xiaohongshu": "xiaohongshu",
    "抖音": "douyin",
    "douyin": "douyin",
    "b站": "bilibili",
    "哔哩哔哩": "bilibili",
    "bilibili": "bilibili",
    "微博": "weibo",
    "weibo": "weibo",
    "微信": "wechat",
    "wechat": "wechat",
}


def _datatap_generic_kol_search_adapter(item: ToolEvidence) -> tuple[NormalizedKolEvidence, ...]:
    """解析 DataTap 其它平台的统一达人列表响应。

    当前 DataTap 的 B 站、微博和微信工具使用与平台专用工具相同的
    ``result`` JSON 包装，但字段会随平台略有差异。这里仅提取统一身份和
    白名单指标，避免把平台原始响应写入报告快照。
    """
    raw_result = item.payload.get("result")
    if not isinstance(raw_result, str):
        raise ValueError("invalid_datatap_generic_result")
    try:
        payload = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_datatap_generic_result") from exc
    candidates = _datatap_candidate_rows(payload)
    # kol.detail 是多平台工具：平台不在行内时回退到证据构建时注入的
    # payload 级 platform 提示（来自计划步骤参数）。
    platform_hint = _DATATAP_PLATFORM_BY_TOOL[item.internal_tool_name]
    if platform_hint is None and isinstance(item.payload.get("platform"), str):
        platform_hint = item.payload["platform"]
    normalized: list[NormalizedKolEvidence] = []
    for candidate in candidates:
        try:
            normalized.append(_normalize_datatap_generic_candidate(item, candidate, platform_hint))
        except (TypeError, ValueError):
            # Summary rows without a stable creator identity must not fail the
            # entire cross-platform task.
            continue
    return tuple(normalized)


def _datatap_candidate_rows(payload: Any) -> list[dict[str, Any]]:
    preferred = (
        "KOL 列表",
        "达人信息列表",
        "达人列表",
        "达人详情列表",
        "详情列表",
        "kol_list",
        "creators",
        "authors",
        "items",
        "rows",
        "list",
    )

    def visit(node: Any) -> list[dict[str, Any]] | None:
        if isinstance(node, dict):
            if any(key in node for key in _GENERIC_ACCOUNT_ID_KEYS):
                return [node]
            for key in preferred:
                value = node.get(key)
                # 空列表是合法的“无匹配结果”，不是格式错误。
                if isinstance(value, list) and (
                    not value or all(isinstance(item, dict) for item in value)
                ):
                    return value
            for value in node.values():
                found = visit(value)
                if found:
                    return found
        elif isinstance(node, list) and all(isinstance(item, dict) for item in node):
            if any(any(key in item for key in _GENERIC_ACCOUNT_ID_KEYS) for item in node):
                return node
            for item in node:
                found = visit(item)
                if found:
                    return found
        return None

    rows = visit(payload)
    if rows is not None:
        return rows
    raise ValueError("invalid_datatap_generic_candidates")


def extract_kol_uids(structured_content: Any, *, limit: int = 10) -> list[str]:
    """从已结算的 KOL 搜索证据中提取达人 uid，供详情调用回填。

    搜索类工具的证据是 ``{"result": "<json string>"}`` 包装；解析失败或
    无候选时返回空列表，调用方据此跳过详情调用（不发起、不计费）。
    """
    if not isinstance(structured_content, dict):
        return []
    raw_result = structured_content.get("result")
    if not isinstance(raw_result, str):
        return []
    try:
        payload = json.loads(raw_result)
    except json.JSONDecodeError:
        return []
    try:
        rows = _datatap_candidate_rows(payload)
    except ValueError:
        return []
    uids: list[str] = []
    for row in rows:
        uid = _first_present_string(row, _GENERIC_ACCOUNT_ID_KEYS)
        if uid:
            uids.append(uid)
    return list(dict.fromkeys(uids))[:limit]


def _first_present_string(candidate: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _optional_string(candidate.get(key))
        if value is not None:
            return value
    return None


def _loose_string(value: Any) -> str | None:
    """容错版字符串提取：非字符串先 str()，空串归一为 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_INSIGHT_USER_NICKNAME_KEYS = ("用户昵称", "昵称", "达人昵称")
_INSIGHT_USER_ID_KEYS = ("用户ID", "用户id", "uid", "user_id")
_INSIGHT_USER_FOLLOWER_KEYS = ("用户粉丝数", "粉丝数", "粉丝数量")
_INSIGHT_USER_EXPORT_KEYS = {
    "volume": "声量",
    "total_interactions": "互动数",
    "total_likes": "点赞数",
    "total_comments": "评论数",
    "total_favorites": "收藏数",
}


def _datatap_insight_user_stats_adapter(item: ToolEvidence) -> tuple[NormalizedKolEvidence, ...]:
    """解析 insight 按用户维度统计的 per-user 行（query.analysis / hot.user）。

    result 是 JSON 数组字符串，行形如
    ``{"用户ID": ..., "用户昵称": ..., "声量": ..., "用户粉丝数": ...}``。
    无 用户昵称 的行是大盘统计形态（该工具多数调用不是 per-user 形态），
    跳过不报错；缺 用户ID 时按 platform+nickname 派生稳定身份。平台来自
    证据构建时从 arguments.datasource 注入的 payload 级 platform 提示。
    """
    raw_result = item.payload.get("result")
    if isinstance(raw_result, str):
        try:
            raw_result = json.loads(raw_result)
        except json.JSONDecodeError:
            return ()
    platform_hint = item.payload.get("platform")
    platform = (
        _GENERIC_PLATFORM_ALIASES.get(platform_hint.strip().casefold())
        if isinstance(platform_hint, str)
        else None
    )
    if platform is None:
        return ()
    normalized: list[NormalizedKolEvidence] = []
    for row in _insight_user_rows(raw_result):
        parsed = _normalize_insight_user_row(item, row, platform)
        if parsed is not None:
            normalized.append(parsed)
    return tuple(normalized)


def _insight_user_rows(value: Any) -> tuple[dict[str, Any], ...]:
    candidates: Any = value
    if isinstance(value, dict):
        candidates = []
        for key in ("data", "items", "rows", "list", "records", "result"):
            child = value.get(key)
            if isinstance(child, list):
                candidates = child
                break
    if not isinstance(candidates, list):
        return ()
    return tuple(row for row in candidates if isinstance(row, dict))


def _normalize_insight_user_row(
    item: ToolEvidence, row: dict[str, Any], platform: str
) -> NormalizedKolEvidence | None:
    nickname = next(
        (text for key in _INSIGHT_USER_NICKNAME_KEYS if (text := _loose_string(row.get(key)))),
        None,
    )
    if nickname is None:
        # 大盘统计形态（如 [{"日期": ..., "声量": ...}]）：不是达人证据。
        return None
    account_id = next(
        (text for key in _INSIGHT_USER_ID_KEYS if (text := _loose_string(row.get(key)))),
        None,
    )
    if account_id is None:
        identity_material = _stable_json(
            {"platform": platform, "nickname": nickname, "profile": None}
        ).encode("utf-8")
        account_id = f"{platform}:{hashlib.sha256(identity_material).hexdigest()[:24]}"
    followers = next(
        (
            parsed
            for key in _INSIGHT_USER_FOLLOWER_KEYS
            if (parsed := _unit_integer(row.get(key))) is not None
        ),
        None,
    )
    export_fields: dict[str, Any] = {}
    for target, source_key in _INSIGHT_USER_EXPORT_KEYS.items():
        try:
            value = _non_negative_number(row.get(source_key))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if _has_value(value):
            export_fields[target] = value
    fields = {
        "nickname": nickname,
        "normalized_profile_url": None,
        "followers": followers,
        "engagement_rate": None,
        "quoted_price_cny": None,
        "content_score": None,
        "audience_score": None,
        "engagement_score": None,
        "budget_score": None,
        "growth_score": None,
        "brand_safety_score": None,
    }
    missing = tuple(name for name, value in fields.items() if value is None)
    return _with_provenance(
        NormalizedKolEvidence(
            platform=platform,
            platform_account_id=account_id,
            risk_flags=(),
            collected_at=item.collected_at,
            evidence_references=(item.source_call_id,) if item.source_call_id else (),
            missing_fields=missing,
            export_fields=redact_evidence_for_storage(export_fields),
            analytics_fields={},
            **fields,
        )
    )


def _normalize_datatap_generic_candidate(
    item: ToolEvidence, candidate: dict[str, Any], platform: str | None
) -> NormalizedKolEvidence:
    account_id = _first_present_string(candidate, _GENERIC_ACCOUNT_ID_KEYS)
    nickname = _first_present_string(candidate, _GENERIC_NICKNAME_KEYS)
    if account_id is None:
        if nickname is None:
            raise ValueError("missing_required_identity:account_id")
        # Some platforms intentionally omit a stable account id from search
        # results. Use a deterministic in-memory identity so the named KOL can
        # still enter the report and be merged across repeated calls without
        # storing the provider's raw payload.
        identity_material = _stable_json(
            {"platform": platform, "nickname": nickname, "profile": _first_present_string(candidate, _GENERIC_PROFILE_KEYS)}
        ).encode("utf-8")
        account_id = f"{platform}:{hashlib.sha256(identity_material).hexdigest()[:24]}"
    reported_platform = _first_present_string(candidate, ("平台", "platform"))
    if platform is None and reported_platform:
        platform = _GENERIC_PLATFORM_ALIASES.get(reported_platform.casefold())
    if platform is None:
        raise ValueError("missing_required_identity:platform")
    if reported_platform:
        reported_platform = reported_platform.casefold()
        if platform not in reported_platform and reported_platform not in {platform, "b站"}:
            raise ValueError("invalid_datatap_generic_platform")
    engagement_rate = _first_percentage(
        candidate,
        "互动率",
        "互动率-日常作品",
        "互动率-商单作品",
        "互动率-图文笔记",
        "互动率-视频笔记",
        "engagement_rate",
    )
    # kol.detail 行的指标藏在嵌套 section 里：发帖汇总/受众画像/商业表现。
    detail_stats = candidate.get("发帖数据-汇总统计")
    works_count = average_interactions = average_reads = None
    if isinstance(detail_stats, dict):
        works_count = _first_value(
            detail_stats, ("作品数", "笔记数", "视频数"), _non_negative_number
        )
        average_interactions = _first_value(
            detail_stats, ("平均互动", "平均互动量", "平均互动数"), _non_negative_number
        )
        average_reads = _first_value(
            detail_stats, ("平均阅读", "平均阅读量", "平均播放", "平均播放量"),
            _non_negative_number,
        )
    if engagement_rate is None and average_interactions is not None and average_reads:
        # 详情行无互动率字段时用 平均互动/平均阅读 估算（与搜索适配器同一 0-100 口径）。
        engagement_rate = min(round(average_interactions / average_reads * 100, 2), 100.0)
    fields = {
        "nickname": nickname,
        "normalized_profile_url": _first_present_string(candidate, _GENERIC_PROFILE_KEYS),
        "followers": _first_value(candidate, _GENERIC_FOLLOWER_KEYS, _unit_integer),
        "engagement_rate": engagement_rate,
        "quoted_price_cny": _first_currency(candidate, "预估报价", "报价", "quoted_price_cny"),
        "content_score": _first_value(candidate, ("综合评分", "content_score"), _score),
        "audience_score": _first_value(candidate, ("有效粉丝率", "audience_score"), _percentage),
        "engagement_score": engagement_rate,
        "budget_score": _first_value(candidate, ("预算匹配度", "budget_score"), _score),
        "growth_score": _first_value(candidate, ("增长评分", "growth_score"), _score),
        "brand_safety_score": _first_value(candidate, ("品牌安全评分", "brand_safety_score"), _score),
    }
    missing = tuple(name for name, value in fields.items() if value is None)
    return _with_provenance(
        NormalizedKolEvidence(
            platform=platform,
            platform_account_id=account_id,
            risk_flags=(),
            collected_at=item.collected_at,
            evidence_references=(item.source_call_id,) if item.source_call_id else (),
            missing_fields=missing,
            export_fields=_detail_export_fields(
                candidate,
                works_count=works_count,
                average_interactions=average_interactions,
                average_reads=average_reads,
            ),
            analytics_fields=_extract_analytics_fields(candidate),
            **fields,
        )
    )


def _detail_export_fields(
    candidate: dict[str, Any],
    *,
    works_count: int | float | None,
    average_interactions: int | float | None,
    average_reads: int | float | None,
) -> dict[str, Any]:
    """kol.detail 嵌套 section → 导出字段；缺失/空 section 容忍，不写空值。"""
    export_fields = _extract_export_fields(candidate)
    if works_count is not None:
        export_fields.setdefault("works_count", works_count)
    if average_interactions is not None:
        export_fields.setdefault("average_interactions", average_interactions)
    if average_reads is not None:
        export_fields.setdefault("average_reads", average_reads)
    audience = candidate.get("受众画像")
    if isinstance(audience, dict):
        for label, rate in _safe_distribution(audience.get("粉丝年龄分布")).items():
            bucket = _age_bucket(label)
            if bucket is not None:
                export_fields.setdefault(bucket, rate)
        regions = _safe_distribution(
            audience.get("粉丝省份分布Top10") or audience.get("粉丝省份分布")
        )
        if regions:
            export_fields.setdefault("province", max(regions, key=lambda key: regions[key]))
        interests = _safe_distribution(audience.get("粉丝兴趣分布"))
        if interests:
            top = sorted(interests, key=lambda key: interests[key], reverse=True)[:5]
            export_fields.setdefault("content_tags", "、".join(top))
    for section_key, export_key in (
        ("商业表现-蒲公英商单", "business_order_stats"),
        ("商业表现-品牌提及", "brand_mention_stats"),
    ):
        section = candidate.get(section_key)
        summary = section.get("汇总统计") if isinstance(section, dict) else None
        if isinstance(summary, dict) and summary:
            cleaned = redact_evidence_for_storage(summary)
            if _has_value(cleaned):
                export_fields.setdefault(export_key, cleaned)
    return export_fields


def _safe_distribution(value: Any) -> dict[str, int | float]:
    try:
        return _distribution(value)
    except (InvalidOperation, TypeError, ValueError):
        return {}


def _age_bucket(label: str) -> str | None:
    """受众年龄分布标签 → 导出模板五档年龄桶；无法识别返回 None。"""
    text = label.strip()
    numbers = [int(part) for part in re.findall(r"\d+", text)]
    if not numbers:
        return None
    if text.startswith("<") or "以下" in text:
        return "age_under_18"
    if text.startswith(">") or "以上" in text:
        return "age_over_44"
    low = numbers[0]
    if low < 18:
        return "age_under_18"
    if low < 25:
        return "age_18_24"
    if low < 35:
        return "age_25_34"
    if low < 45:
        return "age_35_44"
    return "age_over_44"


def _first_value(candidate: dict[str, Any], keys: tuple[str, ...], parser: Callable[[Any], Any]) -> Any:
    for key in keys:
        value = candidate.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = parser(value)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if parsed is not None:
            return parsed
    return None


def _normalize_datatap_douyin_candidate(
    item: ToolEvidence, candidate: Any
) -> NormalizedKolEvidence:
    if not isinstance(candidate, dict):
        raise ValueError("invalid_datatap_douyin_candidate")
    account_id = _required_string(candidate, "账号ID (kwUid)")
    platform = _optional_string(candidate.get("平台")) or "douyin"
    if platform != "douyin":
        raise ValueError("invalid_datatap_douyin_platform")
    quoted_price = _first_price_list_value(candidate.get("预估报价"))
    engagement_rate = _first_percentage(
        candidate,
        "互动率-日常作品",
        "互动率-商单作品",
    )
    risk_flags: list[dict[str, Any]] = []
    if candidate.get("近30天有发文") is False:
        risk_flags.append({"type": "inactive_last_30_days"})
    fields = {
        "nickname": _optional_string(candidate.get("昵称")),
        "normalized_profile_url": _optional_string(candidate.get("达人主页")),
        "followers": _unit_integer(candidate.get("抖音粉丝数")),
        "engagement_rate": engagement_rate,
        "quoted_price_cny": quoted_price,
        "content_score": _score(candidate.get("综合评分")),
        "audience_score": _percentage(candidate.get("有效粉丝率")),
        "engagement_score": engagement_rate,
        "budget_score": None,
        "growth_score": None,
        "brand_safety_score": None,
    }
    missing = tuple(name for name, value in fields.items() if value is None)
    return _with_provenance(
        NormalizedKolEvidence(
            platform=platform,
            platform_account_id=account_id,
            risk_flags=tuple(redact_evidence_for_storage(risk_flags)),
            collected_at=item.collected_at,
            evidence_references=(item.source_call_id,) if item.source_call_id else (),
            missing_fields=missing,
            export_fields=_extract_export_fields(candidate),
            analytics_fields=_extract_analytics_fields(candidate),
            **fields,
        )
    )


def _normalize_candidate(item: ToolEvidence, candidate: Any) -> NormalizedKolEvidence:
    if not isinstance(candidate, dict):
        raise ValueError("invalid_creator_candidate")
    platform = _required_string(candidate, "platform")
    account_id = _required_string(candidate, "account_id")
    profile_url = _optional_string(candidate.get("profile_url"))
    fields = {
        "nickname": _optional_string(candidate.get("nickname")),
        "normalized_profile_url": profile_url.rstrip("/") if profile_url else None,
        "followers": _unit_integer(candidate.get("followers")),
        "engagement_rate": _percentage(candidate.get("engagement_rate")),
        "quoted_price_cny": _currency_cny(candidate.get("quoted_price_cny")),
        "content_score": _score(candidate.get("content_score")),
        "audience_score": _score(candidate.get("audience_score")),
        "engagement_score": _score(candidate.get("engagement_score")),
        "budget_score": _score(candidate.get("budget_score")),
        "growth_score": _score(candidate.get("growth_score")),
        "brand_safety_score": _score(candidate.get("brand_safety_score")),
    }
    flags = candidate.get("risk_flags", [])
    if not isinstance(flags, list) or not all(isinstance(flag, dict) for flag in flags):
        raise ValueError("invalid_risk_flags")
    redacted_flags = redact_evidence_for_storage(flags)
    missing = tuple(name for name, value in fields.items() if value is None)
    return _with_provenance(
        NormalizedKolEvidence(
            platform=platform,
            platform_account_id=account_id,
            risk_flags=tuple(redacted_flags),
            collected_at=item.collected_at,
            evidence_references=(item.source_call_id,) if item.source_call_id else (),
            missing_fields=missing,
            export_fields=_extract_export_fields(candidate),
            analytics_fields=_extract_analytics_fields(candidate),
            **fields,
        )
    )


def _merge(
    previous: NormalizedKolEvidence, current: NormalizedKolEvidence
) -> NormalizedKolEvidence:
    previous = _with_provenance(previous)
    current = _with_provenance(current)
    if (previous.platform, previous.platform_account_id) != (
        current.platform,
        current.platform_account_id,
    ):
        raise ValueError("cannot_merge_different_creators")

    previous_provenance = dict(previous.field_provenance)
    current_provenance = dict(current.field_provenance)
    merged_provenance: dict[str, EvidencePriority] = {}

    def select(
        scope: str,
        name: str,
        previous_value: Any,
        current_value: Any,
        *,
        present: Callable[[Any], bool],
    ) -> Any:
        key = f"{scope}.{name}"
        candidates: list[tuple[EvidencePriority, str, Any]] = []
        if present(previous_value):
            candidates.append(
                (
                    previous_provenance.get(key, _evidence_priority(previous)),
                    _stable_json(previous_value),
                    previous_value,
                )
            )
        if present(current_value):
            candidates.append(
                (
                    current_provenance.get(key, _evidence_priority(current)),
                    _stable_json(current_value),
                    current_value,
                )
            )
        if not candidates:
            return None
        priority, _, value = max(candidates, key=lambda candidate: candidate[:2])
        merged_provenance[key] = priority
        return value

    values = {
        name: select(
            "scalar",
            name,
            getattr(previous, name),
            getattr(current, name),
            present=lambda value: value is not None,
        )
        for name in _MERGEABLE_FIELDS
    }
    export_names = sorted(set(previous.export_fields) | set(current.export_fields))
    export_fields = {
        name: value
        for name in export_names
        if (
            value := select(
                "export",
                name,
                previous.export_fields.get(name),
                current.export_fields.get(name),
                present=_has_value,
            )
        )
        is not None
    }
    analytics_fields = {
        name: value
        for name in _ANALYTICS_FIELD_ORDER
        if (
            value := select(
                "analytics",
                name,
                previous.analytics_fields.get(name),
                current.analytics_fields.get(name),
                present=_has_value,
            )
        )
        is not None
    }
    previous_priority = _evidence_priority(previous)
    current_priority = _evidence_priority(current)
    primary = previous if previous_priority >= current_priority else current
    flags_by_digest = {
        _stable_json(flag): flag
        for flag in previous.risk_flags + current.risk_flags
    }
    missing = tuple(name for name, value in values.items() if value is None)
    return NormalizedKolEvidence(
        platform=primary.platform,
        platform_account_id=primary.platform_account_id,
        risk_flags=tuple(flags_by_digest[key] for key in sorted(flags_by_digest)),
        collected_at=primary.collected_at,
        evidence_references=tuple(
            sorted(set(previous.evidence_references + current.evidence_references))
        ),
        missing_fields=missing,
        export_fields=export_fields,
        analytics_fields=analytics_fields,
        evidence_priority=max(previous_priority, current_priority),
        field_provenance=tuple(sorted(merged_provenance.items())),
        **values,
    )


def _merge_many(records: Iterable[NormalizedKolEvidence]) -> NormalizedKolEvidence:
    records = tuple(records)
    if not records:
        raise ValueError("cannot_merge_empty_evidence")
    return reduce(_merge, records[1:], _with_provenance(records[0]))


def _evidence_priority(item: NormalizedKolEvidence) -> EvidencePriority:
    if item.evidence_priority is not None:
        return item.evidence_priority
    collected_at = item.collected_at
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=timezone.utc)
    return (
        collected_at.timestamp(),
        max(item.evidence_references, default=""),
        hashlib.sha256(_stable_json(item.as_dict()).encode("utf-8")).hexdigest(),
    )


def _with_provenance(item: NormalizedKolEvidence) -> NormalizedKolEvidence:
    if item.evidence_priority is not None and item.field_provenance:
        return item
    priority = _evidence_priority(item)
    provenance = {
        f"scalar.{name}": priority
        for name in _MERGEABLE_FIELDS
        if getattr(item, name) is not None
    }
    provenance.update(
        {
            f"export.{name}": priority
            for name, value in item.export_fields.items()
            if _has_value(value)
        }
    )
    provenance.update(
        {
            f"analytics.{name}": priority
            for name, value in item.analytics_fields.items()
            if name in _ANALYTICS_FIELD_ORDER and _has_value(value)
        }
    )
    return replace(
        item,
        evidence_priority=priority,
        field_provenance=tuple(sorted(provenance.items())),
    )


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


_EXPORT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "city": ("城市", "所在城市", "城市标签", "达人城市", "粉丝主要城市"),
    "total_likes": ("总获赞", "获赞数", "总点赞数", "点赞数", "累计获赞"),
    "total_favorites": ("总收藏", "收藏数", "总收藏数", "累计收藏"),
    "average_reads": ("平均阅读", "平均阅读量", "平均播放", "平均播放量"),
    "average_interactions": ("平均互动", "平均互动量", "平均互动数"),
    "active_follower_rate": ("活跃粉丝率", "活跃粉丝比例", "粉丝活跃率"),
    "content_tags": ("内容标签", "内容标签分布", "兴趣标签", "内容兴趣"),
    "gender": ("性别", "粉丝性别"),
    "target_region_rate": ("目标地区粉丝占比", "浙江粉丝占比", "湖州粉丝占比", "地域占比"),
    "target_age_rate": ("目标年龄段占比", "年龄段占比", "粉丝年龄占比"),
    "industry_interest_rate": ("行业兴趣占比", "类目兴趣占比", "品类兴趣占比"),
}


def _extract_export_fields(candidate: dict[str, Any]) -> dict[str, Any]:
    """从 MCP 候选中提取模板字段并脱敏，不把未知原始字段写入快照。"""
    fields: dict[str, Any] = {}
    for target, aliases in _EXPORT_FIELD_ALIASES.items():
        for alias in aliases:
            value = candidate.get(alias)
            if value not in (None, "", [], {}):
                cleaned = redact_evidence_for_storage(value)
                if cleaned not in (None, "", [], {}):
                    fields[target] = cleaned
                    break
    return fields


_ANALYTICS_FIELD_ORDER = (
    "brand_mentions",
    "exposure",
    "interactions",
    "published_at",
    "sentiment_counts",
    "hot_words",
    "audience_age",
    "audience_gender",
    "audience_regions",
)
_ANALYTICS_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "brand_mentions": (
        "brand_mentions",
        "brand_mention_count",
        "brand_mentions_count",
        "品牌提及数",
        "品牌提及次数",
        "品牌提及量",
        "品牌声量",
    ),
    "exposure": (
        "exposure",
        "exposure_count",
        "impressions",
        "impression_count",
        "views",
        "view_count",
        "total_views",
        "曝光量",
        "曝光数",
        "总曝光",
        "阅读量",
        "播放量",
        "总播放量",
    ),
    "interactions": (
        "interactions",
        "interaction_count",
        "total_interactions",
        "互动量",
        "互动数",
        "总互动量",
        "总互动数",
    ),
    "published_at": (
        "published_at",
        "publish_date",
        "publish_dates",
        "publication_date",
        "发布时间",
        "发布日期",
        "发布日",
    ),
    "sentiment_counts": (
        "sentiment_counts",
        "sentiment",
        "sentiment_distribution",
        "舆情分布",
        "情感统计",
        "情感分布",
    ),
    "hot_words": ("hot_words", "keywords", "keyword_counts", "热词", "热点词"),
    "audience_age": (
        "audience_age",
        "age_distribution",
        "audience_age_distribution",
        "粉丝年龄分布",
        "受众年龄分布",
        "年龄分布",
    ),
    "audience_gender": (
        "audience_gender",
        "gender_distribution",
        "audience_gender_distribution",
        "粉丝性别分布",
        "受众性别分布",
        "性别分布",
    ),
    "audience_regions": (
        "audience_regions",
        "region_distribution",
        "audience_region_distribution",
        "粉丝地域分布",
        "粉丝省份分布Top10",
        "粉丝省份分布",
        "受众地域分布",
        "地域分布",
    ),
}
_SENTIMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "positive": ("positive", "pos", "正向", "正面", "积极"),
    "neutral": ("neutral", "neu", "中立", "中性"),
    "negative": ("negative", "neg", "负向", "负面", "消极"),
}


def _extract_analytics_fields(candidate: dict[str, Any]) -> dict[str, Any]:
    """只投影 BI 白名单字段；单个可选字段无效时保留达人并跳过该字段。"""
    parsers: dict[str, Callable[[Any], Any]] = {
        "brand_mentions": _non_negative_number,
        "exposure": _non_negative_number,
        "interactions": _non_negative_number,
        "published_at": _published_at,
        "sentiment_counts": _sentiment_counts,
        "hot_words": _hot_words,
        "audience_age": _distribution,
        "audience_gender": _distribution,
        "audience_regions": _distribution,
    }
    fields: dict[str, Any] = {}
    for target in _ANALYTICS_FIELD_ORDER:
        value = _first_analytics_value(candidate, _ANALYTICS_FIELD_ALIASES[target])
        if not _has_value(value):
            continue
        try:
            normalized = parsers[target](value)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if _has_value(normalized):
            fields[target] = normalized
    return fields


def _analytics_sources(candidate: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    nested = tuple(
        value
        for key in ("analytics_fields", "analytics", "bi_fields", "BI字段", "受众画像", "粉丝画像")
        if isinstance((value := candidate.get(key)), dict)
    )
    return (candidate, *nested)


def _first_analytics_value(candidate: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for source in _analytics_sources(candidate):
        for alias in aliases:
            value = source.get(alias)
            if _has_value(value):
                return value
    return None


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _non_negative_number(value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValueError("invalid_analytics_number")
    text = str(value).strip().replace(",", "")
    multiplier = Decimal(1)
    if text.endswith("万"):
        text, multiplier = text[:-1], Decimal(10_000)
    elif text.casefold().endswith("k"):
        text, multiplier = text[:-1], Decimal(1_000)
    number = _decimal(text)
    if (
        number is None
        or number < 0
        or number > _MAX_ANALYTICS_NUMBER / multiplier
    ):
        raise ValueError("invalid_analytics_number")
    number *= multiplier
    return int(number) if number == number.to_integral_value() else float(number)


def _distribution_percentage(value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValueError("invalid_distribution_percentage")
    text = str(value).strip()
    has_percent_suffix = text.endswith("%")
    number = _decimal(text[:-1] if has_percent_suffix else text)
    if number is None:
        raise ValueError("invalid_distribution_percentage")
    if not has_percent_suffix and number <= 1:
        number *= 100
    if number < 0 or number > 100:
        raise ValueError("invalid_distribution_percentage")
    return int(number) if number == number.to_integral_value() else float(number)


def _non_negative_count(value: Any) -> int:
    number = _non_negative_number(value)
    if not isinstance(number, int):
        raise ValueError("invalid_analytics_count")
    return number


def _published_at(value: Any) -> str | list[str]:
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("invalid_published_at")
        return [_parsed_date(item) for item in value]
    return _parsed_date(value)


def _parsed_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError("invalid_published_at")
    text = value.strip()
    if not text or redact_evidence_for_storage(text) in ({}, _DROP):
        raise ValueError("invalid_published_at")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).isoformat()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValueError("invalid_published_at") from exc
    return parsed.isoformat()


def _sentiment_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("invalid_sentiment_counts")
    result: dict[str, int] = {}
    for target, aliases in _SENTIMENT_ALIASES.items():
        for alias in aliases:
            if alias in value and value[alias] is not None:
                result[target] = _non_negative_count(value[alias])
                break
    if not result:
        raise ValueError("invalid_sentiment_counts")
    return result


def _safe_term(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_hot_word")
    term = value.strip()
    if (
        not term
        or len(term) > 100
        or _CREDENTIAL_VALUE_PATTERN.fullmatch(term)
        or _INTERNAL_PATH_PATTERN.search(term)
        or _URI_SCHEME_PATTERN.match(term)
        or _ABSOLUTE_PATH_PATTERN.match(term)
        or redact_evidence_for_storage(term) in ({}, _DROP)
    ):
        raise ValueError("invalid_hot_word")
    return term


def _hot_words(value: Any) -> list[str] | list[dict[str, Any]]:
    if isinstance(value, dict):
        result = []
        for term in sorted(value):
            result.append({"term": _safe_term(term), "count": _non_negative_count(value[term])})
        if not result:
            raise ValueError("invalid_hot_words")
        return result
    if not isinstance(value, (list, tuple)):
        raise ValueError("invalid_hot_words")
    strings: list[str] = []
    counted: list[dict[str, Any]] = []
    for item in value:
        try:
            if isinstance(item, str):
                strings.append(_safe_term(item))
                continue
            if not isinstance(item, dict):
                continue
            term = next(
                (item[key] for key in ("term", "word", "keyword", "词", "热词") if key in item),
                None,
            )
            count = next(
                (item[key] for key in ("count", "frequency", "freq", "次数", "频次") if key in item),
                None,
            )
            counted.append({"term": _safe_term(term), "count": _non_negative_count(count)})
        except (TypeError, ValueError):
            continue
    if counted and not strings:
        return list({(item["term"], item["count"]): item for item in counted}.values())
    if strings and not counted:
        return list(dict.fromkeys(strings))
    if not strings and not counted:
        raise ValueError("invalid_hot_words")
    raise ValueError("mixed_hot_word_shapes")


def _distribution(value: Any) -> dict[str, int | float]:
    if isinstance(value, dict):
        result = {
            _safe_term(name): _distribution_percentage(number)
            for name, number in value.items()
        }
    elif isinstance(value, (list, tuple)):
        result = {}
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("invalid_distribution")
            name = next(
                (
                    item[key]
                    for key in ("name", "label", "range", "region", "gender", "名称", "标签", "区间", "地区", "性别", "年龄", "键")
                    if key in item
                ),
                None,
            )
            number = next(
                (
                    item[key]
                    for key in ("value", "count", "rate", "percent", "占比", "数值", "人数", "值")
                    if key in item
                ),
                None,
            )
            result[_safe_term(name)] = _distribution_percentage(number)
    else:
        raise ValueError("invalid_distribution")
    if not result:
        raise ValueError("invalid_distribution")
    return result


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = _optional_string(payload.get(field))
    if value is None:
        raise ValueError(f"missing_required_identity:{field}")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_string")
    cleaned = value.strip()
    return cleaned or None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_numeric_value")
    text = str(value).strip()
    if not text or len(text) > _MAX_NUMERIC_TEXT_LENGTH:
        raise ValueError("invalid_numeric_value")
    try:
        number = Decimal(text)
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("invalid_numeric_value") from exc
    if (
        not number.is_finite()
        or abs(number.adjusted()) > _MAX_DECIMAL_ADJUSTED_EXPONENT
    ):
        raise ValueError("invalid_numeric_value")
    return number


def _unit_integer(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    multiplier = Decimal(1)
    if text.endswith("万"):
        text, multiplier = text[:-1], Decimal(10_000)
    elif text.lower().endswith("k"):
        text, multiplier = text[:-1], Decimal(1_000)
    number = _decimal(text)
    if number is None:
        return None
    if number < 0 or number > _MAX_GENERAL_NUMBER / multiplier:
        raise ValueError("invalid_numeric_value")
    return int(number * multiplier)


def _percentage(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    number = _decimal(text[:-1] if text.endswith("%") else text)
    if number is None:
        return None
    normalized = number * 100 if number <= 1 and not text.endswith("%") else number
    if normalized < 0 or normalized > 100:
        raise ValueError("percentage_out_of_range")
    return float(normalized)


def _first_percentage(candidate: dict[str, Any], *names: str) -> float | None:
    values = [_percentage(candidate.get(name)) for name in names]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _currency_cny(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.startswith("¥") or text.startswith("￥"):
        text = text[1:]
    multiplier = Decimal(1)
    if text.endswith("万"):
        text, multiplier = text[:-1], Decimal(10_000)
    number = _decimal(text)
    if (
        number is None
        or number < 0
        or number > _MAX_GENERAL_NUMBER / multiplier
    ):
        raise ValueError("invalid_currency_value")
    return float(number * multiplier)


def _first_currency(candidate: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _currency_cny(candidate.get(name))
        if value is not None:
            return value
    return None


def _first_price_list_value(value: Any) -> float | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        parsed = _currency_cny(item.get("值"))
        if parsed is not None:
            return parsed
    return None


def _score(value: Any) -> float | None:
    number = _decimal(value)
    if number is None:
        return None
    if number < 0 or number > 100:
        raise ValueError("score_out_of_range")
    return float(number)


_MERGEABLE_FIELDS = (
    "nickname",
    "normalized_profile_url",
    "followers",
    "engagement_rate",
    "quoted_price_cny",
    "content_score",
    "audience_score",
    "engagement_score",
    "budget_score",
    "growth_score",
    "brand_safety_score",
)

_ADAPTERS: dict[str, Adapter] = {
    "creator.search.v1": _creator_search_adapter,
    "creator.profile.v1": _creator_search_adapter,
    "bilibili.creator.search.v1": _creator_search_adapter,
    "bilibili.creator.profile.v1": _creator_search_adapter,
    "datatap.xiaohongshu.kol.search.v1": _datatap_xiaohongshu_search_adapter,
    "datatap.douyin.kol.search.v1": _datatap_douyin_search_adapter,
    "datatap.social.grow.kol.bilibili.search.v1": _datatap_generic_kol_search_adapter,
    "datatap.social.grow.kol.weibo.search.v1": _datatap_generic_kol_search_adapter,
    "datatap.social.grow.kol.wechat.search.v1": _datatap_generic_kol_search_adapter,
    "datatap.social.grow.kol.detail.v1": _datatap_generic_kol_search_adapter,
    "datatap.insight.query.analysis.v1": _datatap_insight_user_stats_adapter,
    "datatap.insight.social.statistic.hot.user.v1": _datatap_insight_user_stats_adapter,
}
