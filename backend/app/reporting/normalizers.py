from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any
import unicodedata

from app.reporting.schemas import NormalizedKolEvidence, ToolEvidence


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


def normalize_tool_evidence(evidence: Iterable[ToolEvidence]) -> tuple[NormalizedKolEvidence, ...]:
    """只接受评审过的内部工具名，按稳定平台身份合并同一达人。"""
    merged: dict[tuple[str, str], NormalizedKolEvidence] = {}
    for item in evidence:
        adapter = _ADAPTERS.get(item.internal_tool_name)
        if adapter is None:
            raise UnknownEvidenceToolError(f"unknown_evidence_tool:{item.internal_tool_name}")
        for normalized in adapter(item):
            key = (normalized.platform, normalized.platform_account_id)
            previous = merged.get(key)
            merged[key] = normalized if previous is None else _merge(previous, normalized)
    return tuple(merged[key] for key in sorted(merged))


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
    return tuple(_normalize_datatap_xiaohongshu_candidate(item, candidate) for candidate in candidates)


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
    return NormalizedKolEvidence(
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
    return tuple(_normalize_datatap_douyin_candidate(item, candidate) for candidate in candidates)


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
    return NormalizedKolEvidence(
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
    return NormalizedKolEvidence(
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


def _merge(
    previous: NormalizedKolEvidence, current: NormalizedKolEvidence
) -> NormalizedKolEvidence:
    values = {
        name: getattr(previous, name) if getattr(previous, name) is not None else getattr(current, name)
        for name in _MERGEABLE_FIELDS
    }
    export_fields = dict(previous.export_fields)
    export_fields.update({key: value for key, value in current.export_fields.items() if value is not None})
    analytics_fields = {
        name: value
        for name in _ANALYTICS_FIELD_ORDER
        if (
            value := (
                previous.analytics_fields.get(name)
                if _has_value(previous.analytics_fields.get(name))
                else current.analytics_fields.get(name)
            )
        )
        is not None
        and _has_value(value)
    }
    flags = tuple({repr(flag): flag for flag in previous.risk_flags + current.risk_flags}.values())
    missing = tuple(name for name, value in values.items() if value is None)
    return NormalizedKolEvidence(
        platform=previous.platform,
        platform_account_id=previous.platform_account_id,
        risk_flags=flags,
        collected_at=max(previous.collected_at, current.collected_at),
        evidence_references=tuple(
            sorted(set(previous.evidence_references + current.evidence_references))
        ),
        missing_fields=missing,
        export_fields=export_fields,
        analytics_fields=analytics_fields,
        **values,
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
        for key in ("analytics_fields", "analytics", "bi_fields", "BI字段")
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


def _non_negative_number(value: Any, *, allow_percent: bool = False) -> int | float:
    if isinstance(value, bool):
        raise ValueError("invalid_analytics_number")
    text = str(value).strip().replace(",", "")
    if allow_percent and text.endswith("%"):
        text = text[:-1]
    multiplier = Decimal(1)
    if text.endswith("万"):
        text, multiplier = text[:-1], Decimal(10_000)
    elif text.casefold().endswith("k"):
        text, multiplier = text[:-1], Decimal(1_000)
    number = _decimal(text)
    if number is None or not number.is_finite() or number < 0:
        raise ValueError("invalid_analytics_number")
    number *= multiplier
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
    if not term or len(term) > 100 or redact_evidence_for_storage(term) in ({}, _DROP):
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
            _safe_term(name): _non_negative_number(number, allow_percent=True)
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
                    for key in ("name", "label", "range", "region", "gender", "名称", "标签", "区间", "地区", "性别", "年龄")
                    if key in item
                ),
                None,
            )
            number = next(
                (
                    item[key]
                    for key in ("value", "count", "rate", "percent", "占比", "数值", "人数")
                    if key in item
                ),
                None,
            )
            result[_safe_term(name)] = _non_negative_number(number, allow_percent=True)
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
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("invalid_numeric_value") from exc


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
    return int(number * multiplier) if number is not None else None


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
    if number is None or number < 0:
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
}
