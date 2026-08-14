"""仅供 Pi POC 子进程回调的最小 HTTP 应用。

它有意不复用 ``app.main``：主应用的 lifespan 会启动 Current Runtime 的后台领取循环，
这会与当前 Pi Run 的专属 runner 竞争同一条 queued Run，破坏一 Run 一进程的边界。
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI

import app.db.models  # noqa: F401, RUF100  # 最小 POC 服务也必须注册跨模块 ORM 外键目标。
from app.agent_runtime.events import AgentEventBroker
from app.pi_runtime_poc.router import router


def _configure_safe_diagnostics() -> None:
    path_value = os.environ.get("PI_RUNTIME_POC_DIAGNOSTIC_LOG")
    if not path_value:
        return
    logger = logging.getLogger("pi_runtime_poc.diagnostics")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    handler = logging.FileHandler(Path(path_value), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


_configure_safe_diagnostics()
app = FastAPI(title="KOL Insight Pi POC Internal API", version="0.1.0")
app.state.agent_event_broker = AgentEventBroker()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "kol-insight-pi-poc-internal"}


app.include_router(router, prefix="/api/v1/internal/pi-poc", tags=["pi-poc"])
