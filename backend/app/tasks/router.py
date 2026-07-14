import json
import asyncio
from contextlib import suppress
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionFactory, get_db
from app.identity.dependencies import CurrentUser
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.events import TaskEventBroker, TaskEventStream
from app.tasks.repository import TaskRepository
from app.tasks.schemas import TaskCreate, TaskRead
from app.tasks.service import TaskService


router = APIRouter()
task_event_broker = TaskEventBroker()


def get_task_event_stream() -> TaskEventStream:
    return TaskEventStream(SessionFactory, TaskRepository, task_event_broker)


def encode_sse_event(event: TaskEvent) -> str:
    data = json.dumps(event.payload_json, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"


def resolve_last_event_id(header_value: str | None, query_value: int) -> int:
    if header_value is None:
        return query_value
    try:
        value = int(header_value)
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


def task_read(task: AnalysisTask) -> TaskRead:
    return TaskRead(
        id=task.id,
        session_id=task.session_id,
        status=task.status,
        estimated_points=task.estimated_points,
        error_code=task.error_code,
        latest_report_id=None,
    )


def task_not_found(error: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found")


@router.post("/sessions/{session_id}/tasks", response_model=TaskRead, status_code=202)
async def create_task(
    session_id: str,
    payload: TaskCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    try:
        task = await TaskService(db).create(user.id, session_id, payload)
    except LookupError as error:
        raise task_not_found(error) from error
    return task_read(task)


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    try:
        task = await TaskRepository(db).get_owned(task_id, user.id)
    except LookupError as error:
        raise task_not_found(error) from error
    return task_read(task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    try:
        task = await TaskService(db).cancel(user.id, task_id)
    except LookupError as error:
        raise task_not_found(error) from error
    return task_read(task)


@router.get("/tasks/{task_id}/events")
async def stream_task_events(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    event_stream: Annotated[TaskEventStream, Depends(get_task_event_stream)],
    last_event_id: Annotated[int, Query(ge=0)] = 0,
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
