from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.mcp_gateway.contracts import DataTapService


class PlanValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PlannerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern=r"^(system|user|assistant)$")
    content: str = Field(min_length=1, max_length=24_000)
    sequence: int = Field(ge=1)


class PlannerTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_id: str = Field(min_length=1)
    internal_name: str = Field(min_length=1, max_length=128)
    service: DataTapService
    description: str = Field(default="已审核工具", min_length=1, max_length=500)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_approved(cls, item: Any) -> "PlannerTool":
        return cls(
            catalog_id=item.catalog_id,
            internal_name=item.internal_name,
            service=item.service,
            description=getattr(item, "reviewed_description", item.internal_name),
            input_schema=item.input_schema,
            output_schema=getattr(item, "output_schema", {}),
        )
