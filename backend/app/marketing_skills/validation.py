from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Collection

from app.core.credential_scanner import contains_credential
from app.marketing_skills.constants import MAX_SKILL_CONTENT_BYTES

_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,95}$")
_ARTIFACT_CONTRACT_RE = re.compile(r"^[a-z][a-z0-9_]{1,95}$")
_SECRET_RE = re.compile(
    r"(?:api[_ -]?key|client[_ -]?secret|password|secret|access[_ -]?token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_DSN_RE = re.compile(r"\b(?:mysql|postgres(?:ql)?|redis|mongodb)://\S+", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:private/)?(?:tmp|var/tmp|home|Users|root)(?:/|$)",
    re.IGNORECASE,
)
_PRIVILEGE_RE = re.compile(
    r"(?:\b(?:bypass|ignore|disable)\s+(?:permission|billing|quota|unknown|review|audit)\b|"
    r"绕过[^\n]{0,24}(?:权限|计费|配额|审计)|忽略[^\n]{0,24}(?:权限|计费|配额|审计))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SkillValidationError:
    code: str
    message: str
    line: int | None = None


@dataclass(frozen=True)
class SkillValidationResult:
    normalized_content: str
    name: str | None
    description: str | None
    required_tools: tuple[str, ...]
    artifact_contract: str | None
    model_input_contract_version: str
    content_digest: str
    errors: tuple[SkillValidationError, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


def _normalize_content(content: str) -> str:
    return unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))


