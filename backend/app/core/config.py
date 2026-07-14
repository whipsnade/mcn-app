from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def database_url(self) -> str:
        password = quote_plus(self.mysql_password.get_secret_value())
        return (
            f"mysql+asyncmy://{self.mysql_user}:{password}@"
            f"{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    @model_validator(mode="after")
    def reject_mock_auth_in_production(self) -> "Settings":
        if self.app_env == "production" and self.auth_mode == "mock":
            raise ValueError("AUTH_MODE=mock is forbidden in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
