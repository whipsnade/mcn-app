"""scripts/b7_evidence 的单元测试：canonical 序列化、hash chain、flush/fsync、
DTO 语义校验、跨文件一致性、correction 帧。全部离线，不连数据库。"""

from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from scripts.b7_evidence import (
    TERMINAL_EVENT_TYPES,
    ZERO_HASH,
    AccountingPayload,
    EventOrderPayload,
    EvidenceWriter,
    RunIdentityPayload,
    ScenarioResultPayload,
    canonical_bytes,
    check_cross_file_consistency,
    event_order_summary,
    run_identity_from_orm,
    tool_call_from_orm,
    usage_record_from_orm,
)


def test_canonical_bytes_key_order_and_compact_separators() -> None:
    raw = canonical_bytes({"b": 1, "a": {"d": 2, "c": "瑞幸"}})
    assert raw == '{"a":{"c":"瑞幸","d":2},"b":1}'.encode("utf-8")


def test_canonical_bytes_non_ascii_not_escaped() -> None:
    assert "瑞幸".encode("utf-8") in canonical_bytes({"x": "瑞幸"})


def _writer(tmp_path) -> EvidenceWriter:
    return EvidenceWriter(tmp_path / "ev")


def test_append_chain_sequence_prev_and_record_hash(tmp_path) -> None:
    writer = _writer(tmp_path)
    f1 = writer.append("manifest.jsonl", scenario_id="round", type="round_opened", payload={"a": 1})
    f2 = writer.append("manifest.jsonl", scenario_id="L0", type="l0_check", payload={"b": 2})
    assert f1["sequence"] == 1 and f1["prev_hash"] == ZERO_HASH
    assert f2["sequence"] == 2 and f2["prev_hash"] == f1["record_hash"]
    body = {k: v for k, v in f2.items() if k != "record_hash"}
    assert f2["record_hash"] == hashlib.sha256(canonical_bytes(body)).hexdigest()
    assert writer.verify("manifest.jsonl") == []


def test_append_flushes_and_fsyncs_every_frame(tmp_path, monkeypatch) -> None:
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def counting(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting)
    writer = _writer(tmp_path)
    writer.append("manifest.jsonl", scenario_id="round", type="t", payload={"x": 1})
    writer.append("manifest.jsonl", scenario_id="round", type="t", payload={"x": 2})
    assert len(fsync_calls) == 2
    # flush 生效：另一个 fd 立即可读到最后一帧
    lines = (tmp_path / "ev" / "manifest.jsonl").read_bytes().strip().split(b"\n")
    assert len(lines) == 2


def test_verify_detects_tamper_delete_reorder_and_reformat(tmp_path) -> None:
    writer = _writer(tmp_path)
    writer.append("manifest.jsonl", scenario_id="round", type="t", payload={"x": 1})
    writer.append("manifest.jsonl", scenario_id="round", type="t", payload={"x": 2})
    writer.append("manifest.jsonl", scenario_id="round", type="t", payload={"x": 3})
    path = tmp_path / "ev" / "manifest.jsonl"
    original = path.read_bytes()

    # 篡改一个字节
    data = bytearray(original)
    data[10] ^= 0x01
    path.write_bytes(bytes(data))
    assert writer.verify("manifest.jsonl") != []

    # 删除中间一帧
    frames = original.strip().split(b"\n")
    path.write_bytes(b"\n".join([frames[0], frames[2]]) + b"\n")
    assert writer.verify("manifest.jsonl") != []

    # 重排帧序
    path.write_bytes(b"\n".join([frames[0], frames[2], frames[1]]) + b"\n")
    assert writer.verify("manifest.jsonl") != []

    # 非 canonical 重序列化（加空格）
    pretty = [json.dumps(json.loads(f), ensure_ascii=False).encode() for f in frames]
    path.write_bytes(b"\n".join(pretty) + b"\n")
    assert writer.verify("manifest.jsonl") != []

    # 尾部帧删除：逐帧自洽不可检，必须由外部锚点（链头）检出
    path.write_bytes(b"\n".join(frames[:2]) + b"\n")
    assert writer.verify("manifest.jsonl") == []
    head = json.loads(frames[2])["record_hash"]
    assert writer.verify("manifest.jsonl", expected_head=head) != []

    path.write_bytes(original)
    assert writer.verify("manifest.jsonl") == []
    assert writer.verify("manifest.jsonl", expected_head=head) == []


def test_unknown_file_rejected(tmp_path) -> None:
    writer = _writer(tmp_path)
    with pytest.raises(ValueError, match="unknown_jsonl_evidence_file"):
        writer.append("notes.jsonl", scenario_id="round", type="t", payload={})


def test_correction_frame_references_original_and_keeps_chain(tmp_path) -> None:
    writer = _writer(tmp_path)
    writer.append("scenario-results.jsonl", scenario_id="L1", type="scenario_result", payload={"v": "wrong"})
    corr = writer.correction(
        "scenario-results.jsonl",
        scenario_id="L1",
        corrects_sequence=1,
        note="fix typo",
        payload={"v": "right"},
    )
    assert corr["sequence"] == 2
    assert corr["type"] == "correction"
    assert corr["payload"]["corrects_sequence"] == 1
    lines = (tmp_path / "ev" / "scenario-results.jsonl").read_text().strip().split("\n")
    assert json.loads(lines[0])["payload"]["v"] == "wrong"  # 原帧保留
    assert writer.verify("scenario-results.jsonl") == []


