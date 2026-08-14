"""受控成功案例加载器测试（Gate B：品牌分析成功案例，只读代码资产）。

验证：curated exemplar 参数化（{{brand}} 占位符）且无来源品牌/日期/真实
汇总值；注入投影删除 source/forbidden_copy_values；purpose 不匹配返回空；
加载失败降级为空列表不阻塞。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.agent_runtime.exemplar_loader import _CuratedExemplar, load_curated_exemplars


def test_curated_brand_exemplar_is_parameterized_and_compact() -> None:
    exemplar = load_curated_exemplars(purpose="agent_loop", limit=1)[0]
    serialized = json.dumps(exemplar, ensure_ascii=False)
    assert exemplar["exemplar_id"] == "brand_analysis_success_v1"
    assert "{{brand}}" in serialized
    assert "瑞幸咖啡" not in serialized
    assert "2026-07-01" not in serialized
    assert "2026-07-31" not in serialized
    assert len(serialized) <= 6000


def test_curated_exemplar_projection_excludes_source_and_forbidden_values() -> None:
    exemplar = load_curated_exemplars(purpose="agent_loop", limit=1)[0]
    assert "source" not in exemplar
    assert "forbidden_copy_values" not in exemplar
    for key in (
        "applicable_when",
        "parameters",
        "successful_strategy",
        "decision_rules",
        "coverage_targets",
        "completion_contract",
    ):
        assert key in exemplar


def test_curated_exemplar_kind_is_marked() -> None:
    exemplar = load_curated_exemplars(purpose="agent_loop", limit=1)[0]
    assert exemplar["kind"] == "curated_strategy"


def test_curated_exemplar_unmatched_purpose_is_empty() -> None:
    assert load_curated_exemplars(purpose="artifact_reviewer", limit=1) == []


def test_curated_exemplar_loader_failure_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """加载失败只降级为空，不抛异常（动态案例已由 memory 兜底）。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("disk read failed")

    monkeypatch.setattr(
        "app.agent_runtime.exemplar_loader._read_curated_exemplar", _boom
    )
    assert load_curated_exemplars(purpose="agent_loop", limit=1) == []


# ---------------------------------------------------------------------------
# P2: Exemplar 契约版本锁定（Literal[1] + completion_contract 嵌套 Schema）
# ---------------------------------------------------------------------------


def _valid_exemplar_payload() -> dict:
    """完全合法的 exemplar payload fixture（每个用例只改一个待验证字段）。"""
    return {
        "exemplar_id": "brand_analysis_success_v1",
        "version": 1,
        "purpose": "agent_loop",
        "domain": "brand",
        "language": "zh-CN",
        "applicable_when": ["品牌分析"],
        "parameters": {},
        "objective": "分析品牌声量",
        "successful_strategy": [
            {
                "stage": "s1",
                "goal": "g1",
                "preferred_capability": "p1",
                "success_signal": "正常返回体积",
                "fallback": "f1",
            }
        ],
        "decision_rules": ["rule1"],
        "coverage_targets": ["coverage1"],
        "completion_contract": {
            "no_current_period_evidence": "a",
            "core_evidence_with_missing_sections": "b",
            "all_required_checks_pass": "c",
            "final_outputs": ["最终报告"],
        },
        "forbidden_copy_values": [],
    }


def test_p2_valid_payload_passes() -> None:
    """完全合法 payload 必须通过校验。"""
    _CuratedExemplar.model_validate(_valid_exemplar_payload())


def test_p2_rejects_version_2() -> None:
    payload = _valid_exemplar_payload()
    payload["version"] = 2
    with pytest.raises(ValidationError) as exc_info:
        _CuratedExemplar.model_validate(payload)
    assert ("version",) in [error["loc"] for error in exc_info.value.errors()]


def test_p2_rejects_string_version() -> None:
    payload = _valid_exemplar_payload()
    payload["version"] = "1"
    with pytest.raises(ValidationError) as exc_info:
        _CuratedExemplar.model_validate(payload)
    assert ("version",) in [error["loc"] for error in exc_info.value.errors()]


def test_p2_rejects_completion_contract_missing_field() -> None:
    payload = _valid_exemplar_payload()
    del payload["completion_contract"]["final_outputs"]
    with pytest.raises(ValidationError) as exc_info:
        _CuratedExemplar.model_validate(payload)
    locs = [error["loc"] for error in exc_info.value.errors()]
    assert ("completion_contract", "final_outputs") in locs


def test_p2_rejects_completion_contract_extra_field() -> None:
    payload = _valid_exemplar_payload()
    payload["completion_contract"]["unknown_field"] = "x"
    with pytest.raises(ValidationError) as exc_info:
        _CuratedExemplar.model_validate(payload)
    locs = [error["loc"] for error in exc_info.value.errors()]
    assert any(loc and loc[0] == "completion_contract" for loc in locs)


def test_p2_rejects_strategy_extra_field() -> None:
    payload = _valid_exemplar_payload()
    payload["successful_strategy"][0]["extra_col"] = 1
    with pytest.raises(ValidationError) as exc_info:
        _CuratedExemplar.model_validate(payload)
    locs = [error["loc"] for error in exc_info.value.errors()]
    assert any(loc and loc[0] == "successful_strategy" for loc in locs)
