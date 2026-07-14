from pydantic import BaseModel, ConfigDict


class WalletRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    balance: int
    reserved: int

    @property
    def available(self) -> int:
        return self.balance
