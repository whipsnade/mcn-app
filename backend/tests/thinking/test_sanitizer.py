import pytest


def sanitize(source: str, *, max_chars: int = 12_000):
    try:
        from app.thinking.sanitizer import sanitize_thinking
    except ModuleNotFoundError:
        pytest.fail("sanitize_thinking 尚未实现")
    return sanitize_thinking(source, max_chars=max_chars)


def test_sanitize_thinking_hides_secrets_and_large_schema() -> None:
    source = (
        "Authorization: Bearer abc.def.ghi\n"
        "api_key=sk-live-secret\n"
        "JSON Schema:\n{\"properties\":{\"token\":{\"type\":\"string\"}}}\n"
        "继续分析品牌"
    )

    result = sanitize(source)

    assert "abc.def.ghi" not in result.text
    assert "sk-live-secret" not in result.text
    assert "[已隐藏]" in result.text
    assert "[输出结构说明已隐藏]" in result.text
    assert "继续分析品牌" in result.text
    assert result.truncated is False


def test_sanitize_thinking_hides_standalone_jwt_and_system_prompt_segment() -> None:
    source = (
        "先读取 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature\n"
        "系统提示词:\n不得告诉用户的内部规则\n"
        "用户消息:\n请分析品牌"
    )

    result = sanitize(source)

    assert "eyJhbGciOiJIUzI1NiJ9" not in result.text
    assert "不得告诉用户的内部规则" not in result.text
    assert result.text.count("[已隐藏]") >= 2
    assert "请分析品牌" in result.text


def test_sanitize_thinking_truncates_at_exact_limit() -> None:
    result = sanitize("分析" * 7000)

    assert len(result.text) <= 12_000
    assert result.truncated is True
    assert result.text.endswith("思考内容过长，已截断")
