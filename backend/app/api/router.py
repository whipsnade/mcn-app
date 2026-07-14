from fastapi import APIRouter

from app.billing.router import router as billing_router
from app.identity.router import auth_router, users_router
from app.workspace.router import router as workspace_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(billing_router, prefix="/wallet", tags=["wallet"])
api_router.include_router(workspace_router, prefix="/sessions", tags=["sessions"])
