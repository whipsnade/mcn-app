"""字段级 Evidence Lineage 的结构模型（设计文档 §10.4）。

``evidence_refs_json`` 不是自由 JSON，而是 ``LineageRef`` 数组。每个条目把
Artifact payload 里的一个业务字段（``artifact_path``，RFC 6901 JSON Pointer）
映射到它的一或多个来源；来源是判别联合（``source_type``: evidence / artifact），
可选地附一个确定性推导（``derivation``）。发布时由 lineage 校验器把完整传递
闭包固化为 ``FrozenLineage`` 快照。

所有模型一律 ``extra="forbid"``：未知字段直接拒绝，避免模型产物静默吞掉结构
错误。路径字段不仅按字符串解析，还校验 RFC 6901 语法（``~`` 必须后跟 ``0``
或 ``1``，非空指针必须以 ``/`` 开头）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


def validate_json_pointer(pointer: str) -> str:
    """RFC 6901 语法校验：空串合法；非空必须以 ``/`` 开头；``~`` 只能转义
    ``0``（表示 ``~``）或 ``1``（表示 ``/``）。返回原指针，供字段校验复用。"""
    if pointer == "":
        return pointer
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or start with '/'")
    for index, char in enumerate(pointer):
        if char == "~" and (
            index + 1 >= len(pointer) or pointer[index + 1] not in ("0", "1")
        ):
            raise ValueError(
                f"invalid JSON Pointer escape '~' at offset {index} (must be ~0 or ~1)"
            )
    return pointer


class LineageSource(BaseModel):
    """判别联合的基座：``source_type`` 区分 evidence 与 artifact 来源。"""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["evidence", "artifact"]


class EvidenceSource(LineageSource):
    """来自当前用户、当前 Session 的不可变 Evidence 的字段。"""

    source_type: Literal["evidence"] = "evidence"
    evidence_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)

    @field_validator("source_path")
    @classmethod
    def _source_path_pointer(cls, value: str) -> str:
        return validate_json_pointer(value)


class ArtifactSource(LineageSource):
    """来自当前 Session 已发布 Artifact Version 的字段（递归解析到 Evidence）。"""

    source_type: Literal["artifact"] = "artifact"
    artifact_version_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)

    @field_validator("source_path")
    @classmethod
    def _source_path_pointer(cls, value: str) -> str:
        return validate_json_pointer(value)


LineageSourceType = Annotated[
    EvidenceSource | ArtifactSource,
    Field(discriminator="source_type"),
]


class DerivationRef(BaseModel):
    """确定性推导：指向已 settled 的内部计算工具调用及其输入路径。

    ``input_paths`` 是相对各来源 payload 的 JSON Pointer，必须能在来源中解析。
    """

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    input_paths: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("input_paths")
    @classmethod
    def _input_paths_pointers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            validate_json_pointer(path)
        return value


class LineageRef(BaseModel):
    """一个 Artifact 字段的 lineage 条目：来源 + 可选推导。"""

    model_config = ConfigDict(extra="forbid")

    artifact_path: str
    sources: tuple[LineageSourceType, ...] = Field(min_length=1)
    derivation: DerivationRef | None = None

    @field_validator("artifact_path")
    @classmethod
    def _artifact_path_pointer(cls, value: str) -> str:
        return validate_json_pointer(value)


class FrozenDerivationRef(BaseModel):
    """冻结快照中的确定性推导。

    独立于输入 ``DerivationRef`` 且不可变：发布后修改模型产物（可变 input ref）
    不得污染已发布快照，保证「自包含、稳定」的 lineage 承诺（设计 §10.4 约束 6）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str
    method: str
    input_paths: tuple[str, ...] = Field(default_factory=tuple)


class FrozenEvidenceSource(BaseModel):
    """闭包快照中的证据叶子。Evidence 不可变，故引用永不失效、快照不陈旧。

    MCP Evidence 保留 ``tool_call_id``；upload Evidence（用户上传文件）保存
    upload id、文件哈希、文件名与上传时间（Gate B：可追溯到源文件）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    source_path: str
    payload_hash: str
    tool_call_id: str | None = None
    upload_id: str | None = None
    upload_sha256: str | None = None
    upload_filename: str | None = None
    uploaded_at: str | None = None


class FrozenLineageRef(BaseModel):
    """发布时冻结的字段级 lineage：来源全部展开为 Evidence 叶子。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_path: str
    sources: tuple[FrozenEvidenceSource, ...] = Field(min_length=1)
    derivation: FrozenDerivationRef | None = None


class FrozenLineage(BaseModel):
    """自包含、稳定的 lineage 快照；保存进发布版本的 ``evidence_refs_json``。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refs: tuple[FrozenLineageRef, ...] = Field(default_factory=tuple)


# 解析原始 dict 形式的 evidence_refs_json（模型产物 / DB JSON 列）。
LINEAGE_REFS_ADAPTER: TypeAdapter[list[LineageRef]] = TypeAdapter(list[LineageRef])


__all__ = [
    "ArtifactSource",
    "DerivationRef",
    "EvidenceSource",
    "FrozenDerivationRef",
    "FrozenEvidenceSource",
    "FrozenLineage",
    "FrozenLineageRef",
    "LINEAGE_REFS_ADAPTER",
    "LineageRef",
    "LineageSource",
    "LineageSourceType",
    "validate_json_pointer",
]
