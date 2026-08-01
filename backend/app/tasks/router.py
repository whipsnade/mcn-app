import json
import asyncio
import logging
from contextlib import suppress
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionFactory, get_db
from app.goals.context import GoalPlannerContextBuilder
from app.goals.planner import GoalPlannerService
from app.goals.respond import OUT_OF_SCOPE_TEXT, USAGE_GUIDE_TEXT, answer_context_qa
from app.goals.schemas import GoalPlannerOutput, GoalQuestion
from app.identity.dependencies import FunctionScopedCurrentUser
from app.mcp_gateway.models import McpToolCatalog
from app.model.dependencies import get_model_adapter
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.events import TaskEventBroker, TaskEventStream
from app.tasks.repository import TaskRepository
from app.tasks.schemas import (
    TaskCreate,
    TaskCreateResult,
    TaskOutcomeClarify,
    TaskOutcomeRespond,
    TaskOutcomeTask,
    TaskRead,
)
from app.tasks.service import TaskConflictError, TaskService
from app.tasks.executor import TaskRunner
from app.thinking.contracts import ThinkingOperationSpec
from app.thinking.persistence import persist_turn_thinking
from app.thinking.service import (
    SessionThinkingService,
    get_session_thinking_service,
)
from app.workspace.models import Message
from app.workspace.serializers import message_read


router = APIRouter()
task_event_broker = TaskEventBroker()
logger = logging.getLogger(__name__)

_INTENT_CLARIFY_TEXT = "请确认您希望进行品牌分析、活动分析，还是达人圈选？"
_INTENT_CLARIFY_OPTIONS = ["品牌分析", "活动分析", "达人圈选"]


def get_task_event_stream() -> TaskEventStream:
    return TaskEventStream(SessionFactory, TaskRepository, task_event_broker)


def get_task_runner(request: Request) -> TaskRunner:
    return request.app.state.task_runner


