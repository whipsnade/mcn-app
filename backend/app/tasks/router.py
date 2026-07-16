import json
import asyncio
from contextlib import suppress
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionFactory, get_db
from app.identity.dependencies import FunctionScopedCurrentUser
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.events import TaskEventBroker, TaskEventStream
from app.tasks.repository import TaskRepository
from app.tasks.schemas import TaskCreate, TaskRead
from app.tasks.service import TaskConflictError, TaskService
from app.tasks.executor import TaskRunner
from app.workspace.models import Message


router = APIRouter()
task_event_broker = TaskEventBroker()


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
    return TaskRead(
        id=task.id,
        session_id=task.session_id,
        trigger_message_id=task.trigger_message_id,
        status=task.status,
        estimated_points=task.estimated_points,
        error_code=task.error_code,
        error_message=task.error_message,
        latest_report_id=None,
        followup_suggestions_status=metadata.get("followup_suggestions_status"),
        followup_suggestions=list(metadata.get("followup_suggestions", [])),
        followup_error=metadata.get("followup_error"),
    )


def task_not_found(error: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found")


@router.post("/sessions/{session_id}/tasks", response_model=TaskRead, status_code=202)
async def create_task(
    session_id: str,
    payload: TaskCreate,
    user: FunctionScopedCurrentUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
) -> TaskRead:
    try:
        task = await TaskService(db).create(user.id, session_id, payload)
    except TaskConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="task_in_progress") from error
    except LookupError as error:
        raise task_not_found(error) from error
    await db.commit()
    task_runner.submit(task.id)
    return task_read(task)


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
    message = await db.get(Message, task.trigger_message_id)
    return task_read(task, message.metadata_json if message is not None else None)


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
