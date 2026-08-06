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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact
from app.agent_artifacts.models import ArtifactExport
from app.core.config import get_settings

# 导出文件名清洗：只保留安全字符，防止路径穿越/注入。
_FILENAME_UNSAFE = re.compile(r"[^0-9A-Za-z._\-]")
# building 租约：超过该时长视为 stale，可被接管重建。
_BUILD_LEASE_SECONDS = 300.0
# 等待 building 完成的重试间隔。
_WAIT_RETRY_SECONDS = 0.05


def sanitize_filename(name: str) -> str:
    cleaned = _FILENAME_UNSAFE.sub("_", name).strip("._")
    return cleaned or "artifact"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ExportedFile:
    """导出结果（用户可见面：不暴露 storage_key）。"""

    filename: str
    sha256: str
    size_bytes: int
    content: bytes


def _template_version_for(schema_version: str) -> str:
    """模板版本：schema_version 即模板标识（v3 族各自固定）。"""
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
    ) -> ExportedFile:
        """构建并返回缓存文件；并发下只渲染一次。"""
        template_version = _template_version_for(schema_version)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
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
            except OperationalError:
                # 并发 gap-lock 死锁：回滚后重试。
                await self._db.rollback()
                continue
            now = _now()
            if row is not None and row.status == "ready" and row.sha256 and row.filename:
                path = self._storage_dir / row.storage_key
                if path.exists():
                    content = await asyncio.to_thread(self._read_file, path)
                    if hashlib.sha256(content).hexdigest() == row.sha256:
                        return ExportedFile(
                            filename=row.filename,
                            sha256=row.sha256,
                            size_bytes=row.size_bytes or 0,
                            content=content,
                        )
                # 文件丢失或 hash 不匹配：标记失效，重建。
                row.status = "building"
                row.error_code = None
                row.sha256 = None
                row.storage_key = None
                row.size_bytes = None
                row.completed_at = None
                row.created_at = now
                await self._db.commit()
            elif row is not None and row.status == "building":
                stale_seconds = (now - row.created_at).total_seconds()
                if stale_seconds < self._lease_seconds:
                    # 租约内：等待构建方完成。
                    await self._db.commit()
                    await asyncio.sleep(_WAIT_RETRY_SECONDS)
                    continue
                # stale building：接管重建（刷新租约）。
                row.created_at = now
                row.completed_at = None
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
                )
                self._db.add(row)
                try:
                    await self._db.commit()
                except IntegrityError:
                    await self._db.rollback()  # 并发撞唯一约束 → 重读 ready。
                    continue
                except OperationalError:
                    await self._db.rollback()  # 并发死锁 → 重试。
                    continue
            else:
                # failed 行：覆盖为 building 重试（刷新租约）。
                row.status = "building"
                row.error_code = None
                row.filename = None
                row.storage_key = None
                row.sha256 = None
                row.size_bytes = None
                row.created_at = now
                row.completed_at = None
                await self._db.commit()

            # 本调用负责渲染（已持有 building 行 + 有效租约）。
            try:
                content = await asyncio.to_thread(
                    self._render, schema_version, payload, filename
                )
            except asyncio.CancelledError:
                # 取消必须安全收尾（标记 failed 可接管）后重新抛出。
                await self._db.rollback()
                await self._mark_failed(
                    artifact_version_id, template_version, error_code="export_cancelled"
                )
                raise
            except Exception as exc:
                await self._db.rollback()
                await self._mark_failed(
                    artifact_version_id,
                    template_version,
                    error_code=getattr(exc, "code", "export_failed"),
                )
                raise
            safe_name = sanitize_filename(filename)
            storage_key = f"{artifact_version_id[:8]}-{uuid4().hex[:12]}.xlsx"
            path = self._storage_dir / storage_key
            try:
                await asyncio.to_thread(self._write_atomic, path, content)
            except Exception:
                await self._mark_failed(
                    artifact_version_id, template_version, error_code="export_write_failed"
                )
                raise
            try:
                await self._mark_ready(
                    artifact_version_id,
                    template_version,
                    filename=safe_name,
                    storage_key=storage_key,
                    content=content,
                )
            except Exception:
                # 文件已写但 DB 更新失败：清理孤儿文件，标记 failed（可恢复）。
                await asyncio.to_thread(self._delete_file, path)
                await self._mark_failed(
                    artifact_version_id, template_version, error_code="export_db_failed"
                )
                raise
            return ExportedFile(
                filename=safe_name,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                content=content,
            )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _render(self, schema_version: str, payload: dict[str, Any] | None, filename: str) -> bytes:
        if self._renderer is not None:
            return self._renderer(payload)
        if payload is None:
            raise ArtifactExportUnsupported(schema_version, reason="no published payload")
        return export_artifact(
            _VersionLike(schema_version=schema_version, payload_json=payload)
        )

    async def _mark_failed(
        self, artifact_version_id: str, template_version: str, *, error_code: str
    ) -> None:
        row = await self._db.scalar(
            select(ArtifactExport)
            .where(
                ArtifactExport.artifact_version_id == artifact_version_id,
                ArtifactExport.template_version == template_version,
            )
            .execution_options(populate_existing=True)
        )
        if row is not None:
            row.status = "failed"
            row.error_code = error_code
            row.completed_at = _now()
            await self._db.commit()

    async def _mark_ready(
        self,
        artifact_version_id: str,
        template_version: str,
        *,
        filename: str,
        storage_key: str,
        content: bytes,
    ) -> None:
        row = await self._db.scalar(
            select(ArtifactExport)
            .where(
                ArtifactExport.artifact_version_id == artifact_version_id,
                ArtifactExport.template_version == template_version,
            )
            .execution_options(populate_existing=True)
        )
        if row is not None:
            row.status = "ready"
            row.filename = filename
            row.storage_key = storage_key
            row.sha256 = hashlib.sha256(content).hexdigest()
            row.size_bytes = len(content)
            row.completed_at = _now()
            await self._db.commit()

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
    """导出器读取面桩：schema_version + payload_json。"""

    def __init__(self, *, schema_version: str, payload_json: dict[str, Any]) -> None:
        self.schema_version = schema_version
        self.payload_json = payload_json
        self.data_status = "complete"


__all__ = [
    "ExportedFile",
    "ExportCacheService",
    "sanitize_filename",
]