def _orm_run(**overrides):
    base = {
        "id": "run-1",
        "session_id": "sess-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "runtime_backend": "pi",
        "runtime_config_version_id": "cfg-1",
        "status": "completed",
        "model": "deepseek-v4-pro",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_run_identity_from_orm_named_fields_and_snapshot_binding() -> None:
    identity = run_identity_from_orm(_orm_run(), expected_config_version_id="cfg-1", attempts=1)
    assert identity.snapshot_binding_ok is True
    assert identity.backend == "pi"
    assert identity.terminal == "completed"
    mismatched = run_identity_from_orm(_orm_run(), expected_config_version_id="cfg-other", attempts=1)
    assert mismatched.snapshot_binding_ok is False


def test_run_identity_pi_requires_config_version() -> None:
    with pytest.raises(ValidationError):
        run_identity_from_orm(
            _orm_run(runtime_config_version_id=None), expected_config_version_id=None, attempts=1
        )


def test_run_identity_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RunIdentityPayload(
            run_id="r",
            session_id="s",
            tenant_id="t",
            user_id="u",
            backend="pi",
            runtime_config_version_id="cfg",
            terminal="completed",
            snapshot_binding_ok=True,
            attempts=1,
            unexpected="nope",
        )


def test_scenario_error_code_must_not_be_model_name_or_free_text() -> None:
    with pytest.raises(ValidationError):
        ScenarioResultPayload(
            scenario="L1", verdict="FAIL", terminal="failed", error_code="deepseek-v4-pro"
        )
    ok = ScenarioResultPayload(
        scenario="L1", verdict="FAIL", terminal="failed", error_code="mcp_tool_identity_invalid"
    )
    assert ok.error_code == "mcp_tool_identity_invalid"


def test_terminal_set_is_closed_and_run_started_never_counted(tmp_path) -> None:
    events = [
        SimpleNamespace(sequence=1, event_type="run.started"),
        SimpleNamespace(sequence=2, event_type="message.completed"),
        SimpleNamespace(sequence=3, event_type="run.completed"),
    ]
    summary = event_order_summary("run-1", events)
    assert summary.terminal_count == 1
    assert summary.terminal_types == ["run.completed"]
    assert summary.message_completed_before_terminal is True
    assert "run.started" not in TERMINAL_EVENT_TYPES
    with pytest.raises(ValidationError):
        EventOrderPayload(
            run_id="r",
            sequence_monotonic=True,
            terminal_count=1,
            terminal_types=["run.started"],
            message_completed_count=0,
            message_completed_before_terminal=False,
        )


def test_accounting_payload_enforces_wallet_identity() -> None:
    with pytest.raises(ValidationError):
        AccountingPayload(
            run_id="r",
            net_points=15,
            confirmed_outbound=1,
            wallet_balance_after=1990,
            wallet_reserved_after=0,
        )
    ok = AccountingPayload(
        run_id="r",
        net_points=10,
        confirmed_outbound=1,
        wallet_balance_after=1990,
        wallet_reserved_after=0,
    )
    assert ok.net_points == 10


def test_cross_file_consistency() -> None:
    identity = run_identity_from_orm(_orm_run(), expected_config_version_id="cfg-1", attempts=1)
    scenario = ScenarioResultPayload(
        scenario="L1", verdict="PASS", terminal="completed", model_requests=1, mcp_outbound=1,
        points_charged=10,
    )
    accounting = AccountingPayload(
        run_id="run-1", net_points=10, confirmed_outbound=1,
        wallet_balance_after=1990, wallet_reserved_after=0,
    )
    usage = [
        usage_record_from_orm(
            SimpleNamespace(
                id="u1", kind="model", backend="pi", model="deepseek-v4-pro",
                input_tokens=10, output_tokens=5, usage_status="available", cost_status="unpriced",
            )
        )
    ]
    assert check_cross_file_consistency(
        run_identity=identity, scenario_result=scenario, accounting=accounting,
        usage_records=usage,
    ) == []

    bad_scenario = ScenarioResultPayload(
        scenario="L1", verdict="PASS", terminal="failed", model_requests=3, mcp_outbound=1,
        points_charged=10,
    )
    errors = check_cross_file_consistency(
        run_identity=identity, scenario_result=bad_scenario, accounting=accounting,
        usage_records=usage,
    )
    assert "scenario_terminal_mismatch" in errors
    assert "model_requests_mismatch" in errors

    bad_identity = run_identity_from_orm(_orm_run(), expected_config_version_id="other", attempts=1)
    errors = check_cross_file_consistency(
        run_identity=bad_identity, scenario_result=scenario, accounting=accounting,
        usage_records=usage,
    )
    assert "snapshot_binding_mismatch" in errors


def test_builders_use_named_fields_only() -> None:
    call = SimpleNamespace(
        id="c1", internal_tool_name="match_best_tag", service="insight-cube-mcp",
        status="settled", points_settled=10, logical_call_id="lc-1",
    )
    payload = tool_call_from_orm(call)
    assert payload.internal_tool_name == "match_best_tag"
    assert payload.points_settled == 10


def test_append_accepts_strict_dto(tmp_path) -> None:
    writer = _writer(tmp_path)
    identity = run_identity_from_orm(_orm_run(), expected_config_version_id="cfg-1", attempts=1)
    frame = writer.append(
        "run-identities.jsonl", scenario_id="L1", type="run_identity", payload=identity
    )
    assert frame["payload"]["backend"] == "pi"
    assert writer.verify("run-identities.jsonl") == []
