"""行业词 → 上游标准标签的解析器。

insight 系（爆贴/统计）走 match_best_tag（品类标签）；KOL 搜索走
kol_match_mentions_tag（品类提及）。标签匹配本身是计费 MCP 调用，解析结果
（含"未匹配到"）在进程内缓存 24h，避免每个快捷请求重复烧积分；匹配失败时
调用方兜底为 keyword/textContentWord。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.mcp_gateway.transport import JsonValue


CACHE_TTL_SECONDS = 24 * 3600

MATCH_BEST_TAG_TOOL = "datatap.insight.match.best.tag.v1"
KOL_MATCH_MENTIONS_TAG_TOOL = "datatap.social.grow.kol.match.mentions.tag.v1"


class QuickCallFailedError(RuntimeError):
    """快捷调用链任一环节失败；路由层映射为 502 QUICK_CALL_FAILED。

    定义在 tags.py（共享低层模块），service.py 经这里导入。
    """

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type

# match_best_tag 成功时返回纯文本：「已找到合适的标签: 伊利」。
_BEST_TAG_RE = re.compile(r"已找到合适的标签\s*[:：]\s*(?P<tag>[^:：]+?)\s*$")

# (purpose, industry) -> (expires_at_monotonic, tag | None)；None 表示已确认无匹配。
_cache: dict[tuple[str, str], tuple[float, str | None]] = {}

# call(internal_tool_name, arguments) -> 解析后的 DataTap 载荷（{result} 已解包）。
TagCaller = Callable[[str, dict[str, Any]], Awaitable[JsonValue]]


def clear_tag_cache() -> None:
    """清空进程内缓存（测试隔离用）。"""
    _cache.clear()


def _cached(key: tuple[str, str]) -> tuple[bool, str | None]:
    entry = _cache.get(key)
    if entry is None:
        return False, None
    expires_at, tag = entry
    if expires_at <= time.monotonic():
        _cache.pop(key, None)
        return False, None
    return True, tag


def parse_best_tag(payload: JsonValue) -> str | None:
    """从 match_best_tag 载荷中抽取标准标签名。"""
    if isinstance(payload, str):
        match = _BEST_TAG_RE.search(payload)
        if match:
            tag = match.group("tag").strip()
            return tag or None
    return None


# 行业词近义词表：在上游候选标签中挑选最相关者。上游首候选可能跑偏
# （真实案例："美食"的首候选是"品类提及--酒类--酒类--啤酒"）。
_INDUSTRY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "美食": ("美食", "食品", "餐饮", "食品饮料"),
}


def _tag_preference_key(tag: str, words: tuple[str, ...]) -> tuple[int, int, int]:
    """越小越优：命中近义词的词序、标签层级（"--" 段数越少越宽）。"""
    for index, word in enumerate(words):
        if word and word in tag:
            return (0, index, tag.count("--"))
    return (1, len(words), tag.count("--"))


def parse_mentions_tag(payload: JsonValue, industry: str = "") -> str | None:
    """从 kol_match_mentions_tag 载荷中挑选最合适的提及标签。

    优先选包含行业词或其近义词的候选（词序优先、层级宽者优先）；
    全部不命中时退回首个候选（保持旧行为）。
    """
    data: JsonValue = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    rows = data.get("标签匹配结果列表")
    if not isinstance(rows, list):
        return None
    candidates: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tags = row.get("标签集合")
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if isinstance(tag, str) and tag.strip():
                candidates.append(tag.strip())
    if not candidates:
        return None
    words = tuple(
        dict.fromkeys((industry, *_INDUSTRY_SYNONYMS.get(industry, ())))
    ) if industry else ()
    if not words:
        return candidates[0]
    best = min(candidates, key=lambda tag: _tag_preference_key(tag, words))
    if _tag_preference_key(best, words)[0] != 0:
        return candidates[0]
    return best


class IndustryTagResolver:
    """按行业词解析上游标准标签；解析失败（无匹配）返回 None 并缓存该结论。"""

    def __init__(self, call: TagCaller) -> None:
        self._call = call

    async def resolve_category_tag(self, industry: str) -> str | None:
        """insight 系品类标签（match_best_tag）。

        上游瞬时故障按"未匹配到"兜底（调用方转 keyword），但不写入缓存——
        避免把临时故障缓存成 24h 的"确认无匹配"。
        """
        key = ("category", industry)
        hit, tag = _cached(key)
        if hit:
            return tag
        try:
            payload = await self._call(
                MATCH_BEST_TAG_TOOL,
                {"tag_type": "品类标签", "tag_names": [industry]},
            )
        except QuickCallFailedError:
            return None
        tag = parse_best_tag(payload)
        _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, tag)
        return tag

    async def resolve_mentions_tag(self, industry: str, platform: str) -> str | None:
        """KOL 搜索的品类提及标签（kol_match_mentions_tag，mentionsTagType=2002）。

        上游瞬时故障同样兜底为 None 且不写缓存。
        """
        key = (f"mentions:{platform}", industry)
        hit, tag = _cached(key)
        if hit:
            return tag
        try:
            payload = await self._call(
                KOL_MATCH_MENTIONS_TAG_TOOL,
                {"platform": platform, "mentionsTagType": 2002, "keywords": [industry]},
            )
        except QuickCallFailedError:
            return None
        tag = parse_mentions_tag(payload, industry)
        _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, tag)
        return tag
