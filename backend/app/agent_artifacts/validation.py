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

from typing import Any

from pydantic import ValidationError

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
}

# key 模块 → 生成 artifact_key 所需 business fields（拒绝裸 key，§2.4）。
_REQUIRED_BUSINESS_FIELDS: dict[str, tuple[str, ...]] = {
    "brand": ("brand",),
    "campaign": ("brand", "campaign"),
    "kol-selection": ("scope",),
    "kol-analysis": ("selection_artifact_id",),
    "kol-detail": ("platform", "kol_uid"),
    "insight": ("parent_artifact_version_id", "question"),
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
    if schema_version != expected:
        raise ArtifactPayloadInvalid(
            f"module {module!r} requires schema_version {expected!r}, got {schema_version!r}"
        )
    if artifact_type != schema_version:
        raise ArtifactPayloadInvalid(
            f"artifact_type {artifact_type!r} must equal schema_version {schema_version!r}"
        )


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
        )

    @staticmethod
    def validate_revision_payload(
        *,
        module: str,
        schema_version: str,
        artifact_type: str,
        payload: Any,
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
            instance = payload_cls.model_validate(payload)
        except ValidationError as exc:
            raise ArtifactPayloadInvalid(
                f"payload fails {schema_version!r} contract: {exc.error_count()} error(s)",
                errors=exc.errors(include_context=False),
            ) from exc
        return instance.model_dump(mode="json")

    @staticmethod
    def validate_revision_payload_collecting(
        *,
        module: str,
        schema_version: str,
        artifact_type: str,
        payload: Any,
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
            )
        except ArtifactPayloadInvalid as exc:
            errors = exc.errors or [{"loc": [], "msg": str(exc), "type": exc.code}]
            return None, errors
        return normalized, []


__all__ = [
    "ArtifactPayloadInvalid",
    "ArtifactPayloadValidator",
    "SCHEMA_VERSION_BY_MODULE",
]
