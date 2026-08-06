"""RunTranscriptLoader 测试（v3 加固 §5.4 / A4）。

接管恢复必须从触发消息 + 本 Run 完整 Step 重建模型上下文：

- 已完成 tool_call Step 回放持久结果（settled = ``evidence_id`` + 结构化预览，
  failed/unknown = 原结构化错误结果），**不回灌 raw payload**；
- 崩溃残留的 running Step（外发后 / settle 前崩溃）按 ``agent_tool_calls`` 行
  当前状态构造结果回放，并作为 ``resume_step`` 交给引擎复用（沿用原
  ``logical_call_id``，协调器幂等回放，绝不重发、不重复扣费）；
- 触发消息优先取 ``run.input_message_id``，回退会话最近一条用户消息。
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.kol_detail import (
    build_kol_detail_prompt_snapshot,
    kol_detail_trigger_content,
)
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
)
from app.agent_runtime.repository import utc_now
from app.agent_runtime.tools.mcp import logical_call_id_for
from app.agent_runtime.transcript import RunTranscriptLoader
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.validation import canonical_json_bytes

INTERNAL_NAME = "query_analysis_data"


async def _make_chain(db_session, user_factory, *, message: str = "帮我分析品牌"):
    """用户 + 会话 + 触发消息 + running Run + open Attempt 的最小链路。"""
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="transcript 测试会话",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    input_msg = AgentMessage(
        id=str(uuid4()),
        session_id=session.id,
        run_id=None,
        role="user",
        content=message,
        metadata_json=None,
        sequence=1,
        created_at=now,
    )
    db_session.add(input_msg)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        input_message_id=input_msg.id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
        decision_count=0,
        review_count=0,
        revision_count=0,
        started_at=now,
    )
    db_session.add(run)
    await db_session.flush()
    input_msg.run_id = run.id
    attempt = AgentRunAttempt(
        id=str(uuid4()),
        run_id=run.id,
        attempt=1,
        started_at=now,
        decision_count=0,
        outcome="running",
    )
    db_session.add(attempt)
    await db_session.flush()
    return user, session, run, attempt, input_msg


def _tool_step(
    run: AgentRun,
    attempt: AgentRunAttempt,
    *,
    sequence: int,
    status: str,
    output: dict | None = None,
    arguments: dict | None = None,
) -> AgentStep:
    return AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=sequence,
        step_type="tool_call",
        input_json={
            "internal_tool_name": INTERNAL_NAME,
            "arguments": arguments if arguments is not None else {"keyword": "美妆"},
        },
        output_json=output,
        status=status,
        visibility="user",
        created_at=utc_now(),
    )


def _make_call(
    run: AgentRun,
    step: AgentStep,
    *,
    status: str,
    message: str | None = None,
) -> AgentToolCall:
    args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
    return AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=logical_call_id_for(run.id, INTERNAL_NAME, args_hash),
        service=DataTapService.INSIGHT_CUBE.value,
        internal_tool_name=INTERNAL_NAME,
        arguments_json={"keyword": "美妆"},
        arguments_hash=args_hash,
        status=status,
        points_reserved=10,
        error_type="result_unknown" if status == "unknown" else None,
        safe_error_message=message,
        started_at=utc_now(),
    )


def _tool_results(transcript_messages) -> list[dict]:
    """抽出 transcript 中全部 user 角色的 tool_result 负载（触发消息等非 JSON 跳过）。"""
    results = []
    for message in transcript_messages:
        if message.role != "user":
            continue
        try:
            payload = json.loads(message.content)
        except ValueError:
            continue
        if isinstance(payload, dict) and "tool_result" in payload:
            results.append(payload["tool_result"])
    return results


def _assistant_actions(transcript_messages) -> list[dict]:
    actions = []
    for message in transcript_messages:
        if message.role != "assistant":
            continue
        try:
            payload = json.loads(message.content)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("action") == "call_tool":
            actions.append(payload)
    return actions


# ---------------------------------------------------------------------------
# 完整 Step 回放
# ---------------------------------------------------------------------------


async def test_completed_steps_replay_persisted_results_in_order(
    db_session, user_factory
) -> None:
    """两个已完成 tool_call Step 按 sequence 回放：assistant 动作 + tool_result。"""
    _, _, run, attempt, input_msg = await _make_chain(db_session, user_factory)
    step1 = _tool_step(
        run,
        attempt,
        sequence=1,
        status="completed",
        output={
            "status": "success",
            "safe_summary": "声量预览文本",
            "evidence_id": "ev-1",
            "cursor": None,
            "truncated": False,
            "error_type": None,
        },
    )
    step2 = _tool_step(
        run,
        attempt,
        sequence=2,
        status="failed",
        output={
            "status": "failed",
            "safe_summary": "upstream reported a business error",
            "evidence_id": None,
            "cursor": None,
            "truncated": False,
            "error_type": "failed_confirmed",
        },
        arguments={"keyword": "咖啡"},
    )
    db_session.add_all([step1, step2])
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    # 触发消息在开头
    assert transcript.messages[0].role == "user"
    assert transcript.messages[0].content == "帮我分析品牌"
    # 两个 Step 各回放一对（assistant 动作 + user 结果）
    assert len(transcript.messages) == 5
    actions = _assistant_actions(transcript.messages)
    assert [action["internal_tool_name"] for action in actions] == [
        INTERNAL_NAME,
        INTERNAL_NAME,
    ]
    assert actions[0]["arguments"] == {"keyword": "美妆"}
    assert actions[1]["arguments"] == {"keyword": "咖啡"}
    results = _tool_results(transcript.messages)
    assert [result["status"] for result in results] == ["success", "failed"]
    assert results[0]["summary"] == "声量预览文本"
    assert results[0]["evidence_id"] == "ev-1"
    assert results[1]["error_type"] == "failed_confirmed"
    # 全部 Step 完整：没有待复用的崩溃残留 Step
    assert transcript.resume_step is None


async def test_replay_never_leaks_raw_payload(db_session, user_factory) -> None:
    """回放内容只含 evidence_id + 结构化预览，绝不回灌 raw payload（上下文预算）。"""
    _, session, run, attempt, _ = await _make_chain(db_session, user_factory)
    call_step = _tool_step(run, attempt, sequence=1, status="completed")
    db_session.add(call_step)
    await db_session.flush()
    call = _make_call(run, call_step, status="settled")
    db_session.add(call)
    await db_session.flush()
    # raw payload 含敏感原文；预览里不应出现
    evidence = await EvidenceWriter(db_session).write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name=INTERNAL_NAME,
        scope_json=None,
        period_json=None,
        raw_payload={"result": json.dumps({"rows": [{"note": "SECRET_RAW_CONTENT"}]})},
    )
    call_step.output_json = {
        "status": "success",
        "safe_summary": "受限预览文本",
        "evidence_id": evidence.id,
        "cursor": None,
        "truncated": False,
        "error_type": None,
    }
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    blob = "\n".join(message.content for message in transcript.messages)
    assert "受限预览文本" in blob
    assert evidence.id in blob
    assert "SECRET_RAW_CONTENT" not in blob
    assert "raw_payload" not in blob


async def test_failed_and_unknown_results_replay_original_structured_error(
    db_session, user_factory
) -> None:
    """failed/unknown 结果回放原结构化错误（error_type 保留）。"""
    _, _, run, attempt, _ = await _make_chain(db_session, user_factory)
    step = _tool_step(
        run,
        attempt,
        sequence=1,
        status="failed",
        output={
            "status": "unknown",
            "safe_summary": "gateway timeout 504",
            "evidence_id": None,
            "cursor": None,
            "truncated": False,
            "error_type": "result_unknown",
        },
    )
    db_session.add(step)
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    results = _tool_results(transcript.messages)
    assert len(results) == 1
    assert results[0]["status"] == "unknown"
    assert results[0]["error_type"] == "result_unknown"
    assert results[0]["summary"] == "gateway timeout 504"


# ---------------------------------------------------------------------------
# 崩溃残留 running Step：按 agent_tool_calls 行构造结果 + resume_step
# ---------------------------------------------------------------------------


async def test_running_step_with_settled_call_replays_success(
    db_session, user_factory
) -> None:
    """settle 完成但 Step 未更新即崩溃：回放真实 success（evidence_id + 预览）。"""
    _, session, run, attempt, _ = await _make_chain(db_session, user_factory)
    step = _tool_step(run, attempt, sequence=1, status="running")
    db_session.add(step)
    await db_session.flush()
    call = _make_call(run, step, status="settled")
    db_session.add(call)
    await db_session.flush()
    evidence = await EvidenceWriter(db_session).write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name=INTERNAL_NAME,
        scope_json=None,
        period_json=None,
        raw_payload={"result": json.dumps({"rows": [{"keyword": "美妆"}], "total": 1})},
    )

    transcript = await RunTranscriptLoader(db_session).load(run)

    results = _tool_results(transcript.messages)
    assert len(results) == 1
    assert results[0]["status"] == "success"
    assert results[0]["evidence_id"] == evidence.id
    assert "美妆" in results[0]["summary"]
    # 崩溃残留 Step 交给引擎复用（沿用原 logical_call_id）
    assert transcript.resume_step is not None
    assert transcript.resume_step.id == step.id


async def test_running_step_with_unknown_call_replays_unknown(
    db_session, user_factory
) -> None:
    """外发后崩溃（行已按 A3 迁为 unknown）：回放 unknown 结构化结果。"""
    _, _, run, attempt, _ = await _make_chain(db_session, user_factory)
    step = _tool_step(run, attempt, sequence=1, status="running")
    db_session.add(step)
    await db_session.flush()
    db_session.add(_make_call(run, step, status="unknown", message="gateway timeout 504"))
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    results = _tool_results(transcript.messages)
    assert len(results) == 1
    assert results[0]["status"] == "unknown"
    assert results[0]["error_type"] == "result_unknown"
    assert "504" in results[0]["summary"]
    assert transcript.resume_step is not None
    assert transcript.resume_step.id == step.id


async def test_running_step_without_call_row_replays_interrupted_unknown(
    db_session, user_factory
) -> None:
    """prepare 前崩溃（无调用行）：回放 unknown（未外发），仍可复用 Step 重续。"""
    _, _, run, attempt, _ = await _make_chain(db_session, user_factory)
    step = _tool_step(run, attempt, sequence=1, status="running")
    db_session.add(step)
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    results = _tool_results(transcript.messages)
    assert len(results) == 1
    assert results[0]["status"] == "unknown"
    assert results[0]["error_type"] == "result_unknown"
    assert transcript.resume_step is not None
    assert transcript.resume_step.id == step.id


async def test_model_decision_steps_are_not_replayed(db_session, user_factory) -> None:
    """model_decision Step（含崩溃残留的 running）不参与工具回放。"""
    _, _, run, attempt, _ = await _make_chain(db_session, user_factory)
    db_session.add(
        AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt.id,
            sequence=1,
            step_type="model_decision",
            input_json=[{"role": "user", "content": "帮我分析品牌"}],
            output_json=None,
            status="running",
            visibility="user",
            created_at=utc_now(),
        )
    )
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    assert len(transcript.messages) == 1  # 只有触发消息
    assert transcript.resume_step is None


# ---------------------------------------------------------------------------
# 触发消息选择
# ---------------------------------------------------------------------------


async def test_trigger_message_falls_back_to_latest_user_message(
    db_session, user_factory
) -> None:
    """run.input_message_id 为空时回退到会话最近一条用户消息。"""
    _, session, run, _, _ = await _make_chain(db_session, user_factory)
    run.input_message_id = None
    later = AgentMessage(
        id=str(uuid4()),
        session_id=session.id,
        run_id=None,
        role="user",
        content="换个角度再分析一次",
        metadata_json=None,
        sequence=2,
        created_at=utc_now(),
    )
    db_session.add(later)
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    assert transcript.messages[0].content == "换个角度再分析一次"


# ---------------------------------------------------------------------------
# 显式用户问题锚点（G3）
# ---------------------------------------------------------------------------


async def test_user_question_anchor_is_trigger_message_not_tool_result(
    db_session, user_factory
) -> None:
    """transcript 显式携带触发消息作为 ``user_question`` 锚点（G3）：tool_result
    回放消息同样是 role="user"，引擎不得再从消息列表尾部反推用户问题。"""
    _, _, run, attempt, _ = await _make_chain(db_session, user_factory)
    step = _tool_step(
        run,
        attempt,
        sequence=1,
        status="completed",
        output={
            "status": "success",
            "safe_summary": "声量预览文本",
            "evidence_id": "ev-1",
            "cursor": None,
            "truncated": False,
            "error_type": None,
        },
    )
    db_session.add(step)
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    # 锚点是触发消息，而不是最后一条 user 角色的 tool_result 回放 JSON。
    assert transcript.user_question == "帮我分析品牌"
    assert transcript.messages[0].content == "帮我分析品牌"
    assert transcript.messages[-1].role == "user"
    assert "tool_result" in transcript.messages[-1].content


async def test_kol_detail_run_trigger_restored_from_prompt_snapshot(
    db_session, user_factory
) -> None:
    """kol_detail Run（无 ``input_message_id``）：从 ``prompt_snapshot_json``
    恢复 platform/kol_uid 触发上下文（G3），不回退到会话最近一条普通用户消息。"""
    _, session, run, _, _ = await _make_chain(db_session, user_factory)
    run.input_message_id = None
    run.profile_name = "kol_detail_v1"
    run.prompt_snapshot_json = build_kol_detail_prompt_snapshot(
        platform="xiaohongshu",
        kol_uid="k1",
        selection_artifact_id=None,
        selection_version=None,
    )
    unrelated = AgentMessage(
        id=str(uuid4()),
        session_id=session.id,
        run_id=None,
        role="user",
        content="给我看看上个月的品牌声量",
        metadata_json=None,
        sequence=2,
        created_at=utc_now(),
    )
    db_session.add(unrelated)
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    expected = kol_detail_trigger_content("xiaohongshu", "k1")
    assert transcript.messages[0].role == "user"
    assert transcript.messages[0].content == expected
    assert "品牌声量" not in transcript.messages[0].content
    assert transcript.user_question == expected


async def test_failed_row_replay_keeps_structured_feedback(
    db_session, user_factory
) -> None:
    """崩溃恢复回放同一结构化失败反馈（Gate B Task 6：safe summary 一致）。

    failed 行的 safe_error_message 存 ToolFailureFeedback JSON；resume 回放
    时原样透传（same_fingerprint_retry_allowed 保留），模型拿到与首次失败
    相同的决策依据。
    """
    _, session, run, attempt, _ = await _make_chain(db_session, user_factory)
    step = _tool_step(run, attempt, sequence=1, status="running")
    db_session.add(step)
    await db_session.flush()
    feedback = {
        "tool": "query_analysis_data",
        "arguments_summary": {"keyword": "美妆"},
        "error_type": "definitely_not_sent",
        "upstream_code": None,
        "upstream_reason": "connect timeout",
        "request_state": "failed",
        "points_state": "released",
        "same_fingerprint_retry_allowed": False,
        "normalization_status": None,
        "suggested_actions": ["修改参数或拆分平台后重试", "继续其他章节"],
    }
    args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=logical_call_id_for(run.id, INTERNAL_NAME, args_hash),
        service=DataTapService.INSIGHT_CUBE.value,
        internal_tool_name=INTERNAL_NAME,
        arguments_json={"keyword": "美妆"},
        arguments_hash=args_hash,
        status="failed",
        points_reserved=10,
        points_settled=0,
        error_type="definitely_not_sent",
        safe_error_message=json.dumps(feedback, ensure_ascii=False),
        started_at=utc_now(),
    )
    db_session.add(call)
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)

    results = _tool_results(transcript.messages)
    assert len(results) == 1
    assert results[0]["status"] == "failed"
    assert results[0]["error_type"] == "definitely_not_sent"
    replayed = json.loads(results[0]["summary"])
    assert replayed["same_fingerprint_retry_allowed"] is False
    assert replayed["points_state"] == "released"
    assert replayed["suggested_actions"] == feedback["suggested_actions"]
