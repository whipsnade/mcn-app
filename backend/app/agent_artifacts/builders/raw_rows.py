"""Evidence 原始行归一（brand/campaign builder 共用，B2 / Task 3R 分层）。

Task 3R 分层：本模块只负责**内部 normalized source rows**——把 DataTap MCP 结果
的多种包装形态（``{"result": "<json str>"}`` / ``{"rows": [...]}`` / 裸 list /
裸 dict）提取为 ``(evidence 内路径, 行 dict)`` 序列，按 ``_CANONICAL_ROW_FIELDS``
统一字段别名，供 Builder 计算。对外发布的 :class:`CanonicalField` 与
``field_lineage`` 由 ``app.agent_artifacts.canonical`` 从最终业务 data 生成，
本模块不产出任何对外 canonical path。

口径移植自已删除的旧 ``reporting/brand_assembler.py``（git 历史 ``d54bc06^``）的
确定性归一部分；不包含任何模型调用。source_path 指向 Evidence raw payload 内的
实际行位置（RFC 6901），供字段级 lineage 引用。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, NamedTuple


class RowRef(NamedTuple):
    """一行可归因的原始记录：Evidence id + 行在 raw payload 内的 JSON Pointer + 行。

    ``base_path`` 是行作为「字段路径基准」的 JSON Pointer：绝大多数形态与
    ``source_path`` 相同（指向行本身）；单行 dict 兜底时 ``source_path`` 指向
    首个键（``min_length=1`` 约束），而字段基准必须是根 ``""``，否则
    ``base + 字段名`` 拼出的路径不可解析。``None`` 表示与 ``source_path`` 相同。
    """

    evidence_id: str
    source_path: str
    row: dict[str, Any]
    base_path: str | None = None

    @property
    def field_base(self) -> str:
        """拼字段级 source_path 的基准指针（见 ``join_source_path``）。"""
        return self.source_path if self.base_path is None else self.base_path


# ---------------------------------------------------------------------------
# 字段别名（防御式归一，与旧 assembler 一致）
# ---------------------------------------------------------------------------

PLATFORM_KEYS = ("平台", "platform", "媒体", "媒介", "datasource", "数据源")
VOLUME_KEYS = ("声量", "品牌声量", "品牌提及量", "brand_mentions", "volume", "mentions")
ENGAGEMENT_KEYS = ("互动数", "互动量", "互动", "interactions", "engagement")
POSTS_KEYS = ("发帖数", "帖子数", "笔记数", "posts", "notes")
POSITIVE_KEYS = ("正面声量数", "正面声量", "正面", "positive")
NEUTRAL_KEYS = ("中性声量数", "中性声量", "中性", "neutral")
NEGATIVE_KEYS = ("负面声量数", "负面声量", "负面", "negative")
DATE_KEYS = ("日期", "时间", "date", "published_at")
# Gate B：趋势时间键统一别名（含「日」「周」），brand builder 与
# NormalizationRegistry 共用同一常量，DataTap 返回日/周列时不丢数据。
TIME_KEYS = ("日期", "日", "周", "时间", "date", "published_at")
SENTIMENT_KEYS = ("情感", "内容情感", "情绪", "sentiment")
REGION_KEYS = ("地区", "省份", "地域", "region", "province")
REGION_MAP_KEYS = ("地域分布", "省份分布", "地区分布")
CONTENT_TYPE_KEYS = ("内容类型", "内容形式", "类型", "content_type")
TIER_KEYS = ("达人层级", "创作者层级", "粉丝层级", "creator_tier", "tier")
COMMERCIAL_KEYS = ("是否商单", "商业属性", "内容属性", "是否广告", "is_commercial")
TOPIC_KEYS = ("话题", "话题名称", "topic", "hashtag", "标签名称", "标签名", "tag")
POST_ID_KEYS = ("帖子ID", "帖子id", "笔记ID", "视频ID", "作品ID", "post_id", "aweme_id")
TITLE_KEYS = ("标题", "笔记标题", "内容", "title")
AUTHOR_KEYS = ("昵称", "作者", "用户昵称", "达人昵称", "author")
AUTHOR_ID_KEYS = ("用户ID", "用户id", "uid", "author_id", "kol_uid", "达人ID")
POST_DATE_KEYS = ("发布时间", "采集时间", "publish_time", "published_at", "collected_at")
LIKE_KEYS = ("点赞数", "点赞", "likes", "like_count")
COMMENT_KEYS = ("评论数", "评论", "comments", "comment_count")
COLLECT_KEYS = ("收藏数", "收藏", "collects", "collect_count")
SHARE_KEYS = ("分享数", "转发数", "转发", "分享", "shares", "share_count")
URL_KEYS = ("帖子链接", "原帖链接", "链接", "url")
RELEVANCE_KEYS = ("品牌相关", "是否相关", "is_brand_related")

# 合计/汇总平台行（归一后）：overview 聚合时存在具名平台行则跳过，防双计。
AGGREGATE_PLATFORM_NAMES = frozenset({"all", "全部", "合计", "总计"})

_PLATFORM_ALIASES = {
    "小红书": "xiaohongshu",
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "抖音": "douyin",
    "douyin": "douyin",
    # DataTap datasource「短视频」即抖音（如 hot_user 行的平台字段）。
    "短视频": "douyin",
    "微博": "weibo",
    "weibo": "weibo",
    "微信": "wechat",
    "wechat": "wechat",
    "b站": "bilibili",
    "哔哩哔哩": "bilibili",
    "bilibili": "bilibili",
}
_PLATFORM_ORDER = ("xiaohongshu", "douyin", "weibo", "wechat", "bilibili")

_POSITIVE_LABELS = frozenset(
    {"正面", "正", "积极", "正向", "好评", "满意", "positive", "pos", "up"}
)
_NEGATIVE_LABELS = frozenset(
    {"负面", "负", "消极", "差评", "不满意", "negative", "neg", "down"}
)
_NEUTRAL_LABELS = frozenset({"中性", "中", "一般", "neutral", "neu"})

_PAID_TERMS = ("商单", "广告", "是", "commercial", "paid", "true")
_ORGANIC_TERMS = ("自然", "否", "非商单", "organic", "false")

# 行容器键：dict 形态 Evidence 的行列表所在键（首个命中生效）。
_ROW_CONTAINER_KEYS = ("rows", "list", "items", "data", "posts", "records")


# ---------------------------------------------------------------------------
# 行提取
# ---------------------------------------------------------------------------


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def join_source_path(base: str, *tokens: str) -> str:
    """在行的基准 JSON Pointer 上追加字段 token（RFC 6901 转义）。

    - ``base=""``（根）：返回 ``/{token...}``；
    - ``base="/result"``（``{"result": "<json 字符串>"}`` 包装）：指针无法下钻
      到字符串内部，统一返回 ``"/result"`` 粗粒度引用（仍可解析、可审计）；
    - 其余（如 ``/KOL 列表/0``）：``base + "/" + 转义后的字段名``。
    """
    if base == "/result":
        return "/result"
    suffix = "/".join(_escape(token) for token in tokens)
    return f"{base}/{suffix}" if suffix else base


def unwrap_payload(raw_payload: Any) -> tuple[Any, str]:
    """解开 ``{"result": "<json 字符串>"}`` 包装；返回 (解析后的值, 基路径)。

    字符串无法解析为 JSON 时返回 ``(None, "/result")``——base 仍可用于
    lineage 粗粒度引用（指向整个字符串），但提取不到任何行。
    """
    if isinstance(raw_payload, dict):
        result = raw_payload.get("result")
        if isinstance(result, str):
            try:
                return json.loads(result), "/result"
            except (TypeError, ValueError):
                return None, "/result"
    return raw_payload, ""


def extract_rows(evidence_id: str, raw_payload: Any) -> list[RowRef]:
    """从 Evidence raw payload 提取行序列；每行携带可解析的 source_path。"""
    parsed, base = unwrap_payload(raw_payload)
    rows: list[RowRef] = []

    def _path(*tokens: str) -> str:
        if base == "/result":
            # result 是 JSON 字符串：指针无法下钻到字符串内部，粗粒度指向整个串。
            return "/result"
        parts = [token for token in (base, *tokens) if token]
        return "/" + "/".join(_escape(token) for token in parts)

    if isinstance(parsed, list):
        for index, item in enumerate(parsed):
            if isinstance(item, dict):
                rows.append(RowRef(evidence_id, _path(str(index)), item))
        return rows
    if isinstance(parsed, dict):
        if not parsed:
            return []
        for key in _ROW_CONTAINER_KEYS:
            value = parsed.get(key)
            if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        rows.append(RowRef(evidence_id, _path(key, str(index)), item))
                return rows
        # 已知容器键未命中：扫描首个「dict 列表」值兜底——真实 MCP 结果常用
        # 中文容器键（如 kol_xiaohongshu_search 的「KOL 列表」），整包不能
        # 退化成单行，否则下游按行读取的字段全部缺失。
        for key, value in parsed.items():
            if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        rows.append(RowRef(evidence_id, _path(key, str(index)), item))
                return rows
        # 单行 dict（如一次 overview 查询的聚合结果）：根指针 "" 非合法
        # source_path（min_length=1），退而指向首个键，仍可解析可审计；
        # 但字段路径基准必须是根 ""（base_path），否则 base+字段名 不可解析。
        if base == "/result":
            return [RowRef(evidence_id, "/result", parsed)]
        first_key = next(iter(parsed))
        return [RowRef(evidence_id, f"/{_escape(str(first_key))}", parsed, base_path="")]
    return []


# ---------------------------------------------------------------------------
# 值解析
# ---------------------------------------------------------------------------


def first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] is not None and row[key] != "":
            return row[key]
    return None


def has_any(row: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return first(row, keys) is not None


def text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def num(value: Any) -> float | None:
    """非负有限数值解析；支持 万/亿 中文单位与千分位字符串。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        rendered = value.strip().replace(",", "").replace(" ", "")
        multiplier = 1.0
        if rendered.endswith("万"):
            multiplier, rendered = 1e4, rendered[:-1]
        elif rendered.endswith("亿"):
            multiplier, rendered = 1e8, rendered[:-1]
        if not rendered:
            return None
        try:
            number = float(rendered) * multiplier
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def whole(value: Any) -> int | None:
    """数值 → int（可整除的 float 取整）；非数值返回 None。"""
    number = num(value)
    if number is None:
        return None
    return int(number)


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    rendered = (
        value.strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    )
    rendered = rendered[:10]
    try:
        return date.fromisoformat(rendered)
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    rendered = value.strip().replace("/", "-").replace("年", "-").replace("月", "-")
    rendered = rendered.replace("日", "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(rendered, fmt)
        except ValueError:
            continue
    return None


def canon_platform(value: Any) -> str:
    rendered = str(value or "").strip().casefold()
    if not rendered:
        return "all"
    for alias, platform in _PLATFORM_ALIASES.items():
        if alias in rendered:
            return platform
    return rendered


def platform_sort_key(platform: Any) -> tuple[int, str]:
    name = str(platform)
    try:
        return (_PLATFORM_ORDER.index(name), name)
    except ValueError:
        return (len(_PLATFORM_ORDER), name)


def platform_coverage_incomplete(
    scope_platforms: tuple[str, ...], rows: list[RowRef]
) -> bool:
    """scope 声明了平台但 Evidence 未覆盖全部平台时返回 True（部分覆盖）。

    合计行（全部/合计/总计/all）代表全部平台汇总，出现即视为覆盖完整；
    无具名平台行时同理（DataTap 跨平台汇总行常省略平台字段）。
    """
    if not scope_platforms:
        return False
    covered: set[str] = set()
    for ref in rows:
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        if platform in AGGREGATE_PLATFORM_NAMES:
            return False
        covered.add(platform)
    return not set(scope_platforms).issubset(covered)


def valid_url(value: Any) -> str | None:
    """仅接受 http(s) 绝对 URL；其余一律 None（缺失披露，不伪造链接）。"""
    rendered = text(value)
    if rendered is None:
        return None
    if rendered.startswith("http://") or rendered.startswith("https://"):
        return rendered
    return None


def polarity(value: Any) -> str | None:
    """情感值 → positive/neutral/negative；阈值与 normalize_sentiment 工具一致。

    数值：[-1, 1] 区间 >0.2 正面 / <-0.2 负面；[0, 100] 区间 >60 正面 / <40 负面。
    文本：已知标签直取，未知标签保守归中性。无法解析返回 None（该行不进情感统计）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if -1 <= number <= 1:
            if number > 0.2:
                return "positive"
            if number < -0.2:
                return "negative"
            return "neutral"
        if 0 <= number <= 100:
            if number > 60:
                return "positive"
            if number < 40:
                return "negative"
            return "neutral"
        return None
    if isinstance(value, str):
        import unicodedata

        key = unicodedata.normalize("NFKC", value.strip()).casefold()
        if not key:
            return None
        if key in _POSITIVE_LABELS:
            return "positive"
        if key in _NEGATIVE_LABELS:
            return "negative"
        # 未知标签保守归为中性（与 normalize_sentiment 一致）。
        return "neutral"
    return None


def commercial_kind(value: Any) -> str | None:
    """是否商单 → organic/paid；无法识别返回 None。"""
    rendered = text(value)
    if rendered is None:
        return None
    lowered = rendered.casefold()
    if any(term in lowered for term in _ORGANIC_TERMS):
        return "organic"
    if any(term in lowered for term in _PAID_TERMS):
        return "paid"
    return None


def sentiment_score(positive: float | None, neutral: float | None, negative: float | None) -> float | None:
    """净情感指数：(正面 - 负面) / 总量 * 100，保留 2 位；无构成返回 None。"""
    known = [value for value in (positive, neutral, negative) if value is not None]
    if not known:
        return None
    total = sum(known)
    if not total:
        return None
    return round((((positive or 0.0) - (negative or 0.0)) / total) * 100, 2)


# ---------------------------------------------------------------------------
# Task 3R：内部 normalized source rows（唯一原始 payload 消费入口）
#
# 只产出供 Builder 计算用的规范化行（canonical source key + 原始值 + RowRef），
# 不产出对外 canonical path。原始行去重键为 (evidence_id, source_path,
# canonical source name)：同一 Evidence 行被多个 group 引用时只归一化一次。
# ---------------------------------------------------------------------------

_CANONICAL_ROW_FIELDS: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("platform", PLATFORM_KEYS, None),
    ("volume", VOLUME_KEYS, "mentions"),
    ("engagement", ENGAGEMENT_KEYS, "interactions"),
    ("posts", POSTS_KEYS, "posts"),
    ("positive", POSITIVE_KEYS, "mentions"),
    ("neutral", NEUTRAL_KEYS, "mentions"),
    ("negative", NEGATIVE_KEYS, "mentions"),
    ("date", TIME_KEYS, "timestamp"),
    ("sentiment", SENTIMENT_KEYS, None),
    ("region", REGION_KEYS, None),
    ("region_map", REGION_MAP_KEYS, None),
    ("content_type", CONTENT_TYPE_KEYS, None),
    ("tier", TIER_KEYS, None),
    ("is_commercial", COMMERCIAL_KEYS, None),
    ("topic", TOPIC_KEYS, None),
    ("post_id", POST_ID_KEYS, None),
    ("title", TITLE_KEYS, None),
    ("author", AUTHOR_KEYS, None),
    ("author_id", AUTHOR_ID_KEYS, None),
    ("published_at", POST_DATE_KEYS, "timestamp"),
    ("likes", LIKE_KEYS, "count"),
    ("comments", COMMENT_KEYS, "count"),
    ("collects", COLLECT_KEYS, "count"),
    ("shares", SHARE_KEYS, "count"),
    ("url", URL_KEYS, None),
    ("is_brand_related", RELEVANCE_KEYS, None),
    ("spend", ("投放金额", "花费", "消耗", "成本", "spend", "cost"), "currency"),
    ("impressions", ("曝光", "曝光数", "展示", "impressions", "views"), "impressions"),
    ("conversions", ("转化", "转化数", "转化量", "成交数", "conversions", "conversion"), "count"),
    ("revenue", ("销售额", "销售金额", "收入", "GMV", "revenue", "sales"), "currency"),
    ("is_paid", ("是否付费",), None),
    ("attribution", ("归属", "投放类型", "付费/自然", "attribution"), None),
)


@dataclass(frozen=True)
class CanonicalEvidence:
    """内部 normalized source rows：按 group 提供规范化行（只供 Builder 计算）。"""

    rows_by_group: Mapping[str, tuple[RowRef, ...]]

    def rows(self, group: str) -> list[RowRef]:
        return list(self.rows_by_group.get(group, ()))


def canonicalize_marketing_evidence(
    evidence: Mapping[str, list[tuple[str, Any]]]
) -> CanonicalEvidence:
    """从原始 Evidence shape 提取内部规范化行，给 Builder 提供标准化 rows。

    Builder 此后只读取返回的 rows；原始 payload 只在本函数内解包和字段别名匹配。
    同一 ``(evidence_id, source_path)`` 在 group 内重复（或跨 group 重复）只保留
    一行，防双计；对外 canonical 字段由 ``canonical.publish_canonical`` 从最终
    data 生成，本层不产出 canonical path。
    """
    rows_by_group: dict[str, tuple[RowRef, ...]] = {}
    for group, pairs in evidence.items():
        normalized_rows: list[RowRef] = []
        seen_sources: set[tuple[str, str]] = set()
        for evidence_id, raw_payload in pairs:
            for ref in extract_rows(evidence_id, raw_payload):
                key = (ref.evidence_id, ref.source_path)
                if key in seen_sources:
                    continue  # (evidence_id, source_path) 行级去重
                normalized: dict[str, Any] = {}
                for canonical_name, aliases, _unit in _CANONICAL_ROW_FIELDS:
                    source_key = next(
                        (key for key in aliases if key in ref.row and ref.row[key] not in (None, "")),
                        None,
                    )
                    if source_key is None:
                        continue
                    normalized[canonical_name] = ref.row[source_key]
                if normalized:
                    seen_sources.add(key)
                    normalized_rows.append(
                        RowRef(ref.evidence_id, ref.source_path, normalized, ref.base_path)
                    )
        rows_by_group[group] = tuple(normalized_rows)
    return CanonicalEvidence(rows_by_group=rows_by_group)


__all__ = [
    "AGGREGATE_PLATFORM_NAMES",
    "AUTHOR_ID_KEYS",
    "AUTHOR_KEYS",
    "COLLECT_KEYS",
    "COMMENT_KEYS",
    "COMMERCIAL_KEYS",
    "CONTENT_TYPE_KEYS",
    "DATE_KEYS",
    "ENGAGEMENT_KEYS",
    "LIKE_KEYS",
    "NEGATIVE_KEYS",
    "NEUTRAL_KEYS",
    "PLATFORM_KEYS",
    "POSITIVE_KEYS",
    "POSTS_KEYS",
    "POST_DATE_KEYS",
    "POST_ID_KEYS",
    "REGION_KEYS",
    "REGION_MAP_KEYS",
    "RELEVANCE_KEYS",
    "SENTIMENT_KEYS",
    "SHARE_KEYS",
    "TIER_KEYS",
    "TIME_KEYS",
    "TITLE_KEYS",
    "TOPIC_KEYS",
    "URL_KEYS",
    "VOLUME_KEYS",
    "CanonicalEvidence",
    "RowRef",
    "canon_platform",
    "canonicalize_marketing_evidence",
    "commercial_kind",
    "extract_rows",
    "first",
    "has_any",
    "join_source_path",
    "num",
    "parse_date",
    "parse_datetime",
    "platform_coverage_incomplete",
    "platform_sort_key",
    "polarity",
    "sentiment_score",
    "text",
    "unwrap_payload",
    "valid_url",
    "whole",
]
