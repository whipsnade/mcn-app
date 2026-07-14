from typing import Literal

from pydantic import BaseModel, Field


class SmsCodeRequest(BaseModel):
    phone: str


class SmsCodeResponse(BaseModel):
    mock_code: str
    expires_in: int = 300


class SmsLoginRequest(BaseModel):
    phone: str
    code: str = Field(min_length=6, max_length=6)


class WechatLoginRequest(BaseModel):
    mock_ticket: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class UserRead(BaseModel):
    id: str
    nickname: str
    role: Literal["user", "admin"]
    channels: list[str]
