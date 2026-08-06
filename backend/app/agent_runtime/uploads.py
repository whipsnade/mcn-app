"""安全上传与解析服务（Gate B Task 3）。

用户 CSV/XLSX 上传：扩展名白名单（.csv/.xlsx，拒绝 .xlsm 等宏格式）、
大小上限（默认 20 MiB）、数据行上限（默认 50,000）；SHA-256 作为不可变
文件名的一部分，路径只由服务生成（绝不拼接用户文件名）。CSV 用
``utf-8-sig``，XLSX 用 ``openpyxl.read_only + data_only + keep_links=False``
（不执行宏/公式）。解析在线程执行；结构化 rows 作为 upload Evidence
（``tool_call_id`` 为 NULL、``upload_id`` 有值，XOR 由 DB 约束保证）。

原始上传不可变：重新解析产生新 Evidence，不覆盖来源（Evidence 层
append-only 语义）。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import AgentUpload
from app.core.config import get_settings

_ALLOWED_EXTENSIONS = (".csv", ".xlsx")
_MIME_BY_EXTENSION = {
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# upload Evidence preview 行上限（raw payload 不受影响，仍完整落库）。
_PREVIEW_ROW_CAP = 5000


class UploadRejectedError(Exception):
    """上传被拒绝（大小/格式/行数）；携带 HTTP 状态码与结构化错误码。"""

    def __init__(self, *, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_csv(content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = [
        {key: value for key, value in row.items() if key is not None}
        for row in reader
        if any(value not in (None, "") for value in row.values())
    ]
    return (reader.fieldnames or []), rows


def _parse_xlsx(content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    from openpyxl import load_workbook

    workbook = load_workbook(
        io.BytesIO(content),
        read_only=True,
        data_only=True,
        keep_links=False,
    )
    try:
        sheet = workbook.active
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if row is None or all(cell is None for cell in row):
                continue
            if index == 0:
                columns = [str(cell) if cell is not None else "" for cell in row]
                continue
            rows.append(
                {
                    columns[cell_index]: cell
                    for cell_index, cell in enumerate(row)
                    if cell_index < len(columns)
                }
            )
        return columns, rows
    finally:
        workbook.close()


class UploadService:
    """上传落盘 + 解析 + upload Evidence 写入（单事务）。"""

    def __init__(
        self,
        db_session: AsyncSession,
        *,
        storage_dir: str | None = None,
        max_bytes: int | None = None,
        max_rows: int | None = None,
    ) -> None:
        settings = get_settings()
        self._db = db_session
        self._storage_dir = Path(storage_dir or settings.agent_upload_storage_dir)
        self._max_bytes = max_bytes or settings.agent_upload_max_bytes
        self._max_rows = max_rows or settings.agent_upload_max_rows

    async def create_and_parse(
        self,
        *,
        user_id: str,
        session_id: str,
        filename: str,
        mime_type: str,
        content: bytes,
    ) -> AgentUpload:
        extension = Path(filename).suffix.lower()
        if extension not in _ALLOWED_EXTENSIONS:
            raise UploadRejectedError(
                status_code=415,
                error_code="unsupported_media_type",
                message=f"only {', '.join(_ALLOWED_EXTENSIONS)} uploads are allowed",
            )
        if len(content) > self._max_bytes:
            raise UploadRejectedError(
                status_code=413,
                error_code="upload_too_large",
                message=f"file exceeds {self._max_bytes} bytes",
            )

        upload_id = str(uuid4())
        sha256 = hashlib.sha256(content).hexdigest()
        # 路径只由服务生成：sha256 片段 + 服务生成的 upload_id，绝不拼接用户文件名。
        storage_key = f"{user_id}/{upload_id[:8]}-{sha256[:16]}{extension}"
        await asyncio.to_thread(self._write_file, storage_key, content)

        upload = AgentUpload(
            id=upload_id,
            user_id=user_id,
            session_id=session_id,
            run_id=None,
            original_filename=filename,
            mime_type=mime_type or _MIME_BY_EXTENSION[extension],
            size_bytes=len(content),
            sha256=sha256,
            storage_key=storage_key,
            status="uploaded",
            created_at=_now(),
        )
        self._db.add(upload)
        await self._db.flush()

        columns, rows = await asyncio.to_thread(self._parse, extension, content)
        if len(rows) > self._max_rows:
            upload.status = "failed"
            upload.error_code = "rows_exceeded"
            upload.completed_at = _now()
            await self._db.flush()
            raise UploadRejectedError(
                status_code=400,
                error_code="rows_exceeded",
                message=f"file exceeds {self._max_rows} data rows",
            )

        # 上传列映射诊断：所有列都是用户自定义 schema，不映射 MCP 规范键，
        # 全部列为 unmapped_fields 供模型参考（不误报失败）。
        from app.agent_runtime.normalization import NormalizationResult

        upload_normalization = NormalizationResult(
            version="upload_v1",
            status="not_applicable",
            preview={"columns": columns, "row_count": len(rows)},
            field_mapping={},
            unmapped_fields=tuple(columns),
            truncated=len(rows) > _PREVIEW_ROW_CAP,
        )
        await EvidenceWriter(self._db).write(
            session_id=session_id,
            run_id=None,
            tool_call_id=None,
            upload_id=upload_id,
            source_type="user_upload",
            source_name="user_upload",
            scope_json=None,
            period_json=None,
            raw_payload={
                "columns": columns,
                "rows": rows,
                "truncated": len(rows) > _PREVIEW_ROW_CAP,
            },
            normalization=upload_normalization,
        )
        upload.status = "parsed"
        upload.completed_at = _now()
        await self._db.flush()
        return upload

    def _write_file(self, storage_key: str, content: bytes) -> None:
        path = self._storage_dir / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _parse(self, extension: str, content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
        if extension == ".csv":
            return _parse_csv(content)
        return _parse_xlsx(content)

    async def get_owned(self, *, user_id: str, upload_id: str) -> AgentUpload | None:
        return await self._db.scalar(
            select(AgentUpload).where(
                AgentUpload.id == upload_id,
                AgentUpload.user_id == user_id,
            )
        )


__all__ = [
    "UploadRejectedError",
    "UploadService",
]
