from __future__ import annotations

from collections.abc import Callable, Iterable
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
        **fields,
    )


def _merge(
    previous: NormalizedKolEvidence, current: NormalizedKolEvidence
) -> NormalizedKolEvidence:
    values = {
        name: getattr(previous, name) if getattr(previous, name) is not None else getattr(current, name)
        for name in _MERGEABLE_FIELDS
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
        **values,
    )


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
}
