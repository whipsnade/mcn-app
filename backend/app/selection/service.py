from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.selection.models import SessionKolSelection
from app.selection.normalizers import (
    _MERGEABLE_FIELDS,
    UnknownEvidenceToolError,
    normalize_tool_evidence,
)
from app.selection.schemas import DimensionInputs, NormalizedKolEvidence, ToolEvidence
from app.selection.scoring import rating, score_candidate
from app.tasks.errors import _PLATFORM_LABELS
from app.workspace.models import WorkspaceSession


# datasource 中文标签 → 平台码（_PLATFORM_LABELS 反转）；同时容忍参数里
# 直接写平台码（如 ["douyin"]）的形态。
_DATASOURCE_PLATFORM_CODES = {
    **{label: code for code, label in _PLATFORM_LABELS.items()},
    **{code: code for code in _PLATFORM_LABELS},
}


def _payload_with_platform_hint(
    structured_content: dict[str, Any], arguments: dict | None
) -> dict[str, Any]:
    """kol.detail/insight 工具的平台不在行内，从调用参数注入 payload 级提示。

    - arguments.platform（如 kol.detail 的 "xiaohongshu"）直接注入；
    - arguments.datasource（insight 工具，形如 ["小红书"]）取首个可映射标签，
      经中文→平台码映射注入；无匹配则不注入。
    payload 已有 platform 键时不覆盖。
    """
    if not isinstance(arguments, dict) or not arguments or "platform" in structured_content:
        return structured_content
    platform = arguments.get("platform")
    if isinstance(platform, str) and platform.strip():
        return {**structured_content, "platform": platform.strip()}
    datasource = arguments.get("datasource")
    if isinstance(datasource, (list, tuple)):
        for label in datasource:
            code = _DATASOURCE_PLATFORM_CODES.get(str(label).strip())
            if code:
                return {**structured_content, "platform": code}
    return structured_content


