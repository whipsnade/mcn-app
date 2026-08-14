from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.schemas import WalletRead
from app.billing.service import WalletService
from app.db.session import get_db
from app.identity.dependencies import CurrentUser


router = APIRouter()


@router.get("", response_model=WalletRead)
async def get_wallet(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WalletRead:
    service = WalletService(db)
    wallet = await service.get_wallet(user.id)
    return WalletRead(
        balance=wallet.balance,
        reserved=wallet.reserved,
        available=await service.available_points(user.id),
    )
