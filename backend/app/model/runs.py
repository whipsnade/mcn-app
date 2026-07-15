from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.model.contracts import ModelRequestMetadata, TokenUsage
from app.model.models import ModelRun


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ModelRunService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def start(self, task_id: str, request: ModelRequestMetadata) -> ModelRun:
        now = _now()
        run = ModelRun(
            id=str(uuid4()),
            task_id=task_id,
            purpose=request.purpose,
            provider=request.provider,
            model=request.model,
            prompt_template=request.prompt_template,
            prompt_version=request.prompt_version,
            status="running",
            created_at=now,
            updated_at=now,
        )
        self.db.add(run)
        await self.db.flush()
        return run

    async def succeed(
        self,
        run: ModelRun,
        *,
        usage: TokenUsage | None,
        request_id: str | None,
        duration_ms: int,
    ) -> ModelRun:
        run.status = "succeeded"
        run.request_id = request_id
        run.duration_ms = duration_ms
        run.error_type = None
        if usage is not None:
            for field, value in usage.model_dump().items():
                setattr(run, field, value)
        run.updated_at = _now()
        await self.db.flush()
        return run

    async def fail(
        self,
        run: ModelRun,
        *,
        error_type: str,
        request_id: str | None,
        duration_ms: int,
    ) -> ModelRun:
        run.status = "failed"
        run.error_type = error_type
        run.request_id = request_id
        run.duration_ms = duration_ms
        run.updated_at = _now()
        await self.db.flush()
        return run
