from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
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
    # OpenAI 兼容端点与模型名均可自由配置（腾讯 Token Plan、月之暗面 Kimi 等）。
    tencent_plan_model: str = "deepseek-v4-pro"
    # 思考深度（仅 Kimi k3 等推理模型支持，请求顶层 reasoning_effort）；
    # 为 None 时不发送该参数，避免不支持它的端点报 400。
    tencent_plan_reasoning_effort: Literal["low", "high", "max"] | None = None
    # Full-tool planning sends the reviewed MCP schemas in one request. The
    # provider can take longer than the default HTTP timeout to produce a
    # valid structured plan, so keep this configurable but use a safe default.
    model_timeout_seconds: float = Field(default=180.0, gt=0)
    # 单次模型决策的整次墙钟上限（每次 create 尝试，含整个流消费）：
    # model_timeout_seconds 是 httpx 读/写/连接级超时，流持续 trickle 时永不触发，
    # 一次决策可能长期挂死（UAT 中决策挂 3 分钟+）。该墙钟超时即取消本次尝试、
    # 按可重试 MODEL_TIMEOUT 在重试预算内重试。与 Run 租约（AGENT_LEASE_SECONDS
    # 300s、心跳每 lease/3 续租）的关系：决策有上界后心跳不再因单决策挂死而丢失，
    # 默认值 240s 须保持小于租约时长，使单次尝试在租约周期内必然收口。
    model_decision_timeout_seconds: float = Field(default=240.0, gt=0)
    datatap_mcp_token: SecretStr
    # 版本化 Runtime 配置的 AES-256-GCM master key ring；生产必须显式配置，
    # development/test 不提供时由使用方自行注入测试 cipher。
    runtime_secret_master_keys: SecretStr = SecretStr("")
    runtime_secret_active_key_version: str = "v1"
    # 方案 A Pi Extension 只能连接一个经显式配置的 DataTap MCP endpoint；不从
    # 宿主环境隐式继承或由代码猜测服务类型。
    datatap_mcp_url: AnyHttpUrl | None = None
    # DataTap 接入链接返回多服务 endpoint；只接受显式 slug → URL 映射，Pi 不在运行时
    # 猜测、拼接或发现服务 URL。旧的单 endpoint 留作兼容读取，POC 一律使用此映射。
    datatap_mcp_urls: dict[str, AnyHttpUrl] = Field(default_factory=dict)
    # DataTap 网关 origin；默认真实生产地址，仅测试/离线拓扑覆盖为 loopback。
    datatap_mcp_origin: str = "https://datatap.deepminer.com.cn"
    # DataTap 查询级读取超时：统计类查询通常一分钟内返回，超时按失败释放积分。
    datatap_read_timeout_seconds: float = Field(default=60.0, gt=0)
    # Agent 路径单次 MCP 调用外发墙钟上限（不含队列等待）：DataTap 统计查询可能
    # 持续 trickle 返回数据，httpx 读取超时（无活动超时）被不断重置而永不触发
    # （UAT Incident #8）。超过该上限按 result_unknown 收口：保留预留、进恢复
    # 核对、Run 继续后续工具。须小于 agent_tool_call_stuck_seconds。
    agent_mcp_call_timeout_seconds: float = Field(default=150.0, gt=0)
    mcp_call_points: int = 10
    mcp_unknown_reconcile_seconds: int = Field(default=300, gt=0)
    # Agent 工具调用 stuck 阈值：超过该时长仍处于 running/reserved 的调用由恢复
    # 循环迁移为 unknown 再核对（设计 §5.4）；须大于 DataTap 队列 + 读取超时。
    agent_tool_call_stuck_seconds: float = Field(default=900.0, gt=0)
    # 恢复循环扫描间隔（秒）；离线进程级 UAT 可调小以快速演练崩溃恢复。
    agent_recovery_interval_seconds: float = Field(default=30.0, gt=0)
    # 达人详情 Session 级缓存 TTL（设计 §8.1 kol_detail_cache）：默认 24 小时。
    kol_detail_cache_ttl_hours: int = Field(default=24, ge=1)
    # 用户上传存储目录与限制：仅 .csv/.xlsx，文件 20 MiB / 数据行 50,000 上限
    # （Gate B：安全上传与解析）。storage_key 由服务生成，绝不拼接用户文件名。
    agent_upload_storage_dir: str = ".data/agent-uploads"
    agent_export_storage_dir: str = ".data/agent-exports"
    agent_upload_max_bytes: int = Field(default=20971520, gt=0)
    agent_upload_max_rows: int = Field(default=50000, gt=0)
    task_lease_seconds: int = Field(default=60, gt=0)
    goal_planner_shadow_enabled: bool = False
    goal_planner_enforce_enabled: bool = False
    # Pi RPC POC 必须显式开启，并且仅可配合隔离数据库运行。
    pi_runtime_poc_enabled: bool = False
    pi_runtime_poc_internal_secret: SecretStr | None = None
    pi_runtime_poc_base_url: AnyHttpUrl = AnyHttpUrl(
        "http://127.0.0.1:8000/api/v1/internal/pi-poc"
    )
    pi_runtime_poc_run_timeout_seconds: int = Field(default=1800, gt=0)
    pi_runtime_poc_mcp_timeout_seconds: int = Field(default=180, gt=0)
    pi_runtime_poc_max_decisions: int = Field(default=50, gt=0)
    # 生产 Pi Gateway 内部协议：未配置时不开放任何内部路由。
    pi_gateway_internal_secret: SecretStr = SecretStr("")
    pi_gateway_allowed_ids: list[str] = Field(default_factory=list)
    pi_gateway_lease_seconds: int = Field(default=60, gt=0)
    pi_gateway_control_plane_url: AnyHttpUrl | None = None
    # Highest-priority local rollback switch: new Runs use current while
    # existing Pi Runs retain their immutable snapshot.
    pi_gateway_kill_switch: bool = False

    @field_validator("pi_gateway_allowed_ids", mode="before")
    @classmethod
    def parse_gateway_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("pi_gateway_control_plane_url", mode="before")
    @classmethod
    def empty_gateway_url_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def database_url(self) -> str:
        password = quote_plus(self.mysql_password.get_secret_value())
        return (
            f"mysql+asyncmy://{self.mysql_user}:{password}@"
            f"{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    @field_validator("datatap_mcp_url", mode="before")
    @classmethod
    def empty_datatap_endpoint_is_none(cls, value: object) -> object:
        """未配置的旧单 endpoint 不能让 POC 配置解析提前崩溃。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def datatap_endpoint(self, service: DataTapService) -> AnyHttpUrl:
        if not isinstance(service, DataTapService):
            raise TypeError("service must be a DataTapService")
        return AnyHttpUrl(
            f"https://datatap.deepminer.com.cn/api/gateway/{service.value}/mcp"
        )

    @model_validator(mode="after")
    def validate_runtime_contracts(self) -> "Settings":
        if self.mcp_call_points != 10:
            raise ValueError("MCP_CALL_POINTS must be 10")

        if not self.tencent_plan_api_key.get_secret_value().strip():
            raise ValueError("TENCENT_PLAN_API_KEY must not be blank")
        if not self.datatap_mcp_token.get_secret_value().strip():
            raise ValueError("DATATAP_MCP_TOKEN must not be blank")
        if self.app_env == "production" and self.auth_mode == "mock":
            raise ValueError("AUTH_MODE=mock is forbidden in production")
        if self.pi_gateway_control_plane_url is not None:
            url = self.pi_gateway_control_plane_url
            loopback_hosts = {"localhost", "127.0.0.1", "::1"}
            if url.scheme == "http" and (
                self.app_env not in {"development", "test"} or url.host not in loopback_hosts
            ):
                raise ValueError("PI_GATEWAY_CONTROL_PLANE_URL must use HTTPS or loopback HTTP")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