def encode_sse_event(event: TaskEvent) -> str:
    data = json.dumps(event.payload_json, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"


def resolve_last_event_id(header_value: str | None, query_value: str | None) -> int:
    raw_value = header_value if header_value is not None else query_value
    if raw_value is None:
        return 0
    try:
        value = int(raw_value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid_last_event_id") from error
    if value < 0:
        raise HTTPException(status_code=422, detail="invalid_last_event_id")
    return value


async def sse_event_chunks(
    events: AsyncIterator[TaskEvent], *, heartbeat_seconds: float = 15
) -> AsyncIterator[str]:
    iterator = events.__aiter__()
    pending = asyncio.ensure_future(anext(iterator))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                yield ": heartbeat\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            yield encode_sse_event(event)
            pending = asyncio.ensure_future(anext(iterator))
    finally:
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


def task_read(task: AnalysisTask, metadata: dict | None = None) -> TaskRead:
    metadata = metadata or {}
    kind = getattr(task, "kind", "pipeline")
    return TaskRead(
        id=task.id,
        session_id=task.session_id,
        trigger_message_id=task.trigger_message_id,
        status=task.status,
        kind=kind if kind in {"pipeline", "agent"} else "pipeline",
        estimated_points=task.estimated_points,
        error_code=task.error_code,
        error_message=task.error_message,
        latest_report_id=None,
        followup_suggestions_status=metadata.get("followup_suggestions_status"),
        followup_suggestions=list(metadata.get("followup_suggestions", [])),
        followup_error=metadata.get("followup_error"),
    )


async def task_followup_metadata(db: AsyncSession, task: AnalysisTask) -> dict:
    """Read persisted suggestions from the assistant summary, never the user trigger."""
    messages = list(
        (
            await db.scalars(
                select(Message)
                .where(
                    Message.session_id == task.session_id,
                    Message.user_id == task.user_id,
                    Message.role == "assistant",
                )
                .order_by(Message.sequence.desc())
            )
        ).all()
    )
    return next(
        (
            message.metadata_json
            for message in messages
            if message.metadata_json.get("task_id") == task.id
        ),
        {},
    )


def task_not_found(error: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found")


async def _plan_goal_or_fallback(
    db: AsyncSession,
    user_id: str,
    session_id: str,
    content: str,
    *,
    turn_id: str,
    thinking_service: SessionThinkingService,
) -> GoalPlannerOutput | None:
    """执行规划；失败返回 None，由调用方转为最终意图澄清，不创建任务。

    ``LookupError("session_not_found")`` 是会话归属问题，原样上抛映射 404。
    """
    thinking_sink = None
    try:
        thinking_sink = thinking_service.create_sink(
            ThinkingOperationSpec(
                operation_id=str(uuid4()),
                turn_id=turn_id,
                session_id=session_id,
                user_id=user_id,
                purpose="goal_planner",
                label="正在规划分析目标",
            )
        )
    except Exception:
        logger.warning(
            "goal_planner_thinking_sink_create_failed session_id=%s",
            session_id,
            exc_info=True,
        )
    try:
        # 已审核 MCP 工具的紧凑投影：planner 围绕数据能力追问执行条件的依据。
        # 只查 catalog 表不走网络；失败不阻塞规划（空清单也能规划）。
        try:
            catalog_rows = (
                await db.scalars(
                    select(McpToolCatalog).where(
                        McpToolCatalog.is_enabled.is_(True),
                        McpToolCatalog.review_status == "approved",
                    )
                )
            ).all()
            available_tools = tuple(
                {
                    "internal_name": row.internal_tool_name,
                    "description": row.reviewed_description,
                    "required_params": list(
                        (row.input_schema_json or {}).get("required") or []
                    ),
                }
                for row in catalog_rows
            )
        except Exception:
            logger.warning(
                "goal_planner_tools_load_failed session_id=%s",
                session_id,
                exc_info=True,
            )
            available_tools = ()
        context = await GoalPlannerContextBuilder().build_for_message(
            user_id, session_id, content, db=db, available_tools=available_tools
        )
        return await GoalPlannerService(
            model=get_model_adapter(), context_builder=None
        ).plan_context(
            context,
            thinking_sink=thinking_sink,
        )
    except LookupError:
        raise
    except Exception:
        logger.warning(
            "goal_planner_enforce_fallback session_id=%s", session_id, exc_info=True
        )
        return None


async def _append_clarify_message(
    db: AsyncSession,
    user_id: str,
    session_id: str,
    *,
    content: str,
    turn_id: str,
    question,
) -> tuple[Message, Message]:
    """planner clarify：落同 turn 的 user + assistant 澄清消息。"""
    max_sequence = await db.scalar(
        select(func.max(Message.sequence)).where(Message.session_id == session_id)
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    user_message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        role="user",
        content=content,
        sequence=(max_sequence or 0) + 1,
        metadata_json={"turn_id": turn_id},
        created_at=now,
    )
    message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        role="assistant",
        content=question.text,
        sequence=(max_sequence or 0) + 2,
        metadata_json={
            "turn_id": turn_id,
            "clarify": {"options": list(question.options)},
        },
        created_at=now,
    )
    db.add_all([user_message, message])
    await db.flush()
    return user_message, message


async def _append_respond_message(
    db: AsyncSession,
    user_id: str,
    session_id: str,
    *,
    content: str,
    turn_id: str,
    respond_type: str,
    reply: str,
) -> tuple[Message, Message]:
    """planner respond：落同 turn 的 user + assistant 回复消息。"""
    max_sequence = await db.scalar(
        select(func.max(Message.sequence)).where(Message.session_id == session_id)
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    user_message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        role="user",
        content=content,
        sequence=(max_sequence or 0) + 1,
        metadata_json={"turn_id": turn_id},
        created_at=now,
    )
    message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        role="assistant",
        content=reply,
        sequence=(max_sequence or 0) + 2,
        metadata_json={"turn_id": turn_id, "respond": {"type": respond_type}},
        created_at=now,
    )
    db.add_all([user_message, message])
    await db.flush()
    return user_message, message


@router.post("/sessions/{session_id}/tasks", response_model=TaskCreateResult, status_code=202)
async def create_task(
    session_id: str,
    payload: TaskCreate,
    user: FunctionScopedCurrentUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    thinking_service: SessionThinkingService = Depends(get_session_thinking_service),
) -> TaskCreateResult:
    service = TaskService(db)
    goal_specs: list[dict] | None = None
    planner_attempted = False
    turn_id = str(payload.turn_id)
    planner_enabled = get_settings().goal_planner_enforce_enabled
    planner_output: GoalPlannerOutput | None = None
    # 带幂等键命中时不再调 Planner，也不改变该请求既有的任务结果。
    plan_needed = True
    if idempotency_key is not None:
        existing = await service.find_idempotent(user.id, session_id, idempotency_key)
        plan_needed = existing is None
    if plan_needed:
        if planner_enabled:
            planner_attempted = True
            try:
                planner_output = await _plan_goal_or_fallback(
                    db,
                    user.id,
                    session_id,
                    payload.content,
                    turn_id=turn_id,
                    thinking_service=thinking_service,
                )
            except LookupError as error:
                raise task_not_found(error) from error
        if planner_output is None:
            planner_output = GoalPlannerOutput(
                action="clarify",
                question=GoalQuestion(
                    text=_INTENT_CLARIFY_TEXT,
                    options=list(_INTENT_CLARIFY_OPTIONS),
                ),
            )
        if planner_output.action == "clarify":
            user_message, message = await _append_clarify_message(
                db,
                user.id,
                session_id,
                content=payload.content,
                turn_id=turn_id,
                question=planner_output.question,
            )
            try:
                await thinking_service.bind_turn(
                    turn_id=turn_id,
                    user_id=user.id,
                    session_id=session_id,
                    task_id=None,
                    trigger_message_id=user_message.id,
                )
            except Exception:
                logger.warning(
                    "goal_planner_clarify_thinking_bind_failed session_id=%s",
                    session_id,
                    exc_info=True,
                )
            await db.commit()
            try:
                await persist_turn_thinking(
                    SessionFactory,
                    thinking_service,
                    user_id=user.id,
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_message_id=message.id,
                )
            except Exception:
                logger.warning(
                    "goal_planner_clarify_thinking_persist_failed session_id=%s",
                    session_id,
                    exc_info=True,
                )
            return TaskOutcomeClarify(message=message_read(message))
        if planner_output.action == "respond":
            respond_type = planner_output.respond_type
            if respond_type == "usage_help":
                reply = USAGE_GUIDE_TEXT
            elif respond_type == "out_of_scope":
                reply = OUT_OF_SCOPE_TEXT
            else:
                reply = await answer_context_qa(
                    db,
                    get_model_adapter(),
                    user_id=user.id,
                    session_id=session_id,
                    question=payload.content,
                )
            user_message, message = await _append_respond_message(
                db,
                user.id,
                session_id,
                content=payload.content,
                turn_id=turn_id,
                respond_type=respond_type,
                reply=reply,
            )
            try:
                await thinking_service.bind_turn(
                    turn_id=turn_id,
                    user_id=user.id,
                    session_id=session_id,
                    task_id=None,
                    trigger_message_id=user_message.id,
                )
            except Exception:
                logger.warning(
                    "goal_planner_respond_thinking_bind_failed session_id=%s",
                    session_id,
                    exc_info=True,
                )
            await db.commit()
            try:
                await persist_turn_thinking(
                    SessionFactory,
                    thinking_service,
                    user_id=user.id,
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_message_id=message.id,
                )
            except Exception:
                logger.warning(
                    "goal_planner_respond_thinking_persist_failed session_id=%s",
                    session_id,
                    exc_info=True,
                )
            return TaskOutcomeRespond(
                respond_type=respond_type, message=message_read(message)
            )
        # 阶段四顺序编排：planner 输出的 1-3 个 goal 全部落库。
        goal_specs = [
            {
                "goal_type": goal.goal_type,
                "sequence": goal.sequence,
                "depends_on_sequence": goal.depends_on_sequence,
                # 注意：model_dump 不排除默认值，GoalParams.comparison_mode
                # 的默认值 "mom" 会并入所有 goal 类型（含 campaign/kol）的
                # params_json；消费方仅 brand_analysis 读取该字段。
                "params": goal.params.model_dump(mode="json", exclude_none=True),
            }
            for goal in sorted(planner_output.goals, key=lambda item: item.sequence)
        ]
        if len(goal_specs) > 1:
            logger.info(
                "goal_planner_multi_goal session_id=%s goals=%d",
                session_id,
                len(goal_specs),
            )
    try:
        if idempotency_key is None:
            task = await service.create(
                user.id,
                session_id,
                payload,
                goal_specs=goal_specs,
            )
            reused = False
        else:
            task, reused = await service.create_idempotent(
                user.id,
                session_id,
                payload,
                idempotency_key,
                goal_specs=goal_specs,
            )
    except TaskConflictError as error:
        detail = (
            "幂等键对应的请求参数不一致"
            if str(error) == "idempotency_payload_mismatch"
            else str(error)
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid_idempotency_key") from error
    except LookupError as error:
        raise task_not_found(error) from error
    if planner_attempted:
        try:
            await thinking_service.bind_turn(
                turn_id=turn_id,
                user_id=user.id,
                session_id=session_id,
                task_id=task.id,
                trigger_message_id=task.trigger_message_id,
            )
        except Exception:
            logger.warning(
                "goal_planner_task_thinking_bind_failed task_id=%s",
                task.id,
                exc_info=True,
            )
    await db.commit()
    # 思考持久化移到 commit 后独立事务：请求事务内的长事务 + 行锁曾是
    # InnoDB 死锁/静默回滚（幽灵 task_id）的根源。enforce 关闭时 blocks 为空
    # 且 assistant_message_id=None，helper 提前 return，无害。
    try:
        await persist_turn_thinking(
            SessionFactory,
            thinking_service,
            user_id=user.id,
            session_id=session_id,
            turn_id=turn_id,
        )
    except Exception:
        logger.warning(
            "goal_planner_task_thinking_persist_failed task_id=%s",
            task.id,
            exc_info=True,
        )
    if not reused:
        task_runner.submit(task.id)
    return TaskOutcomeTask(task=task_read(task))


@router.post("/tasks/{task_id}/retry", response_model=TaskRead, status_code=202)
async def retry_task(
    task_id: str,
    user: FunctionScopedCurrentUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
) -> TaskRead:
    try:
        task = await TaskService(db).retry(user.id, task_id)
    except TaskConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except LookupError as error:
        raise task_not_found(error) from error
    await db.commit()
    task_runner.submit(task.id)
    return task_read(task)


@router.post("/tasks/{task_id}/followups/retry", response_model=TaskRead, status_code=202)
async def retry_followups(
    task_id: str,
    user: FunctionScopedCurrentUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
) -> TaskRead:
    """Retry only the non-fatal follow-up model call for the same task."""
    try:
        task = await TaskRepository(db).get_owned(task_id, user.id)
    except LookupError as error:
        raise task_not_found(error) from error
    if task.status not in {"completed", "completed_with_warnings"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="task_not_terminal")
    metadata = await task_followup_metadata(db, task)
    if metadata.get("followup_suggestions_status") != "failed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="followup_retry_not_failed")
    started = await task_runner.retry_followup(task_id)
    if not started:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="followup_retry_unavailable")
    # End the read transaction before refreshing: MySQL's repeatable-read
    # snapshot otherwise keeps returning the pre-retry `failed` metadata.
    await db.commit()
    await db.refresh(task)
    return task_read(task, await task_followup_metadata(db, task))


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: str,
    user: FunctionScopedCurrentUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> TaskRead:
    try:
        task = await TaskRepository(db).get_owned(task_id, user.id)
    except LookupError as error:
        raise task_not_found(error) from error
    return task_read(task, await task_followup_metadata(db, task))


@router.post("/tasks/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    user: FunctionScopedCurrentUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> TaskRead:
    try:
        task = await TaskService(db).cancel(user.id, task_id)
    except LookupError as error:
        raise task_not_found(error) from error
    await db.commit()
    return task_read(task)


@router.get("/tasks/{task_id}/events")
async def stream_task_events(
    task_id: str,
    user: FunctionScopedCurrentUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    event_stream: Annotated[TaskEventStream, Depends(get_task_event_stream)],
    last_event_id: Annotated[str | None, Query()] = None,
    last_event_id_header: Annotated[
        str | None, Header(alias="Last-Event-ID")
    ] = None,
) -> StreamingResponse:
    try:
        await TaskRepository(db).get_owned(task_id, user.id)
    except LookupError as error:
        raise task_not_found(error) from error
    seen = resolve_last_event_id(last_event_id_header, last_event_id)
    events = event_stream.stream(task_id, user.id, seen)
    return StreamingResponse(
        sse_event_chunks(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
