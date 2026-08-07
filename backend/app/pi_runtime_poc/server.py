"""仅供 Pi POC 子进程回调的最小 HTTP 应用。

它有意不复用 ``app.main``：主应用的 lifespan 会启动 Current Runtime 的后台领取循环，
这会与当前 Pi Run 的专属 runner 竞争同一条 queued Run，破坏一 Run 一进程的边界。
"""

from fastapi import FastAPI

from app.agent_runtime.events import AgentEventBroker
from app.pi_runtime_poc.router import router

app = FastAPI(title="KOL Insight Pi POC Internal API", version="0.1.0")
app.state.agent_event_broker = AgentEventBroker()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "kol-insight-pi-poc-internal"}


app.include_router(router, prefix="/api/v1/internal/pi-poc", tags=["pi-poc"])
