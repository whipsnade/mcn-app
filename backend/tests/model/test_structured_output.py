import pytest

from app.model.structured_output import (
    ThinkJsonStreamParser,
    extract_single_json_object,
    parse_non_stream_output,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('{"value":1}', '{"value":1}'),
        ('```json\n{"value":1}\n```', '{"value":1}'),
        ('说明文字 {"value":{"items":[1,2]}} 结束', '{"value":{"items":[1,2]}}'),
        (r'前缀 {"text":"包含 } 和 \" 引号"} 后缀', r'{"text":"包含 } 和 \" 引号"}'),
    ],
)
def test_extract_single_json_object(source: str, expected: str) -> None:
    assert extract_single_json_object(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "没有 JSON",
        '{"value":1',
        '{"value":1} {"value":2}',
        r'{"a":"未闭合}',
        r'{"a":"未闭合} {"b":2}',
    ],
)
def test_extract_single_json_object_rejects_missing_truncated_or_multiple(source: str) -> None:
    with pytest.raises(ValueError, match="structured_json_invalid"):
        extract_single_json_object(source)


def test_parser_handles_tags_split_across_chunks() -> None:
    parser = ThinkJsonStreamParser()
    deltas: list[str] = []
    for chunk in ("<th", "ink>正在", "分析</thi", "nk>{\"value\":1}"):
        deltas.extend(parser.feed_content(chunk))
    result = parser.finish()
    assert "".join(deltas) == "正在分析"
    assert result.thinking_text == "正在分析"
    assert result.json_text == '{"value":1}'


def test_parser_merges_reasoning_content_and_tagged_think() -> None:
    parser = ThinkJsonStreamParser()
    assert parser.feed_reasoning("先确认品牌") == ("先确认品牌",)
    assert parser.feed_content("<think>再确认平台</think>{\"value\":1}") == ("再确认平台",)
    result = parser.finish()
    assert result.thinking_text == "先确认品牌再确认平台"
    assert result.json_text == '{"value":1}'


def test_non_stream_parser_ignores_think_and_markdown() -> None:
    result = parse_non_stream_output(
        "<think>内部思考</think>\n```json\n{\"value\":1}\n```"
    )
    assert result.thinking_text == "内部思考"
    assert result.json_text == '{"value":1}'
