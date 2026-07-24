from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.selection.models import KolSelectionItem, KolSelectionSet, SessionKolSelection
from app.selection.normalizers import (
    _GENERIC_PLATFORM_ALIASES,
    _MERGEABLE_FIELDS,
    UnknownEvidenceToolError,
    normalize_tool_evidence,
)
from app.selection.schemas import DimensionInputs, NormalizedKolEvidence, ToolEvidence
from app.selection.scoring import rating, score_candidate
from app.workspace.models import WorkspaceSession


def _normalize_platform_code(value: Any) -> str | None:
    """平台标签/平台码（任意大小写、含空白）→ 规范平台码；无法识别返回 None。"""
    if not isinstance(value, str):
        return None
    return _GENERIC_PLATFORM_ALIASES.get(value.strip().casefold())


def _payload_with_platform_hint(
    structured_content: dict[str, Any], arguments: dict | None
) -> dict[str, Any]:
    """kol.detail/insight 工具的平台不在行内，从调用参数注入 payload 级提示。

    - arguments.platform（如 kol.detail 的 "xiaohongshu"）：经别名/大小写
      规范化后注入（"小红书"/"Xiaohongshu" → "xiaohongshu"），无法识别不注入；
    - arguments.datasource（insight 工具，形如 ["小红书"] 或裸字符串 "小红书"）
      取首个可映射标签，同样规范化后注入。
    payload 已有 platform 键时不覆盖。
    """
    if not isinstance(arguments, dict) or not arguments or "platform" in structured_content:
        return structured_content
    code = _normalize_platform_code(arguments.get("platform"))
    if code is None:
        datasource = arguments.get("datasource")
        if isinstance(datasource, str):
            labels: tuple[Any, ...] = (datasource,)
        elif isinstance(datasource, (list, tuple)):
            labels = tuple(datasource)
        else:
            labels = ()
        code = next(
            (mapped for label in labels if (mapped := _normalize_platform_code(label))),
            None,
        )
    if code is None:
        return structured_content
    return {**structured_content, "platform": code}


_NESTED_DICT_FIELDS = ("export_fields", "analytics_fields")
# as_dict() 中与评分缺失判定相关的标量字段，复用 normalizers 的合并字段清单。
_MISSING_FIELD_NAMES = _MERGEABLE_FIELDS
# normalizers 对缺稳定身份的达人派生的占位 uid 格式（{platform}:{sha256hex24}）。
_DERIVED_UID_PATTERN = re.compile(r"[a-z0-9_]+:[0-9a-f]{24}")


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


def _score_total(row: SessionKolSelection | KolSelectionItem) -> float:
    return float((row.score_json or {}).get("total") or 0.0)


def serialize_selection_item(row: SessionKolSelection | KolSelectionItem) -> dict[str, Any]:
    """端点 item DTO：session_kol_selections 与 kol_selection_items 行形状一致。"""
    return {
        "platform": row.platform,
        "kol_uid": row.kol_uid,
        "nickname": row.nickname,
        "followers": row.followers,
        "city": row.city,
        "profile_url": row.profile_url,
        "fields": row.fields_json,
        "score": row.score_json,
    }


