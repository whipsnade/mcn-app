"""受控成功案例加载器测试（Gate B：品牌分析成功案例，只读代码资产）。

验证：curated exemplar 参数化（{{brand}} 占位符）且无来源品牌/日期/真实
汇总值；注入投影删除 source/forbidden_copy_values；purpose 不匹配返回空；
加载失败降级为空列表不阻塞。
"""

from __future__ import annotations

import json

import pytest

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


def test_p2_rejects_wrong_version() -> None:
    with pytest.raises(Exception):
        _CuratedExemplar.model_validate(
            {"exemplar_id": "x", "version": 2, "purpose": "agent_loop",
             "domain": "brand", "language": "zh-CN", "applicable_when": ["x"],
             "parameters": {}, "objective": "x",
             "successful_strategy": [], "decision_rules": ["x"],
             "coverage_targets": ["x"],
             "completion_contract": {"no_current_period_evidence": "a",
                                     "core_evidence_with_missing_sections": "b",
                                     "all_required_checks_pass": "c",
                                     "final_outputs": ["d"]}}
        )


def test_p2_rejects_string_version() -> None:
    with pytest.raises(Exception):
        _CuratedExemplar.model_validate(
            {"exemplar_id": "x", "version": "1", "purpose": "agent_loop",
             "domain": "brand", "language": "zh-CN", "applicable_when": ["x"],
             "parameters": {}, "objective": "x",
             "successful_strategy": [], "decision_rules": ["x"],
             "coverage_targets": ["x"],
             "completion_contract": {"no_current_period_evidence": "a",
                                     "core_evidence_with_missing_sections": "b",
                                     "all_required_checks_pass": "c",
                                     "final_outputs": ["d"]}}
        )


def test_p2_rejects_unknown_nested_field() -> None:
    with pytest.raises(Exception):
        _CuratedExemplar.model_validate(
            {"exemplar_id": "x", "version": 1, "purpose": "agent_loop",
             "domain": "brand", "language": "zh-CN", "applicable_when": ["x"],
             "parameters": {}, "objective": "x",
             "successful_strategy": [{"stage": "s", "goal": "g", "preferred_capability": "p",
                                      "success_signal": "ok", "fallback": "f", "extra_col": 1}],
             "decision_rules": ["x"], "coverage_targets": ["x"],
             "completion_contract": {"no_current_period_evidence": "a",
                                     "core_evidence_with_missing_sections": "b",
                                     "all_required_checks_pass": "c",
                                     "final_outputs": ["d"]}}
        )


def test_p2_rejects_wrong_contract_type() -> None:
    with pytest.raises(Exception):
        _CuratedExemplar.model_validate(
            {"exemplar_id": "x", "version": 1, "purpose": "agent_loop",
             "domain": "brand", "language": "zh-CN", "applicable_when": ["x"],
             "parameters": {}, "objective": "x",
             "successful_strategy": [], "decision_rules": ["x"],
             "coverage_targets": ["x"], "completion_contract": {"bad": "shape"}}
        )