def canonical_skill_digest(content: str) -> str:
    """Return the digest of normalized UTF-8/LF Skill content."""

    normalized = _normalize_content(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_list(value: str) -> list[str] | None:
    value = value.strip()
    if value == "[]":
        return []
    if not (value.startswith("[") and value.endswith("]")):
        return None
    items = []
    for item in value[1:-1].split(","):
        item = _unquote(item)
        if item:
            items.append(item)
    return items


def _parse_frontmatter(
    normalized_content: str,
) -> tuple[dict[str, str | list[str]], list[SkillValidationError]]:
    errors: list[SkillValidationError] = []
    lines = normalized_content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, [SkillValidationError("frontmatter_required", "Skill 必须以 frontmatter 开始")]

    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, [SkillValidationError("frontmatter_unclosed", "Skill frontmatter 缺少结束标记")]

    values: dict[str, str | list[str]] = {}
    list_key: str | None = None
    allowed_keys = {
        "name",
        "description",
        "required_tools",
        "artifact_contract",
        "model_input_contract_version",
    }
    for offset, raw_line in enumerate(lines[1:closing], start=2):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") and list_key == "required_tools":
            value = _unquote(line[1:].strip())
            if not value:
                errors.append(
                    SkillValidationError("required_tool_empty", "required_tools 不能包含空工具名", offset)
                )
                continue
            current = values.setdefault("required_tools", [])
            if not isinstance(current, list):
                current = []
                values["required_tools"] = current
            current.append(value)
            continue
        if ":" not in line:
            errors.append(SkillValidationError("frontmatter_invalid_line", "frontmatter 行格式无效", offset))
            list_key = None
            continue
        key, raw_value = (part.strip() for part in line.split(":", 1))
        if key not in allowed_keys:
            errors.append(
                SkillValidationError("frontmatter_unknown_key", f"不允许的 frontmatter 字段: {key}", offset)
            )
            list_key = None
            continue
        if key in values:
            errors.append(SkillValidationError("frontmatter_duplicate_key", f"字段重复: {key}", offset))
        raw_value = raw_value.strip()
        if key == "required_tools":
            parsed = _parse_list(raw_value) if raw_value else []
            if raw_value and parsed is None:
                if raw_value.startswith("["):
                    errors.append(
                        SkillValidationError("required_tools_invalid", "required_tools 列表格式无效", offset)
                    )
                    parsed = []
                else:
                    parsed = []
                    errors.append(
                        SkillValidationError("required_tools_invalid", "required_tools 必须是列表", offset)
                    )
            values[key] = parsed or []
            list_key = key if not raw_value else None
        else:
            values[key] = _unquote(raw_value)
            list_key = None
    return values, errors


def _security_errors(content: str) -> list[SkillValidationError]:
    checks = (
        (_SECRET_RE, "secret_reference_forbidden", "Skill 不得包含凭证或密钥赋值"),
        (_BEARER_RE, "bearer_token_forbidden", "Skill 不得包含 Bearer 凭证"),
        (_DSN_RE, "dsn_forbidden", "Skill 不得包含数据库或缓存 DSN"),
        (_ABSOLUTE_PATH_RE, "absolute_path_forbidden", "Skill 不得依赖绝对临时或用户路径"),
        (_PRIVILEGE_RE, "privilege_boundary_forbidden", "Skill 不得声明绕过权限、计费或审计边界"),
    )
    errors: list[SkillValidationError] = []
    for pattern, code, message in checks:
        if pattern.search(content):
            errors.append(SkillValidationError(code, message))
    if contains_credential(content):
        errors.append(
            SkillValidationError(
                "credential_reference_forbidden", "Skill 不得包含私钥、云密钥、JWT 或供应商凭证"
            )
        )
    return errors


def validate_skill_content(
    content: str,
    *,
    expected_name: str | None,
    approved_tools: Collection[str],
) -> SkillValidationResult:
    normalized = _normalize_content(content)
    errors = _security_errors(normalized)
    if len(normalized.encode("utf-8")) > MAX_SKILL_CONTENT_BYTES:
        errors.append(
            SkillValidationError(
                "content_too_large",
                f"Skill UTF-8 内容不得超过 {MAX_SKILL_CONTENT_BYTES} bytes",
            )
        )
    metadata, parse_errors = _parse_frontmatter(normalized)
    errors.extend(parse_errors)

    raw_name = metadata.get("name")
    name = raw_name if isinstance(raw_name, str) else None
    raw_description = metadata.get("description")
    description = raw_description if isinstance(raw_description, str) else None
    raw_tools = metadata.get("required_tools", [])
    required_tools = tuple(raw_tools) if isinstance(raw_tools, list) else ()
    raw_contract = metadata.get("artifact_contract")
    artifact_contract = raw_contract if isinstance(raw_contract, str) and raw_contract else None
    raw_contract_version = metadata.get("model_input_contract_version")
    if raw_contract_version is None:
        model_input_contract_version = "direct_model_input_v1"
    elif (
        isinstance(raw_contract_version, str)
        and raw_contract_version in {"direct_model_input_v1", "source_bound_input_v2"}
    ):
        model_input_contract_version = raw_contract_version
    else:
        model_input_contract_version = "direct_model_input_v1"
        errors.append(
            SkillValidationError(
                "model_input_contract_version_invalid",
                "model_input_contract_version 必须是 direct_model_input_v1 或 source_bound_input_v2",
            )
        )

    if name is None:
        errors.append(SkillValidationError("frontmatter_name_required", "必须提供 Skill name"))
    elif not _SKILL_NAME_RE.fullmatch(name):
        errors.append(SkillValidationError("skill_name_invalid", "Skill name 格式无效"))
    if expected_name is not None and name is not None and name != expected_name:
        errors.append(SkillValidationError("skill_name_mismatch", "Skill name 与目标名称不一致"))
    if description is None or not description.strip():
        errors.append(SkillValidationError("frontmatter_description_required", "必须提供 Skill description"))
    elif len(description) > 512:
        errors.append(SkillValidationError("description_too_long", "Skill description 超出长度限制"))
    if "required_tools" not in metadata:
        errors.append(SkillValidationError("frontmatter_required_tools_required", "必须提供 required_tools"))
    seen_tools: set[str] = set()
    approved = set(approved_tools)
    for tool_name in required_tools:
        if tool_name in seen_tools:
            errors.append(
                SkillValidationError("duplicate_required_tool", f"required_tools 重复: {tool_name}")
            )
        seen_tools.add(tool_name)
        if tool_name not in approved:
            errors.append(
                SkillValidationError("unknown_required_tool", f"工具未在审核目录中: {tool_name}")
            )
    if artifact_contract is not None and not _ARTIFACT_CONTRACT_RE.fullmatch(artifact_contract):
        errors.append(SkillValidationError("artifact_contract_invalid", "artifact_contract 格式无效"))
    if normalized.endswith("---") and normalized.count("---") == 2:
        errors.append(SkillValidationError("skill_body_required", "Skill 必须包含 Markdown 正文"))

    return SkillValidationResult(
        normalized_content=normalized,
        name=name,
        description=description,
        required_tools=required_tools,
        artifact_contract=artifact_contract,
        model_input_contract_version=model_input_contract_version,
        content_digest=canonical_skill_digest(normalized),
        errors=tuple(errors),
    )


__all__ = [
    "SkillValidationError",
    "SkillValidationResult",
    "canonical_skill_digest",
    "validate_skill_content",
]
