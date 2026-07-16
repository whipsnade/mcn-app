import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.tasks.dependencies import create_task_runtime, refresh_approved_datatap_tools


def create_app() -> FastAPI:
    settings = get_settings()
    runner, recovery = create_task_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await refresh_approved_datatap_tools()
        app.state.task_runner = runner
        stop_recovery = asyncio.Event()

        async def recover_once() -> None:
            try:
                await recovery.recover_expired()
                await recovery.recover_pending_followups()
            except Exception:
                # A later fixed-interval pass retries transient database faults.
                return

        async def recover_periodically() -> None:
            while not stop_recovery.is_set():
                try:
                    await asyncio.wait_for(stop_recovery.wait(), timeout=30)
                except TimeoutError:
                    await recover_once()

        startup_recovery = asyncio.create_task(recover_once())
        coordinator = asyncio.create_task(recover_periodically())
        try:
            yield
        finally:
            stop_recovery.set()
            await coordinator
            await startup_recovery
            await runner.shutdown()

    app = FastAPI(title="KOL Insight API", version="0.1.0", lifespan=lifespan)
    # httpx's ASGI transport may skip lifespan in narrow route tests; keep the
    # same runner available while production startup still performs recovery.
    app.state.task_runner = runner
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "kol-insight-api"}

    app.include_router(api_router)
    return app


app = create_app()
