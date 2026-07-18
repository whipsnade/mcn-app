from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.mcp_gateway.contracts import DataTapService


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    auth_mode: str = "mock"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "kol_insight"
    mysql_user: str = "root"
    mysql_password: SecretStr
    jwt_secret: SecretStr
    access_token_minutes: int = 30
    refresh_token_days: int = 30
    frontend_origin: str = "http://localhost:5173"
    tencent_plan_base_url: AnyHttpUrl = AnyHttpUrl(
        "https://tokenhub.tencentmaas.com/plan/v3"
    )
    tencent_plan_api_key: SecretStr
    tencent_plan_model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
    # Full-tool planning sends the reviewed MCP schemas in one request. The
    # provider can take longer than the default HTTP timeout to produce a
    # valid structured plan, so keep this configurable but use a safe default.
    model_timeout_seconds: float = Field(default=180.0, gt=0)
    datatap_mcp_token: SecretStr
    # DataTap may need several minutes to finish a social-data query. A read
    # timeout is deliberately long, while connection and pool timeouts remain
    # short in the transport.
    datatap_read_timeout_seconds: float = Field(default=300.0, gt=0)
    mcp_call_points: int = 10
    mcp_max_calls_per_task: int = 10
    mcp_unknown_reconcile_seconds: int = Field(default=300, gt=0)
    task_lease_seconds: int = Field(default=60, gt=0)

    @property
    def database_url(self) -> str:
        password = quote_plus(self.mysql_password.get_secret_value())
        return (
            f"mysql+asyncmy://{self.mysql_user}:{password}@"
            f"{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    def datatap_endpoint(self, service: DataTapService) -> AnyHttpUrl:
        if not isinstance(service, DataTapService):
            raise TypeError("service must be a DataTapService")
        return AnyHttpUrl(
            f"https://datatap.deepminer.com.cn/api/gateway/{service.value}/mcp"
        )

    @model_validator(mode="after")
    def validate_runtime_contracts(self) -> "Settings":
        if self.tencent_plan_base_url.unicode_string() != (
            "https://tokenhub.tencentmaas.com/plan/v3"
        ):
            raise ValueError("TENCENT_PLAN_BASE_URL must use the confirmed provider endpoint")
        if self.mcp_call_points != 10:
            raise ValueError("MCP_CALL_POINTS must be 10")
        if self.mcp_max_calls_per_task != 10:
            raise ValueError("MCP_MAX_CALLS_PER_TASK must be 10")

        if not self.tencent_plan_api_key.get_secret_value().strip():
            raise ValueError("TENCENT_PLAN_API_KEY must not be blank")
        if not self.datatap_mcp_token.get_secret_value().strip():
            raise ValueError("DATATAP_MCP_TOKEN must not be blank")
        if self.app_env == "production" and self.auth_mode == "mock":
            raise ValueError("AUTH_MODE=mock is forbidden in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
