from fastapi import APIRouter

from app.admin.router import router as admin_router
from app.billing.router import router as billing_router
from app.brainstorm.router import router as brainstorm_router
from app.identity.router import auth_router, users_router
from app.quick.router import router as quick_router
from app.reporting.router import router as reporting_router
from app.selection.router import router as selection_router
from app.tasks.router import router as tasks_router
from app.workspace.router import router as workspace_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(billing_router, prefix="/wallet", tags=["wallet"])
api_router.include_router(workspace_router, prefix="/sessions", tags=["sessions"])
api_router.include_router(brainstorm_router, prefix="/sessions", tags=["sessions"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
api_router.include_router(quick_router, prefix="/quick", tags=["quick"])
api_router.include_router(tasks_router, tags=["tasks"])
api_router.include_router(reporting_router, tags=["reporting"])
api_router.include_router(selection_router, tags=["selection"])