class KolSelectionService:
    """圈选名单沉淀：KOL 工具 settled 证据 → session_kol_selections / kol_selection_items。

    旧表（session_kol_selections）与新表（set/items）双写过渡，阶段五停写旧表。
    """

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
        normalized_rows = self._normalize_evidence(
            tool_name=tool_name,
            structured_content=structured_content,
            arguments=arguments,
            source_call_id=task_id,
        )
        for item in normalized_rows:
            await self._upsert_row(
                model=SessionKolSelection,
                owner_attr=SessionKolSelection.session_id,
                owner_id=session_id,
                user_id=user_id,
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

    async def ensure_selection_set(
        self,
        user_id: str,
        session_id: str,
        *,
        task_id: str | None = None,
        goal_id: str | None = None,
        title: str,
        scope: dict[str, Any] | None = None,
    ) -> KolSelectionSet:
        """取或建一份圈选名单容器；task_id/goal_id 非空时按二者查重（幂等）。

        version 并发模式与 AnalysisReportService.build_session_report 一致：
        锁定读（FOR UPDATE）max(version)+1，撞 (session_id, version) 唯一约束
        时 SAVEPOINT 回滚重试一次，再失败抛领域错误。

        错误契约：
        - ``LookupError("session_not_found")``：会话不存在/不属于该用户/已软删；
        - ``LookupError("selection_set_version_conflict")``：两次写入均撞唯一约束。
        """
        await self._require_owned_session(user_id, session_id)
        if task_id is not None or goal_id is not None:
            identity = []
            if task_id is not None:
                identity.append(KolSelectionSet.task_id == task_id)
            if goal_id is not None:
                identity.append(KolSelectionSet.goal_id == goal_id)
            existing = await self._db.scalar(
                select(KolSelectionSet)
                .where(KolSelectionSet.session_id == session_id, or_(*identity))
                .order_by(KolSelectionSet.version)
                .limit(1)
            )
            if existing is not None:
                return existing
        for attempt in (1, 2):
            version = (
                await self._db.scalar(
                    select(func.max(KolSelectionSet.version))
                    .where(KolSelectionSet.session_id == session_id)
                    .with_for_update()
                )
                or 0
            ) + 1
            now = _utcnow()
            selection_set = KolSelectionSet(
                id=str(uuid4()),
                session_id=session_id,
                task_id=task_id,
                goal_id=goal_id,
                version=version,
                title=title[:200],
                scope_json=scope,
                status="active",
                created_at=now,
                updated_at=now,
            )
            try:
                async with self._db.begin_nested():
                    self._db.add(selection_set)
                    await self._db.flush()
                return selection_set
            except IntegrityError:
                # 与 build_session_report 同策略：宽捕获重试一次，第二次仍失败
                # 说明并发写入持续竞争，抛领域错误交给调用方映射。
                if attempt == 2:
                    raise LookupError("selection_set_version_conflict") from None
        raise RuntimeError("unreachable")  # pragma: no cover

    async def ingest_tool_evidence_to_set(
        self,
        *,
        user_id: str,
        selection_set_id: str,
        task_id: str,
        tool_name: str,
        structured_content: Any,
        arguments: dict | None = None,
    ) -> int:
        """解析一条 settled 工具证据并写入指定名单（kol_selection_items）。

        归一化、派生 uid 二次归并、字段合并与评分逻辑与旧表路径完全一致；
        归属校验经 set→session。返回写入行数。
        """
        selection_set = await self._require_owned_set(user_id, selection_set_id)
        normalized_rows = self._normalize_evidence(
            tool_name=tool_name,
            structured_content=structured_content,
            arguments=arguments,
            source_call_id=task_id,
        )
        for item in normalized_rows:
            await self._upsert_row(
                model=KolSelectionItem,
                owner_attr=KolSelectionItem.selection_set_id,
                owner_id=selection_set.id,
                user_id=user_id,
                task_id=task_id,
                tool_name=tool_name,
                item=item,
            )
        await self._db.flush()
        return len(normalized_rows)

    async def latest_selection_set(self, session_id: str) -> KolSelectionSet | None:
        """会话最新一份名单容器（version 最大），无则 None。"""
        return await self._db.scalar(
            select(KolSelectionSet)
            .where(KolSelectionSet.session_id == session_id)
            .order_by(KolSelectionSet.version.desc())
            .limit(1)
        )

    async def list_latest_items(
        self, *, user_id: str, session_id: str, offset: int = 0, limit: int = 200
    ) -> tuple[int, list[KolSelectionItem]]:
        """读最新 set 的 items（无 set 返回 (0, [])）；归属校验先于查询。"""
        await self._require_owned_session(user_id, session_id)
        selection_set = await self.latest_selection_set(session_id)
        if selection_set is None:
            return 0, []
        return await self.list_selection_items(
            user_id=user_id,
            selection_set_id=selection_set.id,
            offset=offset,
            limit=limit,
        )

    async def count_latest_items(self, *, session_ids: Sequence[str]) -> dict[str, int]:
        """批量统计各会话最新 set 的 item 数（列表页避免 N+1），无 set 补 0。"""
        if not session_ids:
            return {}
        latest_versions = await self._db.execute(
            select(KolSelectionSet.session_id, func.max(KolSelectionSet.version))
            .where(KolSelectionSet.session_id.in_(session_ids))
            .group_by(KolSelectionSet.session_id)
        )
        version_by_session = {
            session_id: version for session_id, version in latest_versions.all()
        }
        if not version_by_session:
            return {session_id: 0 for session_id in session_ids}
        set_rows = (
            await self._db.execute(
                select(
                    KolSelectionSet.id,
                    KolSelectionSet.session_id,
                    KolSelectionSet.version,
                ).where(KolSelectionSet.session_id.in_(version_by_session.keys()))
            )
        ).all()
        set_id_by_session = {
            session_id: set_id
            for set_id, session_id, version in set_rows
            if version_by_session.get(session_id) == version
        }
        counts = await self._db.execute(
            select(KolSelectionItem.selection_set_id, func.count())
            .where(KolSelectionItem.selection_set_id.in_(set_id_by_session.values()))
            .group_by(KolSelectionItem.selection_set_id)
        )
        count_by_set = {set_id: int(count) for set_id, count in counts.all()}
        return {
            session_id: count_by_set.get(set_id_by_session.get(session_id), 0)
            for session_id in session_ids
        }

    async def list_selection_items(
        self, *, user_id: str, selection_set_id: str, offset: int = 0, limit: int = 200
    ) -> tuple[int, list[KolSelectionItem]]:
        """(总数, 按 score 倒序的行)，归属校验经 set→session。"""
        selection_set = await self._require_owned_set(user_id, selection_set_id)
        rows = await self._set_rows(selection_set.id)
        rows.sort(key=_score_total, reverse=True)
        return len(rows), rows[offset : offset + limit]

    async def count_items(self, selection_set_id: str) -> int:
        total = await self._db.scalar(
            select(func.count())
            .select_from(KolSelectionItem)
            .where(KolSelectionItem.selection_set_id == selection_set_id)
        )
        return int(total or 0)

    async def get_all_items_for_export(
        self, *, user_id: str, selection_set_id: str
    ) -> list[KolSelectionItem]:
        selection_set = await self._require_owned_set(user_id, selection_set_id)
        rows = await self._set_rows(selection_set.id)
        rows.sort(key=_score_total, reverse=True)
        return rows

    async def _set_rows(self, selection_set_id: str) -> list[KolSelectionItem]:
        return list(
            (
                await self._db.scalars(
                    select(KolSelectionItem).where(
                        KolSelectionItem.selection_set_id == selection_set_id
                    )
                )
            ).all()
        )

    async def _require_owned_set(self, user_id: str, selection_set_id: str) -> KolSelectionSet:
        selection_set = await self._db.get(KolSelectionSet, selection_set_id)
        if selection_set is None:
            raise LookupError("selection_set_not_found")
        await self._require_owned_session(user_id, selection_set.session_id)
        return selection_set

    def _normalize_evidence(
        self,
        *,
        tool_name: str,
        structured_content: Any,
        arguments: dict | None,
        source_call_id: str,
    ) -> list[NormalizedKolEvidence]:
        """settled 载荷 → 归一化达人证据；非 KOL 工具/畸形载荷返回空列表。"""
        if not isinstance(structured_content, dict) or not structured_content:
            return []
        evidence = ToolEvidence(
            internal_tool_name=tool_name,
            payload=_payload_with_platform_hint(structured_content, arguments),
            source_call_id=source_call_id,
            collected_at=_utcnow(),
        )
        try:
            return list(normalize_tool_evidence([evidence]))
        except UnknownEvidenceToolError:
            return []
        except ValueError:
            # 载荷整体畸形（如 result 非 JSON 字符串）：不阻塞任务循环。
            return []

    async def _upsert_row(
        self,
        *,
        model: type[SessionKolSelection] | type[KolSelectionItem],
        owner_attr: Any,
        owner_id: str,
        user_id: str,
        task_id: str,
        tool_name: str,
        item: NormalizedKolEvidence,
    ) -> None:
        """按 (归属列, platform, kol_uid) upsert，新旧两张圈选表共用。

        ``owner_attr`` 为归属列的 ORM 属性（SessionKolSelection.session_id 或
        KolSelectionItem.selection_set_id），其 ``.key`` 同时作为构造 kwarg 名。
        """
        platform = item.platform[:32]
        kol_uid = item.platform_account_id[:128]
        existing = await self._db.scalar(
            select(model).where(
                owner_attr == owner_id,
                model.platform == platform,
                model.kol_uid == kol_uid,
            )
        )
        if existing is None and _present(item.nickname):
            # 二次归并：派生 uid（{platform}:{24位hex}）只是无 用户ID 证据的占位
            # 身份，同一达人先后以派生/真实 uid 入场时不得重复建行。
            # - 新证据是派生 uid：并入同 nickname 的已有行（任意 uid）；
            # - 新证据是真实 uid：并入同 nickname 的派生占位行并把行重挂到
            #   真实 uid（同 nickname 的真实 uid 行不并，避免误并不同达人）。
            # nickname 为空不做二次归并。
            sibling = await self._db.scalar(
                select(model)
                .where(
                    owner_attr == owner_id,
                    model.platform == platform,
                    model.nickname == str(item.nickname)[:200],
                )
                .order_by(model.created_at)
                .limit(1)
            )
            if sibling is not None:
                new_uid_is_derived = _DERIVED_UID_PATTERN.fullmatch(kol_uid) is not None
                if new_uid_is_derived:
                    existing = sibling
                elif _DERIVED_UID_PATTERN.fullmatch(sibling.kol_uid) is not None:
                    # 真实 uid 行此时尚不存在（上方按 uid 查询未命中），重挂不撞唯一约束。
                    sibling.kol_uid = kol_uid
                    existing = sibling
        now = _utcnow()
        if existing is None:
            fields = item.as_dict()
            row = model(
                id=str(uuid4()),
                user_id=user_id,
                platform=platform,
                kol_uid=kol_uid,
                source_tool=tool_name,
                first_task_id=task_id,
                last_task_id=task_id,
                created_at=now,
                updated_at=now,
                **{owner_attr.key: owner_id},
            )
            self._apply_fields(row, fields)
            self._db.add(row)
            return
        fields = _merge_fields(existing.fields_json or {}, item.as_dict())
        existing.last_task_id = task_id
        existing.updated_at = now
        self._apply_fields(existing, fields)

    def _apply_fields(
        self, row: SessionKolSelection | KolSelectionItem, fields: dict[str, Any]
    ) -> None:
        export_fields = fields.get("export_fields") or {}
        city = export_fields.get("city")
        row.nickname = str(fields.get("nickname") or "")[:200]
        row.followers = fields.get("followers")
        row.city = str(city)[:64] if _present(city) else None
        profile_url = fields.get("normalized_profile_url")
        row.profile_url = str(profile_url)[:512] if _present(profile_url) else None
        row.fields_json = fields
        row.score_json = _score_payload(fields)
