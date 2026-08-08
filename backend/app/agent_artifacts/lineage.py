"""字段级 Evidence Lineage 校验器（设计文档 §10.4 / Task 11）。

``validate_and_freeze_lineage`` 在提交 Reviewer 前校验：每个业务数字都能追溯到
当前用户 + 当前 Session 的不可变 Evidence（或对 Evidence 的确定性推导），并在
发布时把完整传递闭包固化为 ``FrozenLineage`` 快照。

必选 numeric 检测规则（本任务定义，以 insight_board_v1 为具体示例）：
- ``data`` 下每个叶子 JSON 数值（int/float，不含 bool）都要求 lineage；
- 例外（spec §12.1「Lineage 与消费边界」）：
  - 日期组合：``date/start/end/timezone/published_at/fetched_at/expires_at`` 等；
  - 版本号：``version/schema_version``；
  - 评分公式常量权重：``weight`` 键，以及 ``data/scoring`` 配置子树；
  - 纯文本标签、枚举、稳定身份（非数值叶子天然不要求 lineage）。
- 数组的 ``rank/count/share``、评分结果和分布统计都要求 lineage；
- ``None`` 视为无值，不要求 lineage（payload 校验已要求 partial/unavailable 披露）。

loader 通过 ``LineageLoader`` 协议注入：测试可用内存 loader，生产用
``DbLineageLoader``（MySQL）。任一约束不满足即抛 ``LineageError``（含稳定
``code``）；``evidence_refs_json`` 结构/语法错误抛 Pydantic ``ValidationError``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.canonical import walk_data_leaves
from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_artifacts.schemas import (
    LINEAGE_REFS_ADAPTER,
    ArtifactSource,
    DerivationRef,
    EvidenceSource,
    FrozenDerivationRef,
    FrozenEvidenceSource,
    FrozenLineage,
    FrozenLineageRef,
    LineageRef,
)
from app.agent_runtime.models import AgentRun, AgentToolCall, AgentUpload, EvidenceItem

# Artifact 递归展开的最大深度：超过即报 ``lineage_too_deep``，防止深链/组合爆炸
# 拖垮校验请求（菱形共享子图已由 memo 消除重复查询）。
MAX_ARTIFACT_DEPTH = 32


class LineageError(Exception):
    """结构化 lineage 校验失败；``code`` 为稳定错误码（供上层分类/展示）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidationIssue:
    """结构化发布门禁问题；不携带原始 Evidence/正文，便于安全回喂。"""

    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "msg": self.message}
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True)
class EvidenceScope:
    """结构化 claims validator 使用的最小值对象。

    ``evidence`` 的值可以是 ``EvidenceScopeEntry`` 或等价 mapping；发布服务
    传入冻结 lineage 中的 Evidence ID，纯函数测试可传完整 user/session/run
    归属信息。上传 Evidence 的 ``run_id=None`` 是 Run 创建前上传的合法例外。
    """

    user_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    allowed_artifact_version_ids: frozenset[str] = frozenset()
    field_versions: Mapping[str, str] = field(default_factory=dict)
    field_evidence_ids: Mapping[str, frozenset[str]] = field(default_factory=dict)


