from pydantic import BaseModel, ConfigDict


class WalletRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    balance: int
    reserved: int
    available: int
