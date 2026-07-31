import pytest
from pydantic import BaseModel

from app.model.structured_output import (
    ThinkJsonStreamParser,
    extract_single_json_object,
    parse_non_stream_output,
    validate_with_repair,
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


class _Decision(BaseModel):
    action: str
    rationale: str | None = None


def test_validate_with_repair_passes_valid_json_unchanged() -> None:
    result = validate_with_repair(_Decision, '{"action":"finish","rationale":"够了"}')
    assert result.action == "finish"
    assert result.rationale == "够了"


def test_validate_with_repair_fixes_unescaped_quotes() -> None:
    # 还原 2026-07-30 UAT 失败样例：字符串内含未转义双引号
    broken = (
        '{"action":"call_tool",'
        '"rationale":"evidence_gaps中包含"受众"缺口，补充受众维度分析。"}'
    )
    result = validate_with_repair(_Decision, broken)
    assert result.action == "call_tool"
    assert result.rationale is not None and "受众" in result.rationale


def test_validate_with_repair_raises_original_error_when_unfixable() -> None:
    with pytest.raises(ValueError):
        validate_with_repair(_Decision, '{"rationale":"缺少必填字段"}')
