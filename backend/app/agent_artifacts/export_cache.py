"""Excel 导出缓存服务（Gate C Task 6 / 审核修复 A5）。

同一 ``(artifact_version_id, template_version)`` 只构建一次：

- ``get_or_build`` 用行锁 + 唯一约束串行化并发（后到者等待先到者提交后读到
  ready 行直接复用）；
- **building 租约**：超过 ``_BUILD_LEASE_SECONDS`` 未完成的 building 行视为
  stale，后续请求在行锁下接管重建（构建请求被取消/线程异常/写文件失败/
  _mark_ready 失败都不会永久卡住）；
- ``asyncio.CancelledError`` 安全收尾（标记 failed 可接管）后重新抛出；
- ready 行对应文件丢失或 hash 不匹配 → 标记失效并重建；
- 每次循环前 ``expire_all()``，等待方重新从数据库读最新状态；
- 文件写入成功但数据库更新失败 → 清理孤儿文件并标记 failed（可恢复）；
- 失败重试只重做 Excel 导出，绝不调用模型/MCP；
- 只暴露 filename/sha256/size，不暴露 storage_key。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact
from app.agent_artifacts.exporters.analysis_report import ANALYSIS_REPORT_EXPORTER_VERSION
from app.agent_artifacts.exporters.workbook import workbook_layout_digest
from app.agent_artifacts.models import ArtifactExport
from app.agent_artifacts.payloads.analysis_report import AnalysisReportV1
from app.core.config import get_settings

# 导出文件名清洗：只保留安全字符，防止路径穿越/注入。
_FILENAME_UNSAFE = re.compile(r"[^0-9A-Za-z._\-]")
# building 租约：超过该时长视为 stale，可被接管重建。
_BUILD_LEASE_SECONDS = 300.0
# 等待 building 完成的重试间隔。
_WAIT_RETRY_SECONDS = 0.05
# MySQL 可重试错误（死锁 1213 / 锁等待超时 1205）的有限重试预算与指数退避基值。
_MAX_DB_RETRY_ATTEMPTS = 5
_DB_RETRY_BASE_SECONDS = 0.05
_MYSQL_RETRYABLE_CODES = frozenset({1205, 1213})


def sanitize_filename(name: str) -> str:
    cleaned = _FILENAME_UNSAFE.sub("_", name).strip("._")
    return cleaned or "artifact"


def _mysql_error_code(exc: OperationalError) -> int | None:
    """提取 MySQL 错误码（asyncmy 的 orig.args[0] 为 int code）。"""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    args = getattr(orig, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _is_mysql_retryable(exc: OperationalError) -> bool:
    """只对 MySQL 可重试的死锁/锁等待做退避；连接断开等错误直接抛出。"""
    code = _mysql_error_code(exc)
    if code is not None:
        return code in _MYSQL_RETRYABLE_CODES
    message = str(exc).casefold()
    return "deadlock" in message or "lock wait timeout" in message


def _retry_backoff(attempt_index: int) -> float:
    """第 attempt_index 次重试（0 起）的指数退避时长。"""
    return _DB_RETRY_BASE_SECONDS * (2**attempt_index)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ExportedFile:
    """导出结果（用户可见面：不暴露 storage_key）。"""

    filename: str
    sha256: str
    size_bytes: int
    content: bytes


def export_cache_key(
    *,
    artifact_version_id: str,
    schema_version: str,
    payload: dict[str, Any] | None = None,
    exporter_version: str | None = None,
    layout_digest: str | None = None,
) -> str:
    """生成导出缓存身份；标准 Artifact 保持历史 schema_version 语义。"""
    if schema_version == "analysis_report_v1":
        exporter_version = exporter_version or ANALYSIS_REPORT_EXPORTER_VERSION
        if layout_digest is None:
            if payload is None:
                raise ArtifactExportUnsupported(
                    schema_version, reason="no published payload for layout digest"
                )
            layout_digest = workbook_layout_digest(
                AnalysisReportV1.model_validate(payload).workbook
            )
        return hashlib.sha256(
            f"{artifact_version_id}{exporter_version}{layout_digest}".encode("utf-8")
        ).hexdigest()
    if exporter_version is not None or layout_digest is not None:
        return hashlib.sha256(
            f"{artifact_version_id}{exporter_version or schema_version}{layout_digest or ''}".encode(
                "utf-8"
            )
        ).hexdigest()
    return schema_version


class ExportCacheService:
    """按 Version 构建并缓存 Excel 导出文件。"""

    def __init__(
        self,
        db_session: AsyncSession,
        *,
        storage_dir: str | None = None,
        renderer: Any | None = None,
        lease_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self._db = db_session
        self._storage_dir = Path(storage_dir or settings.agent_export_storage_dir)
        self._renderer = renderer  # 测试注入渲染桩；None 用 export_artifact
        self._lease_seconds = lease_seconds if lease_seconds is not None else _BUILD_LEASE_SECONDS

    async def get_or_build(
        self,
        *,
        artifact_version_id: str,
        schema_version: str,
        payload: dict[str, Any] | None,
        filename: str,
        lineage_snapshot: dict[str, Any] | None = None,
        exporter_version: str | None = None,
        layout_digest: str | None = None,
    ) -> ExportedFile:
        """构建并返回缓存文件；并发下只渲染一次。

        ``lineage_snapshot`` 透传给构建路径（direct payload 识别所需：
        ``mode == "model_direct_v1"`` 时 export_artifact 启用 direct lineage
        context）；缓存命中路径不消费该参数。
        """
        template_version = export_cache_key(
            artifact_version_id=artifact_version_id,
            schema_version=schema_version,
            payload=payload,
            exporter_version=exporter_version,
            layout_digest=layout_digest,
        )
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        retry_budget = _MAX_DB_RETRY_ATTEMPTS
        while True:
            # 等待方必须重新读取最新状态（identity map 旧对象会永久卡住）；
            # 用 populate_existing 强制重读 ArtifactExport 行，不波及调用方
            # 持有的其他 ORM 对象。
            try:
                row = await self._db.scalar(
                    select(ArtifactExport)
                    .where(
                        ArtifactExport.artifact_version_id == artifact_version_id,
                        ArtifactExport.template_version == template_version,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            except OperationalError as exc:
                # 只对 MySQL 死锁/锁等待做有限次数指数退避；连接断开等直接抛。
                await self._db.rollback()
                if not _is_mysql_retryable(exc) or retry_budget <= 0:
                    raise
                retry_budget -= 1
                await asyncio.sleep(_retry_backoff(_MAX_DB_RETRY_ATTEMPTS - retry_budget - 1))
                continue
            now = _now()
            if (
                row is not None
                and row.status == "ready"
                and row.filename
                and row.storage_key
                and row.sha256
                and row.size_bytes
            ):
                # ready 命中必须同时校验 filename/storage_key/sha256/size_bytes；
                # 任一元数据不完整、文件缺失、hash/size 不匹配都统一失效重建。
                path = self._storage_dir / row.storage_key
                if path.exists():
                    content = await asyncio.to_thread(self._read_file, path)
                    if (
                        hashlib.sha256(content).hexdigest() == row.sha256
                        and len(content) == row.size_bytes
                    ):
                        return ExportedFile(
                            filename=row.filename,
                            sha256=row.sha256,
                            size_bytes=row.size_bytes,
                            content=content,
                        )
                    # hash/size 不匹配：先删除旧损坏文件再重建。
                    await asyncio.to_thread(self._delete_file, path)
                # 元数据不完整/文件缺失/hash 不匹配：标记失效，重建（换新 token）。
                row.status = "building"
                row.error_code = None
                row.sha256 = None
                row.storage_key = None
                row.size_bytes = None
                row.completed_at = None
                row.created_at = now
                row.claim_token = uuid4().hex
                await self._db.commit()
            elif row is not None and row.status == "building":
                stale_seconds = (now - row.created_at).total_seconds()
                if stale_seconds < self._lease_seconds:
                    # 租约内：等待构建方完成。
                    await self._db.commit()
                    await asyncio.sleep(_WAIT_RETRY_SECONDS)
                    continue
                # stale building：接管重建（换新 claim_token，旧 owner 被 fence）。
                row.created_at = now
                row.completed_at = None
                row.claim_token = uuid4().hex
                await self._db.commit()
            elif row is None:
                row = ArtifactExport(
                    id=str(uuid4()),
                    artifact_version_id=artifact_version_id,
                    template_version=template_version,
                    status="building",
                    filename=None,
                    storage_key=None,
                    sha256=None,
                    size_bytes=None,
                    error_code=None,
                    created_at=now,
                    claim_token=uuid4().hex,
                )
                self._db.add(row)
                try:
                    await self._db.commit()
                except IntegrityError:
                    await self._db.rollback()  # 并发撞唯一约束 → 重读 ready。
                    continue
                except OperationalError as exc:
                    await self._db.rollback()
                    if not _is_mysql_retryable(exc) or retry_budget <= 0:
                        raise
                    retry_budget -= 1
                    await asyncio.sleep(_retry_backoff(_MAX_DB_RETRY_ATTEMPTS - retry_budget - 1))
                    continue
            else:
                # failed 行：覆盖为 building 重试（换新 claim_token）。
                row.status = "building"
                row.error_code = None
                row.filename = None
                row.storage_key = None
                row.sha256 = None
                row.size_bytes = None
                row.created_at = now
                row.completed_at = None
                row.claim_token = uuid4().hex
                await self._db.commit()

            # 本调用负责渲染（已持有 building 行 + 有效租约 + 本次 claim_token）。
            claim_token = row.claim_token
            try:
                content = await asyncio.to_thread(
                    self._render,
                    schema_version,
                    payload,
                    filename,
                    lineage_snapshot,
                )
            except asyncio.CancelledError:
                # 取消必须安全收尾（标记 failed 可接管）后重新抛出。
                await self._db.rollback()
                await self._mark_failed(
                    artifact_version_id,
                    template_version,
                    error_code="export_cancelled",
                    claim_token=claim_token,
                )
                raise
            except Exception as exc:
                await self._db.rollback()
                await self._mark_failed(
                    artifact_version_id,
                    template_version,
                    error_code=getattr(exc, "code", "export_failed"),
                    claim_token=claim_token,
                )
                raise
            safe_name = sanitize_filename(filename)
            storage_key = f"{artifact_version_id[:8]}-{uuid4().hex[:12]}.xlsx"
            path = self._storage_dir / storage_key
            # 后台写入包装为可跟踪 Task 并用 shield 隔离外层取消：取消
            # to_thread 的 await 不会终止后台写线程，若直接删除文件返回，
            # 线程随后 os.replace 会重新生成未登记的孤儿文件（P1-1）。
            # 取消后必须等待写任务真正结束，再清理本 owner 的 .xlsx/.tmp。
            write_task = asyncio.create_task(
                asyncio.to_thread(self._write_atomic, path, content)
            )
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                # 外层取消：shield 保证写入任务继续运行，等待它真正结束。
                await asyncio.gather(write_task, return_exceptions=True)
                # 后台写入结束后清理本 owner 生成的最终文件与半写临时文件；
                # storage_key 是本 owner 的 uuid，不会误删接管方文件。
                await asyncio.to_thread(self._delete_file, path)
                await asyncio.to_thread(self._delete_file, path.with_suffix(".tmp"))
                await self._db.rollback()
                await self._mark_failed(
                    artifact_version_id,
                    template_version,
                    error_code="export_cancelled",
                    claim_token=claim_token,
                )
                raise
            except Exception:
                await self._mark_failed(
                    artifact_version_id,
                    template_version,
                    error_code="export_write_failed",
                    claim_token=claim_token,
                )
                raise
            try:
                marked = await self._mark_ready(
                    artifact_version_id,
                    template_version,
                    filename=safe_name,
                    storage_key=storage_key,
                    content=content,
                    claim_token=claim_token,
                )
            except asyncio.CancelledError:
                # mark_ready 阶段取消：可能已提交也可能未提交，重读确认。
                await self._db.rollback()
                current = await self._db.scalar(
                    select(ArtifactExport).where(
                        ArtifactExport.artifact_version_id == artifact_version_id,
                        ArtifactExport.template_version == template_version,
                    )
                )
                committed = (
                    current is not None
                    and current.status == "ready"
                    and current.storage_key == storage_key
                    and current.claim_token == claim_token
                )
                if committed:
                    await self._db.commit()  # 已生效：文件保留，他人可复用。
                else:
                    # 未生效：清理自己的文件（mark_failed 仍带 token fence，
                    # 丢失租约的 owner 不能更新缓存行）。
                    await asyncio.to_thread(self._delete_file, path)
                    await self._mark_failed(
                        artifact_version_id,
                        template_version,
                        error_code="export_cancelled",
                        claim_token=claim_token,
                    )
                raise
            except Exception:
                # 文件已写但 DB 更新失败：清理孤儿文件，标记 failed（可恢复）。
                await asyncio.to_thread(self._delete_file, path)
                await self._mark_failed(
                    artifact_version_id,
                    template_version,
                    error_code="export_db_failed",
                    claim_token=claim_token,
                )
                raise
            if not marked:
                # 渲染期间被接管（claim_token 已换）：本次结果被 fence，清理
                # 孤儿文件后重读新 owner 状态，绝不覆盖接管方。
                await asyncio.to_thread(self._delete_file, path)
                await self._db.commit()
                continue
            return ExportedFile(
                filename=safe_name,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                content=content,
            )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _render(
        self,
        schema_version: str,
        payload: dict[str, Any] | None,
        filename: str,
        lineage_snapshot: dict[str, Any] | None = None,
    ) -> bytes:
        if self._renderer is not None:
            return self._renderer(payload)
        if payload is None:
            raise ArtifactExportUnsupported(schema_version, reason="no published payload")
        return export_artifact(
            _VersionLike(
                schema_version=schema_version,
                payload_json=payload,
                lineage_snapshot_json=lineage_snapshot,
            )
        )

    async def _mark_failed(
        self,
        artifact_version_id: str,
        template_version: str,
        *,
        error_code: str,
        claim_token: str | None,
    ) -> bool:
        """条件更新 owner fencing：仅 claim_token 匹配的当前 owner 能落 failed。

        返回是否命中（rowcount>0）；被接管的僵尸构建方影响 0 行。
        """
        result = await self._db.execute(
            update(ArtifactExport)
            .where(
                ArtifactExport.artifact_version_id == artifact_version_id,
                ArtifactExport.template_version == template_version,
                ArtifactExport.claim_token == claim_token,
            )
            .values(status="failed", error_code=error_code, completed_at=_now())
        )
        await self._db.commit()
        return bool(result.rowcount)

    async def _mark_ready(
        self,
        artifact_version_id: str,
        template_version: str,
        *,
        filename: str,
        storage_key: str,
        content: bytes,
        claim_token: str | None,
    ) -> bool:
        """条件更新 owner fencing：仅 claim_token 匹配的当前 owner 能落 ready。

        返回是否命中（rowcount>0）；渲染期间被接管则返回 False，调用方清理
        孤儿文件并重读新 owner 状态，绝不覆盖接管方结果。
        """
        result = await self._db.execute(
            update(ArtifactExport)
            .where(
                ArtifactExport.artifact_version_id == artifact_version_id,
                ArtifactExport.template_version == template_version,
                ArtifactExport.claim_token == claim_token,
            )
            .values(
                status="ready",
                filename=filename,
                storage_key=storage_key,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                completed_at=_now(),
            )
        )
        await self._db.commit()
        return bool(result.rowcount)

    @staticmethod
    def _read_file(path: Path) -> bytes:
        return path.read_bytes()

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        temp = path.with_suffix(".tmp")
        temp.write_bytes(content)
        os.replace(temp, path)

    @staticmethod
    def _delete_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class _VersionLike:
    """导出器读取面桩：schema_version + payload_json + lineage_snapshot_json。"""

    def __init__(
        self,
        *,
        schema_version: str,
        payload_json: dict[str, Any],
        lineage_snapshot_json: dict[str, Any] | None = None,
    ) -> None:
        self.schema_version = schema_version
        self.payload_json = payload_json
        self.lineage_snapshot_json = lineage_snapshot_json
        self.data_status = "complete"


__all__ = [
    "ExportedFile",
    "ExportCacheService",
    "export_cache_key",
    "sanitize_filename",
]
