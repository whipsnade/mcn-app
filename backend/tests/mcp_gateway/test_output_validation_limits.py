from app.mcp_gateway.validation import validate_output


DATATAP_STRING_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}


def test_output_accepts_bounded_datatap_string_result_larger_than_input_limit() -> None:
    payload = {"result": "达" * 17_156}

    assert validate_output(payload, DATATAP_STRING_RESULT_SCHEMA) is payload
