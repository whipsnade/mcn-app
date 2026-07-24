from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
    industries: list[str]


class BrandProfileItem(BaseModel):
    brand_name: str
    is_default: bool


class BrandProfileList(BaseModel):
    items: list[BrandProfileItem]


class BrandProfileDefaultSet(BaseModel):
    brand_name: str = Field(min_length=1, max_length=100)

    @field_validator("brand_name")
    @classmethod
    def _strip_and_require_nonblank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("brand_name_empty")
        return stripped
