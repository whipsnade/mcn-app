from fastapi import APIRouter

from app.admin.router import router as admin_router
from app.agent_artifacts.router import router as agent_artifacts_router
from app.agent_runtime.router import router as agent_runtime_router
from app.billing.router import router as billing_router
from app.favorites.router import router as favorites_router
from app.identity.router import auth_router, users_router
from app.thinking.router import router as thinking_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(billing_router, prefix="/wallet", tags=["wallet"])
api_router.include_router(favorites_router, tags=["favorites"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
api_router.include_router(agent_runtime_router, prefix="/agent", tags=["agent"])
api_router.include_router(agent_artifacts_router, prefix="/agent", tags=["agent"])
# 前端 ChatArea 仍通过 /sessions/{id}/events 消费 thinking 流（Task 24 不删除 thinking）。
api_router.include_router(thinking_router, prefix="/sessions", tags=["sessions"])
