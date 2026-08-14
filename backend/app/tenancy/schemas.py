from typing import Literal

from pydantic import BaseModel, ConfigDict


class TenantContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    membership_role: Literal["owner", "admin", "member"]
