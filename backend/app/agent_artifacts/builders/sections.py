"""brand/campaign 共用的章节确定性装配（B2）。

两个 builder 的 ``sentiment`` 与 ``top_posts`` 章节口径完全一致，在此共用：

- 情感：行级极性映射（``polarity``，阈值与 normalize_sentiment 工具一致），
  每行计数取声量值、缺失按 1 计；summary/by_platform 的 count/share 全部由
  代码计算，share 保留 4 位；合计行（全部/合计/总计，或无平台键但携带声量
  的汇总行）与具名平台行并存时只保留具名行防双计（与 overview 的 named
  优先口径一致），仅有合计行时归入 ``all`` 平台；
- 热帖：剔除显式标注非品牌相关的行；缺 post_id 或发布时间的行无法构成合法
  TopPost（必填字段），跳过并计数披露；互动量优先取互动字段，缺失时按
  赞/评/转求和；按互动量降序（缺失排后），同量按平台序 + post_id 保证
  确定性；链接只保留 http(s)，缺失披露不伪造。

所有数值都在 ``LineageCollector`` 登记贡献行，供字段级 lineage 输出。
"""

from __future__ import annotations

from typing import Any

from app.agent_artifacts.builders.common import LineageCollector
from app.agent_artifacts.builders.raw_rows import (
    AGGREGATE_PLATFORM_NAMES,
    AUTHOR_KEYS,
    COMMENT_KEYS,
    ENGAGEMENT_KEYS,
    LIKE_KEYS,
    PLATFORM_KEYS,
    POST_DATE_KEYS,
    POST_ID_KEYS,
    RELEVANCE_KEYS,
    SENTIMENT_KEYS,
    SHARE_KEYS,
    TITLE_KEYS,
    URL_KEYS,
    VOLUME_KEYS,
    RowRef,
    canon_platform,
    first,
    has_any,
    num,
    parse_datetime,
    platform_sort_key,
    polarity,
    text,
    valid_url,
    whole,
)

_POLARITIES = ("positive", "neutral", "negative")
_NEGATIVE_RELEVANCE_FLAGS = ("否", "no", "false")


