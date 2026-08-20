"""Artifact 强类型 payload 校验边界（v3 加固 §2.4/§2.5/§5.6 / A5）。

发布链路唯一强类型边界：

- ``schema_version`` 必须映射到唯一 Pydantic 类型（``TYPED_PAYLOAD_BY_SCHEMA``）；
- key 模块 / ``schema_version`` / ``artifact_type`` 是固定组合，三者必须一致；
- Artifact key 所需 business fields 非空（拒绝 ``brand:`` 这类裸 key）；
- payload 本体复用既有 payload 类型校验（``extra="forbid"``、URL scheme、
  Top20、评分权重、§2.5 反向聚合：必需章节在 ``availability`` 中齐全、
  ``complete`` 当且仅当全部必需章节 complete、``restricted`` 当且仅当至少一个
  必需章节 partial/unavailable 且有覆盖 limitation、null 不得被当 0）。

校验通过后返回标准化 ``model_dump(mode="json")``（默认值填充、tuple→list、
日期转 ISO 字符串），Draft Revision 与发布 Version 一律保存该形态。

失败抛 ``ArtifactPayloadInvalid``（``code == "artifact_payload_invalid"``）：
Draft 工具层映射为结构化 ToolResult 回喂模型；发布事务内则阻断整批发布，
绝不泄漏为 500。
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from pydantic import ValidationError

from app.agent_artifacts.canonical import CanonicalPayloadMixin, model_direct_lineage_context
from app.agent_artifacts.lineage import ValidationIssue, validate_structured_claims
from app.agent_artifacts.payloads import TYPED_PAYLOAD_BY_SCHEMA


class ArtifactPayloadInvalid(Exception):
    """Draft/Revision payload 未通过强类型校验（结构化错误，非崩溃）。

    ``code == "artifact_payload_invalid"``；``errors`` 携带 Pydantic 错误明细
    （不含上下文对象，JSON 可序列化），供工具层结构化回喂模型。
    """

    code = "artifact_payload_invalid"

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        self.errors = errors or []
        super().__init__(message)


# key 模块 → schema_version 固定组合（与 ``build_artifact_key`` 的模块一一对应）。
SCHEMA_VERSION_BY_MODULE: dict[str, str] = {
    "brand": "brand_report_v3",
    "campaign": "campaign_report_v2",
    "kol-selection": "kol_selection_v3",
    "kol-analysis": "kol_analysis_v2",
    "kol-detail": "kol_detail_v2",
    "insight": "insight_board_v1",
    "report": "analysis_report_v1",
}

# key 模块 → 生成 artifact_key 所需 business fields（拒绝裸 key，§2.4）。
_REQUIRED_BUSINESS_FIELDS: dict[str, tuple[str, ...]] = {
    "brand": ("brand",),
    "campaign": ("brand", "campaign"),
    "kol-selection": ("scope",),
    "kol-analysis": ("selection_artifact_id",),
    "kol-detail": ("platform", "kol_uid"),
    "insight": ("parent_artifact_version_id", "question"),
    "report": ("scope",),
}


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, dict):
        return not value
    return False


def _check_fixed_combo(module: str, schema_version: str, artifact_type: str) -> None:
    expected = SCHEMA_VERSION_BY_MODULE.get(module)
    if expected is None:
        raise ArtifactPayloadInvalid(f"unknown artifact module: {module!r}")
    allowed_schema_versions = {expected}
    if module == "campaign":
        # campaign_report_v2 remains the current/legacy contract.  Pi's
        # direct Artifact Skill may also submit the reviewed v3 alias.
        allowed_schema_versions.add("campaign_report_v3")
    if schema_version not in allowed_schema_versions:
        raise ArtifactPayloadInvalid(
            f"module {module!r} requires schema_version in {sorted(allowed_schema_versions)!r}, "
            f"got {schema_version!r}"
        )
    if artifact_type != schema_version:
        raise ArtifactPayloadInvalid(
            f"artifact_type {artifact_type!r} must equal schema_version {schema_version!r}"
        )


_KOL_SCOPE_REQUIRED = ("brand", "category", "platforms", "audience", "filters")
_KOL_SCOPE_V3_FIELDS = (
    "region",
    "age_range",
    "period",
    "budget",
    "ranking_mode",
    "top_limit",
    "scoring_version",
)
_KOL_PLATFORM_ALIASES = {
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "小红书": "xiaohongshu",
    "douyin": "douyin",
    "抖音": "douyin",
    "短视频": "douyin",
    "kuaishou": "kuaishou",
    "快手": "kuaishou",
    "bilibili": "bilibili",
    "b站": "bilibili",
}
_KOL_INVALID_PLATFORMS = {"unknown", "all", "aggregate", "合计", "全部", "未知", ""}
_KOL_SUPPORTED_PLATFORMS = frozenset({"xiaohongshu", "douyin", "kuaishou", "bilibili"})
_KOL_RANKING_MODES = frozenset({"balanced"})
_KOL_SCORING_VERSIONS = frozenset({"kol_score_v2", "kol_value_score_v3"})


def _kol_platform(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().casefold()
    return _KOL_PLATFORM_ALIASES.get(value, value)


def _kol_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _kol_scope_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, Mapping)):
        return bool(value)
    return value is not None


def _kol_budget_bounds(scope: Mapping[str, Any]) -> tuple[float | None, float | None]:
    budget = scope.get("budget")
    minimum: float | None = None
    maximum: float | None = None
    if isinstance(budget, Mapping):
        raw_min = budget.get("min", budget.get("budget_min"))
        raw_max = budget.get("max", budget.get("budget_max"))
        minimum = (
            float(raw_min)
            if isinstance(raw_min, (int, float))
            and not isinstance(raw_min, bool)
            and isfinite(float(raw_min))
            else None
        )
        maximum = (
            float(raw_max)
            if isinstance(raw_max, (int, float))
            and not isinstance(raw_max, bool)
            and isfinite(float(raw_max))
            else None
        )
    elif (
        isinstance(budget, (int, float))
        and not isinstance(budget, bool)
        and isfinite(float(budget))
    ):
        maximum = float(budget)
    filters = scope.get("filters")
    if isinstance(filters, Mapping):
        raw_min = filters.get("budget_min")
        raw_max = filters.get("budget_max")
        if (
            minimum is None
            and isinstance(raw_min, (int, float))
            and not isinstance(raw_min, bool)
            and isfinite(float(raw_min))
        ):
            minimum = float(raw_min)
        if (
            maximum is None
            and isinstance(raw_max, (int, float))
            and not isinstance(raw_max, bool)
            and isfinite(float(raw_max))
        ):
            maximum = float(raw_max)
    return minimum, maximum


def _kol_observed_score(snapshot: Any) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    dimensions = snapshot.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return False
    for dimension in dimensions.values():
        if not isinstance(dimension, Mapping):
            continue
        if dimension.get("missing_reason") not in (None, ""):
            continue
        source = dimension.get("source")
        # source 是评分输入的可验证痕迹；仅有模型写入的非零 raw_score
        # 不能证明存在真实观测，避免“补分后再发布”。
        if _kol_nonempty(source):
            return True
    return False


def validate_kol_candidates(
    payload: Any, *, require_v3_scope: bool = False
) -> list[ValidationIssue]:
    """校验 KOL 名单是否具备可发布的候选与完整范围。

    这是发布前的纯值对象门禁：不访问数据库、不把缺失评分伪造成有效零值，
    也不把昵称当作稳定身份。历史 v2/v3 payload 的新增 scope 字段使用兼容
    默认值；旧 payload 仍需满足原有范围字段与候选基本身份约束。
    """
    source_scope_fields: set[str] | None = None
    source_scope = getattr(payload, "scope", None)
    if source_scope is not None:
        model_fields_set = getattr(source_scope, "model_fields_set", None)
        if model_fields_set is not None:
            source_scope_fields = set(model_fields_set)
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    if not isinstance(payload, Mapping):
        return [ValidationIssue("kol_payload_invalid", "KOL payload must be an object")]

    issues: list[ValidationIssue] = []
    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        return [ValidationIssue("kol_scope_missing", "KOL selection scope is required", "scope")]
    missing_scope = [field for field in _KOL_SCOPE_REQUIRED if field not in scope]
    if missing_scope:
        issues.append(
            ValidationIssue(
                "kol_scope_incomplete",
                "KOL selection scope is missing required fields",
                "scope",
            )
        )
    raw_data = payload.get("data")
    raw_scoring = raw_data.get("scoring") if isinstance(raw_data, Mapping) else None
    v3_scoring_payload = (
        isinstance(raw_scoring, Mapping) and raw_scoring.get("version") == "kol_value_score_v3"
    )
    present_v3_fields = [field for field in _KOL_SCOPE_V3_FIELDS if field in scope]
    if source_scope_fields is not None:
        present_v3_fields = [field for field in _KOL_SCOPE_V3_FIELDS if field in source_scope_fields]
    strict_v3_scope = require_v3_scope or bool(present_v3_fields)
    if source_scope_fields is None:
        strict_v3_scope = strict_v3_scope or v3_scoring_payload
    if strict_v3_scope and len(present_v3_fields) != len(_KOL_SCOPE_V3_FIELDS):
        issues.append(
            ValidationIssue(
                "kol_scope_incomplete",
                "KOL selection v3 scope fields must be persisted together",
                "scope",
            )
        )

    raw_allowed = scope.get("platforms")
    if strict_v3_scope:
        if not isinstance(raw_allowed, (list, tuple)) or not raw_allowed:
            issues.append(
                ValidationIssue(
                    "kol_scope_platforms_missing",
                    "new KOL selection scope requires a non-empty platform allowlist",
                    "scope.platforms",
                )
            )
        invalid_scope_platforms = [
            item
            for item in (raw_allowed if isinstance(raw_allowed, (list, tuple)) else ())
            if _kol_platform(item) not in _KOL_SUPPORTED_PLATFORMS
        ]
        if invalid_scope_platforms:
            issues.append(
                ValidationIssue(
                    "kol_scope_platform_invalid",
                    "scope platforms must use the supported platform allowlist",
                    "scope.platforms",
                )
            )
        if not scope.get("ranking_mode") or scope.get("ranking_mode") not in _KOL_RANKING_MODES:
            issues.append(
                ValidationIssue(
                    "kol_scope_v3_invalid",
                    "scope ranking_mode is not a supported value",
                    "scope.ranking_mode",
                )
            )
        top_limit = scope.get("top_limit")
        if (
            not isinstance(top_limit, int)
            or isinstance(top_limit, bool)
            or not 1 <= top_limit <= 20
        ):
            issues.append(
                ValidationIssue(
                    "kol_scope_v3_invalid",
                    "scope top_limit must be an integer from 1 to 20",
                    "scope.top_limit",
                )
            )
        scoring_version = scope.get("scoring_version")
        if scoring_version not in _KOL_SCORING_VERSIONS:
            issues.append(
                ValidationIssue(
                    "kol_scope_v3_invalid",
                    "scope scoring_version is not supported",
                    "scope.scoring_version",
                )
        )
        for field in ("region", "age_range", "period"):
            if not _kol_scope_present(scope.get(field)):
                issues.append(
                    ValidationIssue(
                        "kol_scope_v3_invalid",
                        f"scope {field} is required for a new v3 publication",
                        f"scope.{field}",
                    )
                )

    allowed = {
        _kol_platform(item)
        for item in (raw_allowed if isinstance(raw_allowed, (list, tuple, set)) else ())
    }
    allowed.discard("")
    minimum_budget, maximum_budget = _kol_budget_bounds(scope)
    data = payload.get("data")
    items = data.get("items") if isinstance(data, Mapping) else None
    scoring = data.get("scoring") if isinstance(data, Mapping) else None
    if strict_v3_scope and (
        not isinstance(scoring, Mapping)
        or scope.get("scoring_version") != scoring.get("version")
    ):
        issues.append(
            ValidationIssue(
                "kol_scoring_version_mismatch",
                "scope scoring_version must match data scoring version",
                "scope.scoring_version",
            )
        )
    if not isinstance(items, (list, tuple)):
        return issues + [ValidationIssue("kol_items_missing", "KOL selection items are required", "data.items")]
    if not items:
        issues.append(
            ValidationIssue(
                "kol_empty_items",
                "empty KOL selection is a structured gap and cannot be published",
                "data.items",
            )
        )

    item_ids: set[str] = set()
    for index, item in enumerate(items):
        path = f"data.items.{index}"
        if not isinstance(item, Mapping):
            issues.append(ValidationIssue("kol_candidate_invalid", "candidate must be an object", path))
            continue
        nickname = item.get("nickname")
        if not _kol_nonempty(nickname):
            issues.append(ValidationIssue("kol_nickname_missing", "candidate nickname is required", f"{path}.nickname"))
        platform = _kol_platform(item.get("platform"))
        platform_invalid = platform in _KOL_INVALID_PLATFORMS or platform not in _KOL_SUPPORTED_PLATFORMS
        if (
            platform_invalid
            or not platform
            or ((strict_v3_scope or allowed) and platform not in allowed)
        ):
            issues.append(ValidationIssue("kol_platform_invalid", "candidate platform is not allowed", f"{path}.platform"))
        identity = item.get("kol_uid")
        if not _kol_nonempty(identity):
            # 仅接受明确稳定 ID；nickname 不得作为隐式 ID。
            stable = next(
                (
                    item.get(key)
                    for key in ("stable_id", "platform_uid", "author_id", "uid")
                    if _kol_nonempty(item.get(key))
                ),
                None,
            )
            if stable is None:
                issues.append(ValidationIssue("kol_identity_missing", "candidate stable identity is required", f"{path}.kol_uid"))
            else:
                identity = stable
        if _kol_nonempty(identity):
            item_ids.add(str(identity).strip())
        if not _kol_observed_score(item.get("score_snapshot")):
            issues.append(ValidationIssue("kol_scores_missing", "candidate has no observed score input", f"{path}.score_snapshot"))

        snapshot = item.get("score_snapshot")
        quote = item.get("quoted_price")
        enforce_quote_traceability = bool(scope.get("scoring_version"))
        if isinstance(snapshot, Mapping):
            snapshot_quote = snapshot.get("quoted_price")
            if (
                enforce_quote_traceability
                and quote is not None
                and (
                    (snapshot_quote is not None and quote != snapshot_quote)
                    or (snapshot_quote is None and quote != 0)
                )
            ):
                issues.append(ValidationIssue("kol_quote_untraceable", "candidate quote differs from score snapshot", f"{path}.quoted_price"))
            if enforce_quote_traceability and quote is None and snapshot_quote is not None:
                issues.append(ValidationIssue("kol_quote_untraceable", "candidate quote is missing from item", f"{path}.quoted_price"))
            if quote is None:
                quote = snapshot_quote
        if quote is not None and (
            not isinstance(quote, (int, float))
            or isinstance(quote, bool)
            or not isfinite(float(quote))
        ):
            issues.append(ValidationIssue("kol_quote_untraceable", "candidate quote is not numeric", f"{path}.quoted_price"))
        if quote is None and (minimum_budget is not None or maximum_budget is not None):
            issues.append(ValidationIssue("kol_budget_untraceable", "candidate quote cannot be checked against budget", f"{path}.quoted_price"))
        elif isinstance(quote, (int, float)):
            if minimum_budget is not None and quote < minimum_budget:
                issues.append(ValidationIssue("kol_budget_untraceable", "candidate quote is below scope budget", f"{path}.quoted_price"))
            if maximum_budget is not None and quote > maximum_budget:
                issues.append(ValidationIssue("kol_budget_untraceable", "candidate quote exceeds scope budget", f"{path}.quoted_price"))

    if strict_v3_scope:
        top_limit = scope.get("top_limit")
        summary = data.get("summary") if isinstance(data, Mapping) else None
        selected_count = summary.get("selected_count") if isinstance(summary, Mapping) else None
        candidate_count = summary.get("candidate_count") if isinstance(summary, Mapping) else None
        if isinstance(top_limit, int) and len(items) > top_limit:
            issues.append(
                ValidationIssue(
                    "kol_top_limit_exceeded",
                    "number of selected candidates exceeds scope top_limit",
                    "data.items",
                )
            )
        distribution = summary.get("platform_distribution") if isinstance(summary, Mapping) else None
        distribution_counts = [
            item.get("count")
            for item in (distribution if isinstance(distribution, (list, tuple)) else ())
            if isinstance(item, Mapping) and isinstance(item.get("count"), int)
        ]
        if (
            not isinstance(top_limit, int)
            or isinstance(top_limit, bool)
            or not isinstance(selected_count, int)
            or isinstance(selected_count, bool)
            or selected_count < 0
            or selected_count > top_limit
            or (
                isinstance(candidate_count, int)
                and not isinstance(candidate_count, bool)
                and selected_count > candidate_count
            )
            or (
                distribution_counts
                and sum(distribution_counts) != selected_count
            )
            or selected_count != len(items)
        ):
            issues.append(
                ValidationIssue(
                    "kol_summary_count_mismatch",
                    "summary selected_count must match items and stay within scope top_limit",
                    "data.summary.selected_count",
                )
            )

    narrative = payload.get("narrative")
    if isinstance(narrative, Mapping):
        def _walk(node: Any) -> list[str]:
            found: list[str] = []
            if isinstance(node, Mapping):
                value = node.get("kol_uid")
                if _kol_nonempty(value):
                    found.append(str(value).strip())
                for child in node.values():
                    found.extend(_walk(child))
            elif isinstance(node, (list, tuple)):
                for child in node:
                    found.extend(_walk(child))
            return found

        for kol_uid in _walk(narrative):
            if kol_uid not in item_ids:
                issues.append(ValidationIssue("kol_narrative_outsider", "narrative names a candidate outside this Version", "narrative"))
    return issues


class ArtifactPayloadValidator:
    """schema_version → 唯一 Pydantic 类型的强类型校验与标准化边界。"""

    @staticmethod
    def validate_new_draft(
        *,
        module: str,
        schema_version: str,
        artifact_type: str,
        business_fields: dict[str, Any],
        payload: Any,
        direct_model_payload: bool = False,
    ) -> dict[str, Any]:
        """新建 Draft 全量校验：固定组合 + business fields + 强类型，返回标准化形态。"""
        _check_fixed_combo(module, schema_version, artifact_type)
        for field in _REQUIRED_BUSINESS_FIELDS[module]:
            if _is_blank(business_fields.get(field)):
                raise ArtifactPayloadInvalid(
                    f"business field {field!r} is required for module {module!r}; "
                    "refusing to build a naked artifact key"
                )
        return ArtifactPayloadValidator.validate_revision_payload(
            module=module,
            schema_version=schema_version,
            artifact_type=artifact_type,
            payload=payload,
            direct_model_payload=direct_model_payload,
        )

    @staticmethod
    def validate_revision_payload(
        *,
        module: str,
        schema_version: str,
        artifact_type: str,
        payload: Any,
        enforce_kol_publication_validity: bool = False,
        direct_model_payload: bool = False,
    ) -> dict[str, Any]:
        """Revision 级校验（update/publish 复用）：固定组合 + 强类型。

        key 已在 create 时生成并校验，此处不重复 business fields 检查。
        """
        _check_fixed_combo(module, schema_version, artifact_type)
        payload_cls = TYPED_PAYLOAD_BY_SCHEMA[schema_version]
        if not isinstance(payload, dict):
            raise ArtifactPayloadInvalid(
                f"payload for {schema_version!r} must be a JSON object, "
                f"got {type(payload).__name__}"
            )
        try:
            with model_direct_lineage_context(direct_model_payload):
                instance = payload_cls.model_validate(payload)
        except ValidationError as exc:
            raise ArtifactPayloadInvalid(
                f"payload fails {schema_version!r} contract: {exc.error_count()} error(s)",
                errors=exc.errors(include_context=False),
            ) from exc
        if enforce_kol_publication_validity and schema_version == "kol_selection_v3":
            kol_issues = validate_kol_candidates(instance, require_v3_scope=True)
            if kol_issues:
                raise ArtifactPayloadInvalid(
                    "payload fails kol candidate publication validity",
                    errors=[
                        {
                            "loc": [issue.path] if issue.path else [],
                            "msg": issue.message,
                            "type": issue.code,
                            "code": issue.code,
                        }
                        for issue in kol_issues
                    ],
                )
        if isinstance(instance, CanonicalPayloadMixin):
            try:
                instance.require_canonical()
            except ValueError as exc:
                raise ArtifactPayloadInvalid(
                    f"payload fails {schema_version!r} canonical publication contract",
                    errors=[
                        {
                            "loc": ["canonical_data"],
                            "msg": str(exc),
                            "type": "value_error",
                        }
                    ],
                ) from exc
        return instance.model_dump(mode="json")

    @staticmethod
    def validate_revision_payload_collecting(
        *,
        module: str,
        schema_version: str,
        artifact_type: str,
        payload: Any,
        enforce_kol_publication_validity: bool = False,
        direct_model_payload: bool = False,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """收集式 Revision 校验：不抛异常，返回 ``(标准化 payload | None, 错误列表)``。

        直接发布服务（``agent_artifacts.publishing``）用它在不中断逐项循环的
        前提下拿到结构化失败明细，固化进 ``validation_json`` 校验快照；语义与
        :meth:`validate_revision_payload` 完全一致，只是失败改由返回值表达。
        """
        try:
            normalized = ArtifactPayloadValidator.validate_revision_payload(
                module=module,
                schema_version=schema_version,
                artifact_type=artifact_type,
                payload=payload,
                enforce_kol_publication_validity=enforce_kol_publication_validity,
                direct_model_payload=direct_model_payload,
            )
        except ArtifactPayloadInvalid as exc:
            errors = exc.errors or [{"loc": [], "msg": str(exc), "type": exc.code}]
            return None, errors
        return normalized, []


__all__ = [
    "SCHEMA_VERSION_BY_MODULE",
    "ArtifactPayloadInvalid",
    "ArtifactPayloadValidator",
    "ValidationIssue",
    "validate_kol_candidates",
    "validate_structured_claims",
]
