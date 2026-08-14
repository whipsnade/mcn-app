"""Pi POC SQLite 测试的最小启动配置（只含非敏感占位值）。

pytest 会在收集测试模块前加载本文件，避免导入运行时工具时意外读取本机 `.env`。
所有测试仍显式构造 SQLite 内存库与随机 Settings，不连接任何 MySQL 数据库。
"""

import os
import secrets

import pytest
from pydantic import SecretStr

from app.core.config import Settings

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MYSQL_DATABASE", "kol_insight_pi_poc")
os.environ.setdefault("MYSQL_USER", "pi")
os.environ.setdefault("MYSQL_PASSWORD", "pi")
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-characters-12345678")
os.environ.setdefault("TENCENT_PLAN_API_KEY", "placeholder")
os.environ.setdefault("DATATAP_MCP_TOKEN", "placeholder")


@pytest.fixture
def settings() -> Settings:
    """每例独立随机凭证；仅供 SQLite POC 测试签发临时 Run token。"""
    return Settings(
        _env_file=None,
        app_env="test",
        mysql_database="kol_insight_pi_poc",
        mysql_password=SecretStr(secrets.token_urlsafe(24)),
        jwt_secret=SecretStr(secrets.token_urlsafe(32)),
        tencent_plan_api_key=SecretStr(secrets.token_urlsafe(24)),
        datatap_mcp_token=SecretStr(secrets.token_urlsafe(24)),
        pi_runtime_poc_enabled=True,
        pi_runtime_poc_internal_secret=SecretStr(secrets.token_urlsafe(32)),
    )