def _int_if_integral(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


def _is_aggregate_sentiment_row(row: dict[str, Any]) -> bool:
    """合计行判定（防双计，与 overview 的 named 优先口径一致）。

    显式合计/全部平台行恒为合计行；无平台键但携带声量值的行也是合计行
    （DataTap 跨平台汇总行常省略平台字段）。帖子级明细行按 1 计数、不带
    声量字段，不会被误判为合计行。
    """
    raw_platform = first(row, PLATFORM_KEYS)
    if canon_platform(raw_platform) not in AGGREGATE_PLATFORM_NAMES:
        return False
    if raw_platform is not None:
        return True
    return num(first(row, VOLUME_KEYS)) is not None


def build_sentiment_section(
    rows: list[RowRef], collector: LineageCollector, *, path: str = "/data/sentiment"
) -> tuple[dict[str, Any], bool]:
    """情感章节：``{summary: {positive/neutral/negative: {count, share}}, by_platform: []}``。

    返回 ``(section, has_rows)``；无行情感时 summary 三桶 count/share 全 None
    （由调用方按 unavailable 披露）。
    """
    sentiment_rows = [
        ref for ref in rows if polarity(first(ref.row, SENTIMENT_KEYS)) is not None
    ]
    named_rows = [ref for ref in sentiment_rows if not _is_aggregate_sentiment_row(ref.row)]
    # 合计行与具名平台行并存时只用具名行——合计行是同一批数据的汇总，一并计入
    # 会让 summary 双计（真实 UAT 回归）；仅有合计行时归入 all 平台兜底。
    chosen = named_rows if named_rows else sentiment_rows

    bucket_rows: dict[str, list[RowRef]] = {name: [] for name in _POLARITIES}
    bucket_counts: dict[str, float] = {name: 0.0 for name in _POLARITIES}
    platform_rows: dict[str, dict[str, list[RowRef]]] = {}
    platform_counts: dict[str, dict[str, float]] = {}

    for ref in chosen:
        label = polarity(first(ref.row, SENTIMENT_KEYS))
        if label is None:
            continue
        count = num(first(ref.row, VOLUME_KEYS))
        contribution = count if count is not None else 1.0
        bucket_rows[label].append(ref)
        bucket_counts[label] += contribution
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        platform_rows.setdefault(platform, {name: [] for name in _POLARITIES})[label].append(ref)
        platform_counts.setdefault(platform, {name: 0.0 for name in _POLARITIES})
        platform_counts[platform][label] += contribution

    has_rows = any(bucket_rows[name] for name in _POLARITIES)
    if not has_rows:
        empty = {name: {"count": None, "share": None} for name in _POLARITIES}
        return {"summary": empty, "by_platform": []}, False

    total = sum(bucket_counts.values())
    summary: dict[str, Any] = {}
    for name in _POLARITIES:
        count = bucket_counts[name]
        summary[name] = {
            "count": _int_if_integral(count),
            "share": round(count / total, 4) if total else None,
        }
        sources = bucket_rows[name] or sentiment_rows
        collector.add(f"{path}/summary/{name}/count", sources)
        collector.add(f"{path}/summary/{name}/share", sources)

    by_platform: list[dict[str, Any]] = []
    for platform in sorted(platform_rows, key=platform_sort_key):
        platform_total = sum(platform_counts[platform].values())
        entry: dict[str, Any] = {"platform": platform}
        index = len(by_platform)
        platform_sentiment_rows = [
            ref for name in _POLARITIES for ref in platform_rows[platform][name]
        ]
        for name in _POLARITIES:
            count = platform_counts[platform][name]
            entry[name] = {
                "count": _int_if_integral(count),
                "share": round(count / platform_total, 4) if platform_total else None,
            }
            sources = platform_rows[platform][name] or platform_sentiment_rows
            collector.add(f"{path}/by_platform/{index}/{name}/count", sources)
            collector.add(f"{path}/by_platform/{index}/{name}/share", sources)
        by_platform.append(entry)

    return {"summary": summary, "by_platform": by_platform}, True


def build_top_posts(
    rows: list[RowRef],
    collector: LineageCollector,
    *,
    limit: int = 20,
    path: str = "/data/top_posts",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """热帖章节：返回 ``(items, meta)``；meta 记录跳过与缺失字段供披露。"""
    candidates: list[dict[str, Any]] = []
    skipped = 0
    missing_platform = 0
    for ref in rows:
        row = ref.row
        if not has_any(row, POST_ID_KEYS + TITLE_KEYS + URL_KEYS):
            continue
        flag = first(row, RELEVANCE_KEYS)
        if flag is not None and str(flag).strip().casefold() in _NEGATIVE_RELEVANCE_FLAGS:
            continue  # 显式标注非品牌相关：剔除
        post_id = text(first(row, POST_ID_KEYS))
        published = parse_datetime(first(row, POST_DATE_KEYS))
        platform = text(first(row, PLATFORM_KEYS))
        if post_id is None or published is None:
            skipped += 1
            continue
        if platform is None:
            missing_platform += 1
            continue
        likes = whole(first(row, LIKE_KEYS))
        comments = whole(first(row, COMMENT_KEYS))
        shares = whole(first(row, SHARE_KEYS))
        engagement = whole(first(row, ENGAGEMENT_KEYS))
        if engagement is None:
            parts = [value for value in (likes, comments, shares) if value is not None]
            engagement = sum(parts) if parts else None
        candidates.append(
            {
                "platform": canon_platform(platform),
                "post_id": post_id,
                "title": text(first(row, TITLE_KEYS)),
                "url": valid_url(first(row, URL_KEYS)),
                "author": text(first(row, AUTHOR_KEYS)),
                "published_at": published,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "engagement": engagement,
                "_ref": ref,
            }
        )

    # 互动量降序（缺失排后）；同量按平台序 + post_id 保证确定性。
    candidates.sort(
        key=lambda item: (
            item["engagement"] is None,
            -(item["engagement"] or 0),
            platform_sort_key(item["platform"]),
            item["post_id"],
        )
    )
    # 稳定业务键 (platform, post_id) 去重：保留互动量最高的一条。
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for item in candidates:
        key = (item["platform"], item["post_id"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    items = items[:limit]

    missing_url = 0
    missing_title = 0
    missing_author = 0
    output: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        ref = item.pop("_ref")
        if item["url"] is None:
            missing_url += 1
        if item["title"] is None:
            missing_title += 1
        if item["author"] is None:
            missing_author += 1
        # 全部叶子字段（含文本）都登记贡献行，canonical 发布时才能拿到证据。
        for field_name in ("platform", "post_id", "title", "url", "author", "published_at"):
            if item[field_name] is not None:
                collector.add(f"{path}/{index}/{field_name}", [ref])
        for field_name in ("likes", "comments", "shares", "engagement"):
            if item[field_name] is not None:
                collector.add(f"{path}/{index}/{field_name}", [ref])
        output.append(item)

    return output, {
        "skipped": skipped,
        "missing_platform": missing_platform,
        "missing_url": missing_url,
        "missing_title": missing_title,
        "missing_author": missing_author,
    }


__all__ = ["build_sentiment_section", "build_top_posts"]
