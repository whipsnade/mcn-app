from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db
from app.identity.models import LoginSession, User


bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    return await resolve_current_user(credentials, db)


async def get_function_scoped_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> User:
    return await resolve_current_user(credentials, db)


async def resolve_current_user(
    credentials: HTTPAuthorizationCredentials | None, db: AsyncSession
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="AUTH_EXPIRED")
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="AUTH_EXPIRED"
        ) from error
    user = await db.get(User, payload.get("sub"))
    login_session = await db.get(LoginSession, payload.get("sid"))
    if (
        user is None
        or user.status != "active"
        or login_session is None
        or login_session.user_id != user.id
        or login_session.revoked_at is not None
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="AUTH_EXPIRED")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
FunctionScopedCurrentUser = Annotated[
    User, Depends(get_function_scoped_current_user, scope="function")
]