def _scope_attr(scope: EvidenceScope | Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    if isinstance(scope, Mapping):
        return scope.get(name, default)
    return getattr(scope, name, default)


def _normalise_scope(scope: EvidenceScope | Mapping[str, Any] | Any) -> EvidenceScope:
    evidence = _scope_attr(scope, "evidence", None)
    if evidence is None:
        evidence = _scope_attr(scope, "evidence_by_id", None)
    if evidence is None:
        evidence_ids = _scope_attr(scope, "evidence_ids", None)
        if evidence_ids is None:
            evidence_ids = _scope_attr(scope, "allowed_evidence_ids", ()) or ()
        evidence = {str(evidence_id): None for evidence_id in evidence_ids}
    if isinstance(evidence, (list, tuple, set, frozenset)):
        evidence = {str(evidence_id): None for evidence_id in evidence}
    allowed_versions = _scope_attr(scope, "allowed_artifact_version_ids", ()) or ()
    field_versions = _scope_attr(scope, "field_versions", None)
    if field_versions is None:
        field_versions = _scope_attr(scope, "canonical_versions", {}) or {}
    field_evidence = _scope_attr(scope, "field_evidence_ids", None)
    if field_evidence is None:
        field_evidence = _scope_attr(scope, "lineage_by_path", {}) or {}
    return EvidenceScope(
        user_id=_scope_attr(scope, "user_id"),
        session_id=_scope_attr(scope, "session_id"),
        run_id=_scope_attr(scope, "run_id"),
        evidence=evidence if isinstance(evidence, Mapping) else {},
        allowed_artifact_version_ids=frozenset(str(item) for item in allowed_versions),
        field_versions=(field_versions if isinstance(field_versions, Mapping) else {}),
        field_evidence_ids={
            str(path): frozenset(str(item) for item in ids)
            for path, ids in (field_evidence.items() if isinstance(field_evidence, Mapping) else ())
        },
    )


def _scope_entry(entry: Any, name: str, default: Any = None) -> Any:
    if entry is None:
        return default
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _json_pointer_from_dotted(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    if path.startswith("/"):
        return path
    parts = path.split(".")
    if parts[0] == "data":
        parts = parts[1:]
    if not parts or any(part == "" for part in parts):
        return None
    return "/data/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _is_leaf(value: Any) -> bool:
    return not isinstance(value, (dict, list, tuple))


def _path_section(pointer: str) -> str | None:
    parts = pointer.removeprefix("/data/").split("/")
    return parts[0] if parts and parts[0] else None


def _normalise_limitation_path(path: Any) -> str | None:
    return _json_pointer_from_dotted(path)


def _limitation_covers(limitations: list[Any], pointer: str) -> bool:
    section = _path_section(pointer)
    for limitation in limitations:
        affected = _scope_entry(limitation, "affected_paths", ()) or ()
        if not affected:
            return True
        for raw_path in affected:
            candidate = _normalise_limitation_path(raw_path)
            if candidate == pointer or (section and candidate == f"/data/{section}"):
                return True
    return False


def _narrative_paths(narrative: Any) -> list[tuple[str | None, Any]]:
    found: list[tuple[str | None, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if "supporting_paths" in node:
                paths = node.get("supporting_paths")
                if not isinstance(paths, (list, tuple)) or not paths:
                    found.append(("supporting_path_missing", paths))
                else:
                    for path in paths:
                        found.append((None, path))
            elif (
                ("title" in node and "detail" in node)
                or ("phase" in node and "detail" in node)
                or ("title" in node and "action" in node)
            ):
                found.append(("supporting_path_missing", None))
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(narrative)
    return found


def validate_structured_claims(
    payload: Mapping[str, Any],
    artifact_version_id: str,
    evidence_scope: EvidenceScope | Mapping[str, Any] | Any,
) -> list[ValidationIssue]:
    """验证 canonical/lineage/narrative 的结构化发布门禁。

    该函数只读取结构化字段，不解析 Markdown。非 canonical 历史模块保持兼容，
    直接返回空问题列表；Brand/Campaign 新产物由强类型 validator 先保证完整
    canonical 合同，本函数再做候选 Version、叙事路径与 Evidence scope 的闭环。
    """
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    canonical = payload.get("canonical_data")
    field_lineage = payload.get("field_lineage")
    if not canonical and not field_lineage:
        return []

    issues: list[ValidationIssue] = []
    if not isinstance(canonical, (list, tuple)) or not isinstance(field_lineage, Mapping):
        return [ValidationIssue("canonical_contract_missing", "canonical_data and field_lineage are required")]

    data = payload.get("data")
    scope = _normalise_scope(evidence_scope)
    if (
        scope.allowed_artifact_version_ids
        and artifact_version_id not in scope.allowed_artifact_version_ids
    ):
        issues.append(
            ValidationIssue(
                "canonical_version_mismatch",
                "candidate Version is outside the publication scope",
            )
        )
    canonical_by_path: dict[str, Mapping[str, Any]] = {}
    for field_entry in canonical:
        if not isinstance(field_entry, Mapping):
            issues.append(ValidationIssue("canonical_field_invalid", "canonical field must be an object"))
            continue
        path = field_entry.get("path")
        if not isinstance(path, str) or path in canonical_by_path:
            issues.append(ValidationIssue("canonical_path_invalid", "canonical path must be unique"))
            continue
        if not path.startswith("/data/"):
            issues.append(ValidationIssue("canonical_path_invalid", "canonical path must start with /data/", path))
            continue
        canonical_by_path[path] = field_entry
        version = field_entry.get("artifact_version_id") or scope.field_versions.get(path)
        if version is not None and version != artifact_version_id:
            issues.append(
                ValidationIssue("canonical_version_mismatch", "canonical field is not from candidate Version", path)
            )

    canonical_paths = set(canonical_by_path)
    if set(field_lineage) != canonical_paths:
        issues.append(ValidationIssue("field_lineage_mismatch", "field_lineage keys must match canonical paths"))
    for path in canonical_paths & set(field_lineage):
        targets = field_lineage.get(path)
        if targets != [path] and targets != (path,):
            issues.append(ValidationIssue("field_lineage_mismatch", "field_lineage must point to the same canonical path", path))

    data_leaves = dict(walk_data_leaves(data)) if isinstance(data, (dict, list, tuple)) else {}
    missing_canonical = set(data_leaves) - set(canonical_by_path)
    extra_canonical = set(canonical_by_path) - set(data_leaves)
    if missing_canonical or extra_canonical:
        issues.append(
            ValidationIssue(
                "canonical_data_mismatch",
                "canonical paths must exactly cover payload data leaves",
            )
        )
    for path, field_entry in canonical_by_path.items():
        try:
            value = resolve_pointer(payload, path)
        except (PointerError, TypeError):
            issues.append(ValidationIssue("canonical_path_not_found", "canonical path does not resolve in data", path))
            continue
        if not _is_leaf(value):
            issues.append(ValidationIssue("canonical_path_not_leaf", "canonical path must resolve to a data leaf", path))
        if path in data_leaves and field_entry.get("value") != data_leaves[path]:
            issues.append(ValidationIssue("canonical_value_mismatch", "canonical value differs from payload data", path))

        status = field_entry.get("availability")
        if status not in {"complete", "partial", "unavailable"}:
            issues.append(ValidationIssue("canonical_availability_invalid", "invalid canonical availability", path))
        if status == "unavailable" and field_entry.get("value") is not None:
            issues.append(ValidationIssue("canonical_unavailable_value", "unavailable canonical field must be null", path))
        if status == "partial" and not _limitation_covers(list(payload.get("limitations") or ()), path):
            issues.append(ValidationIssue("partial_without_limitation", "partial canonical field lacks a covering limitation", path))

        section = _path_section(path)
        availability = payload.get("availability") or {}
        section_entry = availability.get(section) if isinstance(availability, Mapping) and section else None
        section_status = _scope_entry(section_entry, "status")
        if status == "partial" and section_status == "complete":
            issues.append(ValidationIssue("availability_mismatch", "canonical availability conflicts with complete section", path))

        value = field_entry.get("value")
        evidence_ids = field_entry.get("evidence_ids") or []
        if isinstance(value, (int, float)) and not isinstance(value, bool) and status != "unavailable":
            if not evidence_ids:
                issues.append(ValidationIssue("numeric_lineage_missing", "numeric canonical field has no Evidence lineage", path))
            else:
                expected_evidence = scope.field_evidence_ids.get(path)
                if expected_evidence is not None and set(evidence_ids) != expected_evidence:
                    issues.append(
                        ValidationIssue(
                            "evidence_lineage_mismatch",
                            "canonical Evidence IDs do not match frozen field lineage",
                            path,
                        )
                    )
                for evidence_id in evidence_ids:
                    entry = scope.evidence.get(evidence_id)
                    if evidence_id not in scope.evidence:
                        issues.append(ValidationIssue("evidence_not_in_scope", "Evidence is not in the publication scope", path))
                        continue
                    if entry is None:
                        continue
                    entry_user = _scope_entry(entry, "user_id")
                    entry_session = _scope_entry(entry, "session_id")
                    entry_run = _scope_entry(entry, "run_id")
                    source_type = _scope_entry(entry, "source_type")
                    if scope.user_id is not None and entry_user is not None and entry_user != scope.user_id:
                        issues.append(ValidationIssue("evidence_user_mismatch", "Evidence user is outside scope", path))
                    if scope.session_id is not None and entry_session is not None and entry_session != scope.session_id:
                        issues.append(ValidationIssue("evidence_session_mismatch", "Evidence session is outside scope", path))
                    is_upload = source_type in {"upload", "user_upload"} or _scope_entry(entry, "upload_id") is not None
                    if scope.run_id is not None and entry_run != scope.run_id and not is_upload:
                        issues.append(ValidationIssue("evidence_run_mismatch", "MCP Evidence is outside the allowed Run", path))
                    if scope.run_id is not None and entry_run not in {None, scope.run_id} and is_upload:
                        issues.append(ValidationIssue("evidence_run_mismatch", "uploaded Evidence is outside the allowed Run", path))

    for marker, raw_path in _narrative_paths(payload.get("narrative")):
        if marker is not None:
            issues.append(ValidationIssue(marker, "narrative claim must include supporting_paths"))
            continue
        pointer = _json_pointer_from_dotted(raw_path)
        if pointer is None:
            issues.append(ValidationIssue("supporting_path_invalid", "supporting path must be a dotted path or JSON Pointer"))
            continue
        try:
            value = resolve_pointer(payload, pointer)
        except (PointerError, TypeError):
            issues.append(ValidationIssue("supporting_path_not_found", "supporting path does not resolve in data", pointer))
            continue
        if not _is_leaf(value):
            issues.append(ValidationIssue("supporting_path_not_leaf", "supporting path must resolve to a data leaf", pointer))
            continue
        field_entry = canonical_by_path.get(pointer)
        if field_entry is None:
            issues.append(ValidationIssue("canonical_field_missing", "supporting path has no canonical field", pointer))
            continue
        if field_entry.get("value") != value:
            issues.append(ValidationIssue("canonical_value_mismatch", "supporting path value differs from canonical value", pointer))
        if field_entry.get("availability") == "unavailable":
            issues.append(ValidationIssue("supporting_path_unavailable", "narrative cannot cite unavailable canonical field", pointer))
        targets = field_lineage.get(pointer)
        if targets != [pointer] and targets != (pointer,):
            issues.append(ValidationIssue("field_lineage_mismatch", "supporting path is not self-lineaged", pointer))
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not field_entry.get("evidence_ids"):
            issues.append(ValidationIssue("numeric_lineage_missing", "narrative numeric claim has no Evidence lineage", pointer))

    return issues


def evidence_scope_from_snapshot(
    snapshot: Mapping[str, Any] | None, *, owner: LineageOwner
) -> EvidenceScope:
    """从已通过 ``ArtifactLineageFreezer`` 的闭包生成 claims scope。

    Freezer 已完成 user/session/run 归属和工具状态校验；这里仅把闭包中的
    Evidence ID 作为结构化 validator 的允许集合，避免再次暴露 DB/路径对象。
    """
    evidence_ids: set[str] = set()
    field_evidence_ids: dict[str, set[str]] = {}
    for ref in (snapshot or {}).get("refs", ()):
        path = ref.get("artifact_path")
        path_bucket = field_evidence_ids.setdefault(path, set()) if isinstance(path, str) else None
        for source in ref.get("sources", ()):
            evidence_id = source.get("evidence_id")
            if isinstance(evidence_id, str):
                evidence_ids.add(evidence_id)
                if path_bucket is not None:
                    path_bucket.add(evidence_id)
    return EvidenceScope(
        user_id=owner.user_id,
        session_id=owner.session_id,
        run_id=owner.run_id,
        evidence={evidence_id: None for evidence_id in evidence_ids},
        field_evidence_ids={
            path: frozenset(ids) for path, ids in field_evidence_ids.items()
        },
    )


@dataclass(frozen=True)
class LineageOwner:
    """被校验 Artifact 的归属：user + session（evidence/artifact/tool call 都按
    session 归属校验，session 唯一且绑定 user，session 匹配即 user+session 匹配）。"""

    user_id: str
    session_id: str
    run_id: str | None = None


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    session_id: str
    raw_payload: Any
    payload_hash: str
    run_id: str | None = None
    # MCP Evidence 的 tool_call_id（upload Evidence 为 None）。
    tool_call_id: str | None = None
    # MCP 调用来源快照（Gate B：可独立审计精确调用）。
    tool_name: str | None = None
    service: str | None = None
    arguments_hash: str | None = None
    # upload Evidence 的来源文件信息（MCP Evidence 为 None；Gate B）。
    upload: dict[str, Any] | None = None


@dataclass(frozen=True)
class ArtifactVersionRecord:
    id: str
    session_id: str
    payload: Any
    evidence_refs: list[dict[str, Any]]


@dataclass(frozen=True)
class ToolCallRecord:
    id: str
    session_id: str
    service: str
    status: str
    run_id: str | None = None


class LineageLoader(Protocol):
    """loader 协议：校验器只依赖它取记录，测试可注入内存实现。"""

    async def load_evidence(self, evidence_id: str) -> EvidenceRecord | None: ...

    async def load_artifact_version(self, version_id: str) -> ArtifactVersionRecord | None: ...

    async def load_tool_call(self, tool_call_id: str) -> ToolCallRecord | None: ...


class PointerError(ValueError):
    """RFC 6901 指针解析失败（指向不存在的位置）。"""


def resolve_pointer(document: Any, pointer: str) -> Any:
    """按 RFC 6901 解析 JSON Pointer；找不到时抛 ``PointerError``。"""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise PointerError(f"invalid JSON Pointer: {pointer!r}")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise PointerError(
                    f"JSON Pointer {pointer!r} does not resolve: missing key {token!r}"
                )
            current = current[token]
        elif isinstance(current, (list, tuple)):
            if token == "-":
                raise PointerError(
                    f"JSON Pointer {pointer!r} does not resolve: '-' is not an existing element"
                )
            # RFC 6901 §4：数组下标必须是无前导零的无符号十进制整数。
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise PointerError(
                    f"JSON Pointer {pointer!r} does not resolve: bad array index {token!r}"
                )
            index = int(token)
            if index >= len(current):
                raise PointerError(
                    f"JSON Pointer {pointer!r} does not resolve: index {index} out of range"
                )
            current = current[index]
        else:
            raise PointerError(
                f"JSON Pointer {pointer!r} does not resolve: cannot descend into scalar"
            )
    return current


def _require_pointer(document: Any, pointer: str, code: str) -> None:
    try:
        resolve_pointer(document, pointer)
    except PointerError as exc:
        raise LineageError(code, str(exc)) from exc


# 例外键：数值叶子即便出现在 ``data`` 下也不要求 lineage（日期/运行时元数据/
# 版本号等；spec §12.1「Lineage 与消费边界」）。注意 ``weight`` 不在此列——
# 评分公式常量权重只在 ``score_snapshot`` 维度下排除（见 _SCORING_WEIGHT_ANCESTOR），
# 普通业务字段名为 ``weight``（如 insight 的物流权重）仍要求 lineage。
_NON_LINEAGE_KEYS = frozenset(
    {
        "date",
        "start",
        "end",
        "timezone",
        "published_at",
        "fetched_at",
        "expires_at",
        "created_at",
        "updated_at",
        "collected_at",
        "version",
        "schema_version",
    }
)

# ``data`` 下整体视为配置/公式元数据的子树，不要求 lineage。
_NON_LINEAGE_DATA_SUBTREES = frozenset({"scoring"})

# kol_score_v2 每维度权重（``data.items[].score_snapshot.dimensions.*.weight``）
# 是评分公式常量，仅当祖先路径含 ``score_snapshot`` 时排除。
_SCORING_WEIGHT_ANCESTOR = "score_snapshot"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def required_numeric_pointers(payload: dict[str, Any]) -> frozenset[str]:
    """返回 ``data`` 下要求 lineage 的全部数值叶子指针（payload 根相对）。

    具体规则见模块 docstring；insight_board_v1 的 metric/series/table 数字都命中，
    布局序号（数组下标不算字段）、日期组合与版本号被排除。
    """
    data_node = payload.get("data")
    if not isinstance(data_node, (dict, list, tuple)):
        return frozenset()
    found: set[str] = set()

    def walk(node: Any, parts: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _NON_LINEAGE_KEYS:
                    continue
                if key == "weight" and _SCORING_WEIGHT_ANCESTOR in parts:
                    continue
                walk(value, [*parts, key])
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, [*parts, str(index)])
        elif _is_number(node) and not (
            len(parts) >= 2 and parts[1] in _NON_LINEAGE_DATA_SUBTREES
        ):
            found.add("/" + "/".join(_escape_token(part) for part in parts))

    walk(data_node, ["data"])
    return frozenset(found)


@dataclass(frozen=True)
class _ResolvedEvidence:
    """一条已解析的证据叶子（闭包展开结果）。"""

    evidence_id: str
    source_path: str
    payload_hash: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    service: str | None = None
    arguments_hash: str | None = None
    upload: dict[str, Any] | None = None


def _parse_refs(raw: Any) -> list[LineageRef]:
    if not raw:
        return []
    try:
        return LINEAGE_REFS_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise LineageError("malformed_refs", f"invalid evidence_refs structure: {exc}") from exc


async def _resolve_source(
    source: EvidenceSource | ArtifactSource,
    path_stack: frozenset[str],
    memo: dict[tuple[str, str], tuple[list[_ResolvedEvidence], Any]],
    depth: int,
    loader: LineageLoader,
    owner: LineageOwner,
) -> tuple[list[_ResolvedEvidence], Any]:
    """把一个来源解析为证据叶子列表，同时返回该来源的直接 payload（供 derivation
    ``input_paths`` 解析）。Artifact 来源递归展开到 Evidence。

    ``memo`` 以 ``(artifact_version_id, source_path)`` 缓存已完成展开，菱形共享子图
    中每个版本只查询一次；``depth`` 限制递归深度，超 ``MAX_ARTIFACT_DEPTH`` 即拒绝。
    """
    if isinstance(source, EvidenceSource):
        record = await loader.load_evidence(source.evidence_id)
        if record is None:
            raise LineageError(
                "evidence_not_found", f"evidence {source.evidence_id!r} not found"
            )
        if record.session_id != owner.session_id:
            raise LineageError(
                "evidence_not_owned",
                f"evidence {source.evidence_id!r} does not belong to current session",
            )
        if record.upload is not None:
            upload_user_id = record.upload.get("_user_id")
            upload_session_id = record.upload.get("_session_id")
            upload_run_id = record.upload.get("_run_id")
            if upload_user_id is not None and upload_user_id != owner.user_id:
                raise LineageError(
                    "evidence_upload_not_owned",
                    f"evidence {source.evidence_id!r} upload is not owned by current user",
                )
            if upload_session_id is not None and upload_session_id != owner.session_id:
                raise LineageError(
                    "evidence_upload_not_owned",
                    f"evidence {source.evidence_id!r} upload is not in current session",
                )
            if owner.run_id is not None and upload_run_id not in {None, owner.run_id}:
                raise LineageError(
                    "evidence_run_not_owned",
                    f"evidence {source.evidence_id!r} upload is not in current run",
                )
        # MCP Evidence 必须来自当前 Run；Run 创建前上传的 Evidence 保留
        # session/user 范围内的合法例外（upload 元数据非空）。
        if owner.run_id is not None and (
            (record.upload is None and record.run_id != owner.run_id)
            or (record.upload is not None and record.run_id not in {None, owner.run_id})
        ):
            raise LineageError(
                "evidence_run_not_owned",
                f"evidence {source.evidence_id!r} does not belong to current run",
            )
        _require_pointer(record.raw_payload, source.source_path, "evidence_source_path_not_found")
        return [
            _ResolvedEvidence(
                evidence_id=record.id,
                source_path=source.source_path,
                payload_hash=record.payload_hash,
                tool_call_id=record.tool_call_id,
                tool_name=record.tool_name,
                service=record.service,
                arguments_hash=record.arguments_hash,
                upload=record.upload,
            )
        ], record.raw_payload

    # artifact source：归属 + 存在性 + source_path 存在性 + 递归展开。
    if source.artifact_version_id in path_stack:
        raise LineageError(
            "lineage_cycle",
            f"artifact lineage cycle detected at version {source.artifact_version_id!r}",
        )
    if depth >= MAX_ARTIFACT_DEPTH:
        raise LineageError(
            "lineage_too_deep",
            f"artifact lineage exceeds max depth {MAX_ARTIFACT_DEPTH} "
            f"at version {source.artifact_version_id!r}",
        )
    memo_key = (source.artifact_version_id, source.source_path)
    if memo_key in memo:
        cached_leaves, cached_payload = memo[memo_key]
        return list(cached_leaves), cached_payload
    record = await loader.load_artifact_version(source.artifact_version_id)
    if record is None:
        raise LineageError(
            "artifact_not_found", f"artifact version {source.artifact_version_id!r} not found"
        )
    if record.session_id != owner.session_id:
        raise LineageError(
            "artifact_not_owned",
            f"artifact version {source.artifact_version_id!r} does not belong to current session",
        )
    _require_pointer(record.payload, source.source_path, "artifact_source_path_not_found")

    # 在被引用版本自己的 lineage 中找 source_path 对应字段；找不到 = 无证据基座。
    sub_refs = _parse_refs(record.evidence_refs)
    match = next(
        (ref for ref in sub_refs if ref.artifact_path == source.source_path),
        None,
    )
    if match is None:
        raise LineageError(
            "artifact_no_lineage_base",
            f"artifact version {record.id!r} has no lineage entry for {source.source_path!r}",
        )
    leaves: list[_ResolvedEvidence] = []
    next_stack = path_stack | {record.id}
    for sub_source in match.sources:
        sub_leaves, _ = await _resolve_source(
            sub_source, next_stack, memo, depth + 1, loader, owner
        )
        leaves.extend(sub_leaves)
    if not leaves:
        raise LineageError(
            "no_evidence_base",
            f"artifact version {record.id!r} field {source.source_path!r} has no evidence base",
        )
    memo[memo_key] = (list(leaves), record.payload)
    return leaves, record.payload


def _pointer_resolves(document: Any, pointer: str) -> bool:
    try:
        resolve_pointer(document, pointer)
    except PointerError:
        return False
    return True


async def _validate_derivation(
    derivation: DerivationRef,
    source_payloads: list[Any],
    loader: LineageLoader,
    owner: LineageOwner,
) -> None:
    tool = await loader.load_tool_call(derivation.tool_call_id)
    if tool is None:
        raise LineageError(
            "derivation_tool_call_invalid",
            f"derivation tool_call {derivation.tool_call_id!r} not found",
        )
    if tool.session_id != owner.session_id:
        raise LineageError(
            "derivation_tool_call_invalid",
            f"derivation tool_call {derivation.tool_call_id!r} is not in current session",
        )
    if owner.run_id is not None and tool.run_id != owner.run_id:
        raise LineageError(
            "derivation_run_not_owned",
            f"derivation tool_call {derivation.tool_call_id!r} is not in current run",
        )
    if tool.status != "settled":
        raise LineageError(
            "derivation_tool_call_invalid",
            f"derivation tool_call {derivation.tool_call_id!r} status "
            f"{tool.status!r} is not settled",
        )
    if tool.service != "internal":
        raise LineageError(
            "derivation_tool_call_invalid",
            f"derivation tool_call {derivation.tool_call_id!r} service "
            f"{tool.service!r} is not internal",
        )
    for input_path in derivation.input_paths:
        if not any(_pointer_resolves(payload, input_path) for payload in source_payloads):
            raise LineageError(
                "derivation_input_path_not_found",
                f"derivation input_path {input_path!r} does not resolve in any source payload",
            )


async def validate_and_freeze_lineage(
    *,
    payload: dict[str, Any],
    refs: list[LineageRef] | list[dict[str, Any]],
    owner: LineageOwner,
    loader: LineageLoader,
) -> FrozenLineage:
    """校验 ``refs`` 并把完整传递闭包冻结为 ``FrozenLineage`` 快照。

    ``refs`` 接受 ``LineageRef`` 或原始 dict 数组（即模型产物 / DB 的
    ``evidence_refs_json``）。任一约束不满足即抛 ``LineageError``。
    """
    parsed_refs = _parse_refs(refs)
    if len({ref.artifact_path for ref in parsed_refs}) != len(parsed_refs):
        raise LineageError("duplicate_artifact_path", "artifact_path must be unique across refs")

    # 1. artifact_path 必须能在 payload 中解析（§10.4 约束 1）。
    for ref in parsed_refs:
        _require_pointer(payload, ref.artifact_path, "pointer_not_found")

    # 2. 逐字段解析来源 + derivation，构建冻结闭包。
    frozen_refs: list[FrozenLineageRef] = []
    memo: dict[tuple[str, str], tuple[list[_ResolvedEvidence], Any]] = {}
    for ref in parsed_refs:
        leaves: list[_ResolvedEvidence] = []
        source_payloads: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for source in ref.sources:
            resolved_leaves, source_payload = await _resolve_source(
                source, frozenset(), memo, 0, loader, owner
            )
            for leaf in resolved_leaves:
                key = (leaf.evidence_id, leaf.source_path)
                if key not in seen:
                    seen.add(key)
                    leaves.append(leaf)
            source_payloads.append(source_payload)
        if not leaves:
            raise LineageError(
                "no_evidence_base", f"field {ref.artifact_path!r} has no evidence base"
            )
        if ref.derivation is not None:
            await _validate_derivation(ref.derivation, source_payloads, loader, owner)
        frozen_refs.append(
            FrozenLineageRef(
                artifact_path=ref.artifact_path,
                sources=tuple(
                    FrozenEvidenceSource(
                        evidence_id=leaf.evidence_id,
                        source_path=leaf.source_path,
                        payload_hash=leaf.payload_hash,
                        tool_call_id=leaf.tool_call_id,
                        tool_name=leaf.tool_name,
                        service=leaf.service,
                        arguments_hash=leaf.arguments_hash,
                        upload_id=(leaf.upload or {}).get("upload_id"),
                        upload_sha256=(leaf.upload or {}).get("sha256"),
                        upload_filename=(leaf.upload or {}).get("original_filename"),
                        uploaded_at=(leaf.upload or {}).get("uploaded_at"),
                    )
                    for leaf in leaves
                ),
                # 复制为不可变 FrozenDerivationRef：发布后修改可变输入 ref 不得
                # 污染已冻结快照（自包含、稳定）。
                derivation=(
                    FrozenDerivationRef(**ref.derivation.model_dump())
                    if ref.derivation is not None
                    else None
                ),
            )
        )

    # 3. 必选 numeric 全覆盖（§10.4 约束 4；缺失引用拒绝进入 review）。
    required = required_numeric_pointers(payload)
    covered = {ref.artifact_path for ref in parsed_refs}
    missing = required - covered
    if missing:
        raise LineageError(
            "missing_lineage",
            "required numeric fields lack a lineage entry: " + ", ".join(sorted(missing)),
        )

    return FrozenLineage(refs=tuple(frozen_refs))


class DbLineageLoader:
    """生产 loader：从 MySQL 读取 evidence / artifact version / tool call。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def load_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        stmt = (
            select(EvidenceItem, AgentUpload, AgentToolCall)
            .outerjoin(AgentUpload, EvidenceItem.upload_id == AgentUpload.id)
            .outerjoin(AgentToolCall, EvidenceItem.tool_call_id == AgentToolCall.id)
            .where(EvidenceItem.id == evidence_id)
        )
        result = await self._db.execute(stmt)
        row = result.first()
        if row is None:
            return None
        evidence, upload, tool_call = row
        upload_info = None
        if upload is not None:
            upload_info = {
                "upload_id": upload.id,
                "sha256": upload.sha256,
                "original_filename": upload.original_filename,
                # 仅供发布前归属校验；冻结的公开 lineage 只保留上面的文件审计字段。
                "_user_id": upload.user_id,
                "_session_id": upload.session_id,
                "_run_id": upload.run_id,
                "uploaded_at": (
                    upload.completed_at.isoformat() if upload.completed_at else None
                ),
            }
        return EvidenceRecord(
            id=evidence.id,
            session_id=evidence.session_id,
            raw_payload=evidence.raw_payload_json,
            payload_hash=evidence.payload_hash,
            run_id=evidence.run_id,
            tool_call_id=evidence.tool_call_id,
            tool_name=tool_call.internal_tool_name if tool_call else None,
            service=tool_call.service if tool_call else None,
            arguments_hash=tool_call.arguments_hash if tool_call else None,
            upload=upload_info,
        )

    async def load_artifact_version(self, version_id: str) -> ArtifactVersionRecord | None:
        stmt = (
            select(AgentArtifactVersion, AgentArtifact.session_id)
            .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
            .where(AgentArtifactVersion.id == version_id)
        )
        result = await self._db.execute(stmt)
        row = result.first()
        if row is None:
            return None
        version, session_id = row
        return ArtifactVersionRecord(
            id=version.id,
            session_id=session_id,
            payload=version.payload_json,
            evidence_refs=version.evidence_refs_json or [],
        )

    async def load_tool_call(self, tool_call_id: str) -> ToolCallRecord | None:
        stmt = (
            select(AgentToolCall, AgentRun.session_id)
            .join(AgentRun, AgentRun.id == AgentToolCall.run_id)
            .where(AgentToolCall.id == tool_call_id)
        )
        result = await self._db.execute(stmt)
        row = result.first()
        if row is None:
            return None
        tool, session_id = row
        return ToolCallRecord(
            id=tool.id,
            session_id=session_id,
            service=tool.service,
            status=tool.status,
            run_id=tool.run_id,
        )


class ArtifactLineageFreezer:
    """发布事务的 lineage 冻结边界（v3 加固 §5.6 / A5）。

    发布时重算 ``validate_and_freeze_lineage`` 的 Evidence 传递闭包，产出可直接
    落库 ``agent_artifact_versions.lineage_snapshot_json`` 的 JSON dict。
    ``evidence_refs_json`` 仍记录模型直接引用，两者职责分离（引用 vs 审计快照）。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._loader = DbLineageLoader(db)

    async def freeze(
        self,
        *,
        payload: dict[str, Any],
        refs: list[dict[str, Any]] | None,
        owner: LineageOwner,
    ) -> dict[str, Any]:
        frozen = await validate_and_freeze_lineage(
            payload=payload,
            refs=refs or [],
            owner=owner,
            loader=self._loader,
        )
        return frozen.model_dump(mode="json")


__all__ = [
    "MAX_ARTIFACT_DEPTH",
    "ArtifactLineageFreezer",
    "ArtifactVersionRecord",
    "DbLineageLoader",
    "EvidenceRecord",
    "EvidenceScope",
    "LineageError",
    "LineageLoader",
    "LineageOwner",
    "ToolCallRecord",
    "ValidationIssue",
    "evidence_scope_from_snapshot",
    "required_numeric_pointers",
    "resolve_pointer",
    "validate_and_freeze_lineage",
    "validate_structured_claims",
]
