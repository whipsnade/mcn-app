import pytest

from app.quick.tags import (
    IndustryTagResolver,
    parse_best_tag,
    parse_mentions_tag,
)


def test_parse_best_tag_reads_success_text() -> None:
    assert parse_best_tag("已找到合适的标签: 伊利") == "伊利"
    assert parse_best_tag("已找到合适的标签：运动户外") == "运动户外"


def test_parse_best_tag_returns_none_on_failure_or_garbage() -> None:
    assert parse_best_tag("未找到匹配的标签，请换个关键词") is None
    assert parse_best_tag({"标签": "美食"}) is None
    assert parse_best_tag(None) is None
    assert parse_best_tag("") is None


def test_parse_mentions_tag_reads_first_tag() -> None:
    payload = {
        "标签匹配结果列表": [
            {"关键词": "餐饮", "标签集合": ["品类提及--运动户外--户外装备--户外餐饮用品"]}
        ]
    }
    assert parse_mentions_tag(payload) == "品类提及--运动户外--户外装备--户外餐饮用品"


def test_parse_mentions_tag_accepts_json_string_and_rejects_empty() -> None:
    assert parse_mentions_tag('{"标签匹配结果列表": [{"标签集合": ["品牌提及--科颜氏"]}]}') == (
        "品牌提及--科颜氏"
    )
    assert parse_mentions_tag({"标签匹配结果列表": [{"标签集合": []}]}) is None
    assert parse_mentions_tag({"标签匹配结果列表": []}) is None
    assert parse_mentions_tag("not-json") is None
    assert parse_mentions_tag(None) is None


class _ScriptedCaller:
    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, internal_tool_name: str, arguments: dict):
        self.calls.append((internal_tool_name, arguments))
        return self.payloads.pop(0)


@pytest.mark.asyncio
async def test_category_tag_resolution_is_cached_within_ttl() -> None:
    caller = _ScriptedCaller(["已找到合适的标签: 美食"])
    resolver = IndustryTagResolver(caller)

    first = await resolver.resolve_category_tag("美食")
    second = await resolver.resolve_category_tag("美食")

    assert first == second == "美食"
    assert len(caller.calls) == 1
    assert caller.calls[0][1] == {"tag_type": "品类标签", "tag_names": ["美食"]}


@pytest.mark.asyncio
async def test_failed_category_resolution_is_cached_as_no_match() -> None:
    caller = _ScriptedCaller(["未找到匹配的标签"])
    resolver = IndustryTagResolver(caller)

    assert await resolver.resolve_category_tag("不存在行业") is None
    assert await resolver.resolve_category_tag("不存在行业") is None
    assert len(caller.calls) == 1


@pytest.mark.asyncio
async def test_mentions_tag_cache_key_includes_platform() -> None:
    caller = _ScriptedCaller(
        [
            {"标签匹配结果列表": [{"标签集合": ["品类提及--美食--A"]}]},
            {"标签匹配结果列表": [{"标签集合": ["品类提及--美食--B"]}]},
        ]
    )
    resolver = IndustryTagResolver(caller)

    assert await resolver.resolve_mentions_tag("美食", "xiaohongshu") == "品类提及--美食--A"
    assert await resolver.resolve_mentions_tag("美食", "douyin") == "品类提及--美食--B"
    assert await resolver.resolve_mentions_tag("美食", "xiaohongshu") == "品类提及--美食--A"
    assert len(caller.calls) == 2
    assert caller.calls[0][1] == {
        "platform": "xiaohongshu",
        "mentionsTagType": 2002,
        "keywords": ["美食"],
    }
