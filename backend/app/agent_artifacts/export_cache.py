"""Excel 导出缓存服务（Gate C Task 6 / 设计 §11.5）。

同一 ``(artifact_version_id, template_version)`` 只构建一次：

- ``get_or_build`` 用行锁 + 唯一约束串行化并发（后到者等待先到者提交后读到
  ready 行直接复用）；
- 渲染在线程执行（openpyxl 同步 CPU 密集），先写临时文件再原子 rename；
- 失败行落 ``failed`` + error_code，可安全重试（重试覆盖为 building）；
- 导出失败绝不调用模型/MCP（渲染器是纯表现层）；
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact
from app.agent_artifacts.models import ArtifactExport
from app.core.config import get_settings

# 导出文件名清洗：只保留安全字符，防止路径穿越/注入。
_FILENAME_UNSAFE = re.compile(r"[^0-9A-Za-z._\-]")


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
    ) -> None:
        settings = get_settings()
        self._db = db_session
        self._storage_dir = Path(storage_dir or settings.agent_export_storage_dir)
        self._renderer = renderer  # 测试注入渲染桩；None 用 export_artifact

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
            row = await self._db.scalar(
                select(ArtifactExport)
                .where(
                    ArtifactExport.artifact_version_id == artifact_version_id,
                    ArtifactExport.template_version == template_version,
                )
                .with_for_update()
            )
            if row is not None and row.status == "ready" and row.sha256 and row.filename:
                content = await asyncio.to_thread(
                    self._read_file, self._storage_dir / row.storage_key
                )
                return ExportedFile(
                    filename=row.filename,
                    sha256=row.sha256,
                    size_bytes=row.size_bytes or 0,
                    content=content,
                )
            if row is not None and row.status == "building":
                await self._db.commit()  # 释放行锁，等待构建方提交后重读。
                await asyncio.sleep(0.05)
                continue
            if row is None:
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
                    created_at=_now(),
                )
                self._db.add(row)
                try:
                    await self._db.commit()
                except IntegrityError:
                    await self._db.rollback()  # 并发撞唯一约束 → 重读 ready。
                    continue
            else:
                # failed 行：覆盖为 building 重试。
                row.status = "building"
                row.error_code = None
                row.filename = None
                row.storage_key = None
                row.sha256 = None
                row.size_bytes = None
                row.created_at = _now()
                row.completed_at = None
                await self._db.commit()

            # 本调用负责渲染（已持有 building 行）。
            try:
                content = await asyncio.to_thread(
                    self._render, schema_version, payload, filename
                )
            except (ArtifactExportUnsupported, Exception) as exc:
                await self._db.rollback()
                await self._mark_failed(
                    artifact_version_id,
                    template_version,
                    error_code=getattr(exc, "code", "export_failed"),
                )
                raise
            safe_name = sanitize_filename(filename)
            storage_key = f"{artifact_version_id[:8]}-{uuid4().hex[:12]}.xlsx"
            await asyncio.to_thread(
                self._write_atomic,
                self._storage_dir / storage_key,
                content,
            )
            await self._mark_ready(
                artifact_version_id,
                template_version,
                filename=safe_name,
                storage_key=storage_key,
                content=content,
            )
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
            select(ArtifactExport).where(
                ArtifactExport.artifact_version_id == artifact_version_id,
                ArtifactExport.template_version == template_version,
            )
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
            select(ArtifactExport).where(
                ArtifactExport.artifact_version_id == artifact_version_id,
                ArtifactExport.template_version == template_version,
            )
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
