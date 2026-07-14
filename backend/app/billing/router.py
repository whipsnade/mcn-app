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
    wallet = await WalletService(db).get_wallet(user.id)
    return WalletRead(balance=wallet.balance, reserved=wallet.reserved, available=wallet.balance)
