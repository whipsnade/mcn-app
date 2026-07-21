"""模型 prompt/响应学习日志的统一写入口。

记录动作使用独立的 SessionFactory 会话，与调用方（请求线程/任务循环）
的事务完全隔离；任何写库异常都只记 warning，绝不阻塞主流程。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from app.db.session import SessionFactory
from app.model.models import ModelPromptLog


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptLogEntry:
    """一次模型调用的完整日志载荷（适配器统一出口组装）。"""

    purpose: str
    model: str
    messages: str
    status: str  # success / invalid / failed
    user_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    tags: tuple[str, ...] = ()
    response: str | None = None
    error_code: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None


PromptLogWriter = Callable[[PromptLogEntry], Awaitable[None]]


async def record_prompt_log(entry: PromptLogEntry) -> None:
    """写入一行 model_prompt_logs；失败只记 warning，不向调用方抛错。"""
    try:
        async with SessionFactory.begin() as db:
            db.add(
                ModelPromptLog(
                    id=str(uuid4()),
                    user_id=entry.user_id,
                    session_id=entry.session_id,
                    task_id=entry.task_id,
                    purpose=entry.purpose,
                    tags=list(entry.tags),
                    model=entry.model,
                    messages=entry.messages,
                    response=entry.response,
                    status=entry.status,
                    error_code=entry.error_code,
                    prompt_tokens=entry.prompt_tokens,
                    completion_tokens=entry.completion_tokens,
                    duration_ms=entry.duration_ms,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
    except Exception:
        logger.warning("model prompt log write failed purpose=%s", entry.purpose, exc_info=True)
