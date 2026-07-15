from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
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
}