_NESTED_DICT_FIELDS = ("export_fields", "analytics_fields")
# as_dict() 中与评分缺失判定相关的标量字段，复用 normalizers 的合并字段清单。
_MISSING_FIELD_NAMES = _MERGEABLE_FIELDS


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _merge_fields(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """合并同一达人的两次证据：新值非空才覆盖，已有值不被新空值冲掉。"""
    merged = dict(old)
    for key, value in new.items():
        if key in _NESTED_DICT_FIELDS:
            nested = dict(old.get(key) or {})
            for nested_key, nested_value in (value or {}).items():
                if _present(nested_value):
                    nested[nested_key] = nested_value
            merged[key] = nested
        elif key == "evidence_references":
            merged[key] = list(dict.fromkeys([*(old.get(key) or []), *(value or [])]))
        elif key == "risk_flags":
            by_digest: dict[str, Any] = {}
            for flag in [*(old.get(key) or []), *(value or [])]:
                digest = json.dumps(flag, sort_keys=True, ensure_ascii=False, default=str)
                by_digest[digest] = flag
            merged[key] = list(by_digest.values())
        elif _present(value):
            merged[key] = value
    merged["missing_fields"] = [
        name for name in _MISSING_FIELD_NAMES if merged.get(name) is None
    ]
    return merged


def _score_payload(fields: dict[str, Any]) -> dict[str, Any]:
    dimensions = DimensionInputs(
        audience=fields.get("audience_score"),
        content=fields.get("content_score"),
        engagement=fields.get("engagement_score"),
        budget=fields.get("budget_score"),
        growth=fields.get("growth_score"),
        brand_safety=fields.get("brand_safety_score"),
    )
    score = score_candidate(dimensions, profile="balanced")
    payload = score.as_dict()
    payload["rating"], payload["stars"] = rating(score.total)
    return payload


def _score_total(row: SessionKolSelection) -> float:
    return float((row.score_json or {}).get("total") or 0.0)


class KolSelectionService:
    """圈选名单沉淀：KOL 工具 settled 证据 → session_kol_selections upsert。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def ingest_tool_evidence(
        self,
        *,
        user_id: str,
        session_id: str,
        task_id: str,
        tool_name: str,
        structured_content: Any,
        arguments: dict | None = None,
    ) -> int:
        """解析一条 settled 工具证据并 upsert 圈选名单，返回写入行数。

        ``tool_name`` 为内部工具名（normalizers 适配器按内部名匹配）。
        ``arguments`` 为本次工具调用参数：kol.detail/insight 等工具的平台
        身份只在参数里（platform / datasource），构建证据时注入 payload。
        非 KOL 工具（UnknownEvidenceToolError）与无法解析的载荷返回 0。
        """
        if not isinstance(structured_content, dict) or not structured_content:
            return 0
        evidence = ToolEvidence(
            internal_tool_name=tool_name,
            payload=_payload_with_platform_hint(structured_content, arguments),
            source_call_id=task_id,
            collected_at=_utcnow(),
        )
        try:
            normalized_rows = normalize_tool_evidence([evidence])
        except UnknownEvidenceToolError:
            return 0
        except ValueError:
            # 载荷整体畸形（如 result 非 JSON 字符串）：不阻塞任务循环。
            return 0
        for item in normalized_rows:
            await self._upsert(
                user_id=user_id,
                session_id=session_id,
                task_id=task_id,
                tool_name=tool_name,
                item=item,
            )
        await self._db.flush()
        return len(normalized_rows)

    async def list_selection(
        self, *, user_id: str, session_id: str, offset: int = 0, limit: int = 200
    ) -> tuple[int, list[SessionKolSelection]]:
        """(总数, 按 score 倒序的行)，校验会话归属。"""
        await self._require_owned_session(user_id, session_id)
        rows = await self._session_rows(session_id)
        rows.sort(key=_score_total, reverse=True)
        return len(rows), rows[offset : offset + limit]

    async def count_selection(self, *, session_id: str) -> int:
        total = await self._db.scalar(
            select(func.count())
            .select_from(SessionKolSelection)
            .where(SessionKolSelection.session_id == session_id)
        )
        return int(total or 0)

    async def count_selections(self, *, session_ids: Sequence[str]) -> dict[str, int]:
        """批量统计多个会话的圈选行数，未出现的会话补 0（列表页避免 N+1）。"""
        if not session_ids:
            return {}
        result = await self._db.execute(
            select(SessionKolSelection.session_id, func.count())
            .where(SessionKolSelection.session_id.in_(session_ids))
            .group_by(SessionKolSelection.session_id)
        )
        counts = {session_id: int(count) for session_id, count in result.all()}
        return {session_id: counts.get(session_id, 0) for session_id in session_ids}

    async def get_all_for_export(
        self, *, user_id: str, session_id: str
    ) -> list[SessionKolSelection]:
        await self._require_owned_session(user_id, session_id)
        rows = await self._session_rows(session_id)
        rows.sort(key=_score_total, reverse=True)
        return rows

    async def _session_rows(self, session_id: str) -> list[SessionKolSelection]:
        return list(
            (
                await self._db.scalars(
                    select(SessionKolSelection).where(
                        SessionKolSelection.session_id == session_id
                    )
                )
            ).all()
        )

    async def _require_owned_session(self, user_id: str, session_id: str) -> None:
        session = await self._db.get(WorkspaceSession, session_id)
        if session is None or session.user_id != user_id or session.deleted_at is not None:
            raise LookupError("session_not_found")

    async def _upsert(
        self,
        *,
        user_id: str,
        session_id: str,
        task_id: str,
        tool_name: str,
        item: NormalizedKolEvidence,
    ) -> None:
        platform = item.platform[:32]
        kol_uid = item.platform_account_id[:128]
        existing = await self._db.scalar(
            select(SessionKolSelection).where(
                SessionKolSelection.session_id == session_id,
                SessionKolSelection.platform == platform,
                SessionKolSelection.kol_uid == kol_uid,
            )
        )
        now = _utcnow()
        if existing is None:
            fields = item.as_dict()
            row = SessionKolSelection(
                id=str(uuid4()),
                user_id=user_id,
                session_id=session_id,
                platform=platform,
                kol_uid=kol_uid,
                source_tool=tool_name,
                first_task_id=task_id,
                last_task_id=task_id,
                created_at=now,
                updated_at=now,
            )
            self._apply_fields(row, fields)
            self._db.add(row)
            return
        fields = _merge_fields(existing.fields_json or {}, item.as_dict())
        existing.last_task_id = task_id
        existing.updated_at = now
        self._apply_fields(existing, fields)

    def _apply_fields(self, row: SessionKolSelection, fields: dict[str, Any]) -> None:
        export_fields = fields.get("export_fields") or {}
        city = export_fields.get("city")
        row.nickname = str(fields.get("nickname") or "")[:200]
        row.followers = fields.get("followers")
        row.city = str(city)[:64] if _present(city) else None
        profile_url = fields.get("normalized_profile_url")
        row.profile_url = str(profile_url)[:512] if _present(profile_url) else None
        row.fields_json = fields
        row.score_json = _score_payload(fields)
