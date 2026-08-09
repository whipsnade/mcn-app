from fastapi import APIRouter

from app.admin.router import router as admin_router
from app.agent_artifacts.router import router as agent_artifacts_router
from app.agent_runtime.router import router as agent_runtime_router
from app.billing.router import router as billing_router
from app.favorites.router import router as favorites_router
from app.identity.router import auth_router, users_router
from app.pi_runtime_poc.router import router as pi_poc_router
from app.pi_gateway.router import router as pi_gateway_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(billing_router, prefix="/wallet", tags=["wallet"])
api_router.include_router(favorites_router, tags=["favorites"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
api_router.include_router(agent_runtime_router, prefix="/agent", tags=["agent"])
api_router.include_router(agent_artifacts_router, prefix="/agent", tags=["agent"])
api_router.include_router(pi_poc_router, prefix="/internal/pi-poc", tags=["pi-poc"])
api_router.include_router(pi_gateway_router, prefix="/internal/pi-gateway/v1", tags=["pi-gateway"])
# 旧 Session thinking SSE（GET /sessions/{id}/events）已随统一 Agent 运行时移除：
# 新运行时的 thinking 事件只走 /agent/runs/{id}/events（§5.8/§6.4），前端旧订阅
# 会对新会话 404 并无限重连。app.thinking 包已无生产引用，整体删除。
