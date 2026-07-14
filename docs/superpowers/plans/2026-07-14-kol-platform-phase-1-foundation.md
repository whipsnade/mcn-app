# KOL 智能选人平台第一阶段实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 React 原型升级为连接 FastAPI 与 MySQL 的可运行纵向切片，完成模拟登录、新用户 1000 积分、用户数据隔离、历史会话和消息持久化，并保持现有界面风格。

**Architecture:** React 只负责视图和交互状态，通过 `/api/v1` REST API 使用后端能力；FastAPI 模块化单体通过 Identity、Billing、Workspace 服务访问 MySQL。认证使用短期 JWT 访问令牌和数据库持久化的 HttpOnly 刷新会话，积分和会话数据不再由浏览器 `localStorage` 充当真实数据源。

**Tech Stack:** React 19、TypeScript 5、Vite 6、Tailwind CSS 4、FastAPI、Pydantic 2、SQLAlchemy 2 Async、Alembic、asyncmy、MySQL 8、PyJWT、pytest、Playwright。

## Global Constraints

- 预期注册用户约 100 人，同时在线用户和同时执行任务均不超过 10。
- Python 使用 3.11 或 3.12；后端 API 前缀固定为 `/api/v1`。
- 本地数据库固定为 MySQL 8，数据库名为 `kol_insight`；真实密码只进入未提交的根目录 `.env`。
- 新用户首次创建时通过账本获得 1000 积分，幂等键为 `welcome-grant:{user_id}`。
- 每个 MCP 工具成功响应扣 10 积分；本阶段实现账本状态机，但不连接真实 DataTap。
- 开发和测试使用 `AUTH_MODE=mock`；生产环境检测到 mock 模式必须拒绝启动。
- Google Trends 不进入配置、渠道目录、工具白名单或测试数据。
- UI 保留原型三栏布局、Indigo/Slate 配色、Lucide 图标、紧凑按钮、Motion 动效和 Recharts 风格。
- 所有普通用户资源查询必须包含当前认证用户的 `user_id` 条件。
- 首阶段不实现真实短信、微信 OAuth、支付、Redis、Celery 或独立 Worker。

## 全系统交付阶段

1. **基础纵向切片（本计划）**：FastAPI、MySQL、模拟认证、钱包账本、用户隔离的会话和消息、前端真实 API 接入。
2. **任务与流式事件**：`analysis_tasks`、`task_events`、SSE、断线重放、任务恢复和取消。
3. **AI 与 Prompt 编排**：Model Adapter、Context Builder、JSON Schema Planner、计划校验和模型降级。
4. **DataTap MCP 与实时扣费**：工具目录、参数校验、并行批次、预留/结算/释放、幂等调用和原始响应审计。
5. **KOL 候选与 BI**：标准化、评分、排序、收藏、对比、报告版本、证据和右侧 BI 增量更新。
6. **管理与发布加固**：用户/权限/账本/调用审计、10 并发测试、视觉回归、安全检查和部署说明。

后续每个阶段开始前，在 `docs/superpowers/plans/` 下生成独立计划；前一阶段必须保持可运行和可回归。

---

## 文件结构

```text
backend/
├── pyproject.toml                     # Python 依赖和 pytest 配置
├── alembic.ini                        # MySQL 迁移入口
├── app/
│   ├── main.py                        # FastAPI 应用工厂与生命周期
│   ├── api/router.py                  # `/api/v1` 聚合路由
│   ├── core/config.py                 # 环境配置和生产安全校验
│   ├── core/errors.py                 # 统一业务错误码
│   ├── core/security.py               # JWT、刷新令牌和 Cookie
│   ├── db/base.py                     # SQLAlchemy Declarative Base
│   ├── db/session.py                  # AsyncEngine 与请求事务
│   ├── db/models.py                   # Alembic 模型导入入口
│   ├── identity/models.py             # 用户、身份、登录会话、渠道权限
│   ├── identity/schemas.py            # 登录与当前用户 DTO
│   ├── identity/providers.py          # Mock 短信/微信认证提供者
│   ├── identity/service.py            # 用户创建、刷新和撤销
│   ├── identity/dependencies.py       # current_user/admin 依赖
│   ├── identity/router.py             # auth 与 users/me API
│   ├── billing/models.py              # 钱包和不可变账本
│   ├── billing/schemas.py             # 钱包 DTO
│   ├── billing/service.py             # 赠送、预留、结算、释放
│   ├── billing/router.py              # wallet API
│   ├── workspace/models.py            # 会话和消息
│   ├── workspace/schemas.py           # 会话/消息 DTO
│   ├── workspace/service.py           # 用户隔离的 CRUD
│   └── workspace/router.py            # sessions/messages API
├── migrations/
│   ├── env.py                         # 异步 Alembic 环境
│   └── versions/                      # 可重复执行的版本迁移
└── tests/
    ├── conftest.py                    # MySQL 测试库、事务和客户端
    ├── test_health.py
    ├── billing/test_wallet_service.py
    ├── identity/test_mock_auth.py
    └── workspace/test_sessions.py

src/
├── api/client.ts                      # 带刷新重试的 fetch 客户端
├── api/contracts.ts                   # 与 FastAPI 对齐的 DTO
├── api/auth.ts                        # 登录/刷新/登出
├── api/wallet.ts                      # 当前钱包
├── api/sessions.ts                    # 会话与消息 API
├── auth/AuthProvider.tsx              # 内存访问令牌与启动恢复
├── App.tsx                            # 移除业务 localStorage，组合服务端状态
├── components/LoginPage.tsx           # 保留视觉，事件改调真实 mock API
├── components/SessionList.tsx         # 使用服务端用户、角色和积分
├── components/NewSessionModal.tsx     # 字段语义改为 KOL 选人条件
└── types.ts                           # 前端领域类型与兼容映射
```

### Task 1: 建立可启动的 FastAPI 骨架

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/app/api/router.py`
- Create: `backend/app/core/config.py`
- Create: `backend/app/core/errors.py`
- Create: `backend/app/db/base.py`
- Create: `backend/app/db/session.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_health.py`
- Modify: `.env.example`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `create_app() -> FastAPI`、`Settings`、`get_db() -> AsyncIterator[AsyncSession]`、`GET /healthz`。
- Consumes: 根目录 `.env` 中的 MySQL 和 JWT 配置。

- [ ] **Step 1: 写健康检查失败测试**

```python
# backend/tests/test_health.py
from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_returns_service_status() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "kol-insight-api"}
```

- [ ] **Step 2: 运行测试并确认因 `app.main` 不存在而失败**

Run: `cd backend && python -m pytest tests/test_health.py -q`

Expected: `ModuleNotFoundError: No module named 'app'`。

- [ ] **Step 3: 创建依赖、配置与应用工厂**

```toml
# backend/pyproject.toml
[project]
name = "kol-insight-api"
version = "0.1.0"
requires-python = ">=3.11,<3.13"
dependencies = [
  "alembic>=1.14,<2",
  "asyncmy>=0.2,<1",
  "fastapi>=0.115,<1",
  "pydantic-settings>=2.7,<3",
  "PyJWT>=2.10,<3",
  "sqlalchemy>=2.0,<3",
  "uvicorn[standard]>=0.34,<1",
]

[project.optional-dependencies]
dev = [
  "httpx>=0.28,<1",
  "pytest>=8.3,<10",
  "pytest-asyncio>=0.25,<2",
  "ruff>=0.9,<1",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

```python
# backend/app/core/config.py
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
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
```

```python
# backend/app/db/base.py
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

```python
# backend/app/db/session.py
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=5,
)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

```python
# backend/app/api/router.py
from fastapi import APIRouter

api_router = APIRouter(prefix="/api/v1")
```

```python
# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="KOL Insight API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "kol-insight-api"}

    app.include_router(api_router)
    return app


app = create_app()
```

`backend/tests/conftest.py` 在导入应用前通过 `os.environ.setdefault` 设置测试数据库名 `kol_insight_test`、非真实测试密码和至少 32 字符测试 JWT；根 `.env.example` 新增同名占位字段，不写入任何真实凭证。

- [ ] **Step 4: 安装依赖并运行测试**

Run: `cd backend && python -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest tests/test_health.py -q`

Expected: `1 passed`。

- [ ] **Step 5: 提交后端骨架**

```bash
git add .env.example .gitignore backend
git commit -m "feat: scaffold FastAPI backend"
```

### Task 2: 建立身份、钱包、会话和消息数据模型

**Files:**
- Create: `backend/alembic.ini`
- Create: `backend/migrations/env.py`
- Create: `backend/migrations/script.py.mako`
- Create: `backend/app/db/models.py`
- Create: `backend/app/identity/models.py`
- Create: `backend/app/billing/models.py`
- Create: `backend/app/workspace/models.py`
- Create: `backend/tests/test_schema.py`
- Create: `backend/migrations/versions/0001_identity_wallet_workspace.py`

**Interfaces:**
- Produces: `User`、`AuthIdentity`、`LoginSession`、`UserChannelPermission`、`Wallet`、`WalletTransaction`、`WorkspaceSession`、`Message`。
- Consumes: `Base` 与 MySQL `utf8mb4` 数据库。

- [ ] **Step 1: 写模型表集合失败测试**

```python
# backend/tests/test_schema.py
from app.db.base import Base
import app.db.models  # noqa: F401


def test_phase_one_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "users",
        "auth_identities",
        "user_sessions",
        "user_channel_permissions",
        "wallets",
        "wallet_transactions",
        "sessions",
        "messages",
    }
```

- [ ] **Step 2: 运行测试并确认缺少模型模块**

Run: `cd backend && .venv/bin/pytest tests/test_schema.py -q`

Expected: `ModuleNotFoundError: No module named 'app.db.models'`。

- [ ] **Step 3: 定义模型及约束**

所有主键使用 `CHAR(36)` UUID；积分使用整数；金额预算使用 `DECIMAL(12,2)`；外部筛选快照和消息元数据使用 MySQL `JSON`。关键约束固定如下：

```python
# backend/app/billing/models.py
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_wallet_balance_nonnegative"),
        CheckConstraint("reserved >= 0", name="ck_wallet_reserved_nonnegative"),
    )

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_wallet_tx_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    balance_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_after: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
```

`identity/models.py` 必须为 `(provider, provider_subject)` 建唯一键；`workspace/models.py` 必须为 `sessions.user_id`、`messages.session_id` 建索引，为 `(session_id, sequence)` 建唯一键。`app/db/models.py` 显式导入三个领域模型模块，使 Alembic 能读取完整 metadata。

- [ ] **Step 4: 创建并检查初始迁移**

Run: `cd backend && .venv/bin/alembic revision --autogenerate -m 'identity wallet workspace'`

将生成文件重命名为 `0001_identity_wallet_workspace.py`，确认 `upgrade()` 创建八张表及上述唯一键、外键、检查约束和索引；`downgrade()` 按外键依赖逆序删除。

Run: `cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic current`

Expected: 当前版本为 `0001 (head)`。

- [ ] **Step 5: 运行模型测试并提交**

Run: `cd backend && .venv/bin/pytest tests/test_schema.py -q`

Expected: `1 passed`。

```bash
git add backend/app backend/migrations backend/alembic.ini backend/tests/test_schema.py
git commit -m "feat: add identity wallet and workspace schema"
```

### Task 3: 实现不可透支且幂等的钱包账本

**Files:**
- Create: `backend/app/billing/service.py`
- Create: `backend/app/billing/schemas.py`
- Create: `backend/tests/billing/test_wallet_service.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Produces: `WalletService.ensure_welcome_grant()`、`reserve()`、`settle()`、`release()`、`get_wallet()`。
- Consumes: `Wallet`、`WalletTransaction` 和一个调用方管理的 `AsyncSession` 事务。

- [ ] **Step 1: 写欢迎积分和调用计费状态机失败测试**

```python
# backend/tests/billing/test_wallet_service.py
import pytest

from app.billing.service import InsufficientPointsError, WalletService


@pytest.mark.asyncio
async def test_welcome_grant_and_call_lifecycle(db_session, user_factory) -> None:
    user = await user_factory()
    service = WalletService(db_session)

    await service.ensure_welcome_grant(user.id)
    await service.ensure_welcome_grant(user.id)
    wallet = await service.get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (1000, 0)

    await service.reserve(user.id, 10, "mcp:call-1:reserve", "call-1")
    wallet = await service.get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 10)

    await service.settle(user.id, 10, "mcp:call-1:settle", "call-1")
    wallet = await service.get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)

    await service.reserve(user.id, 10, "mcp:call-2:reserve", "call-2")
    await service.release(user.id, 10, "mcp:call-2:release", "call-2")
    wallet = await service.get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)


@pytest.mark.asyncio
async def test_reserve_rejects_insufficient_balance(db_session, user_factory) -> None:
    user = await user_factory()
    service = WalletService(db_session)
    await service.ensure_welcome_grant(user.id)

    with pytest.raises(InsufficientPointsError):
        await service.reserve(user.id, 1010, "mcp:too-large:reserve", "too-large")
```

- [ ] **Step 2: 运行测试并确认服务不存在**

Run: `cd backend && .venv/bin/pytest tests/billing/test_wallet_service.py -q`

Expected: 导入 `app.billing.service` 失败。

- [ ] **Step 3: 实现行锁、幂等键和原子状态变更**

先在 `backend/tests/conftest.py` 增加真实 MySQL 事务夹具。每个测试绑定一个外层连接事务并在结束时回滚；`user_factory` 创建最小合法 `User` 后 `flush()`，不提交测试数据：

```python
@pytest_asyncio.fixture
async def db_session():
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest_asyncio.fixture
async def user_factory(db_session):
    async def create_user() -> User:
        now = datetime.now(UTC)
        user = User(
            id=str(uuid4()),
            nickname="测试用户",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return create_user
```

```python
# backend/app/billing/service.py
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import Wallet, WalletTransaction


class InsufficientPointsError(Exception):
    pass


class WalletService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_wallet(self, user_id: str, *, for_update: bool = False) -> Wallet:
        statement = select(Wallet).where(Wallet.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        wallet = await self.db.scalar(statement)
        if wallet is None:
            raise LookupError("wallet_not_found")
        return wallet

    async def _already_applied(self, idempotency_key: str) -> bool:
        statement = select(WalletTransaction.id).where(
            WalletTransaction.idempotency_key == idempotency_key
        )
        return await self.db.scalar(statement) is not None

    async def _record(
        self,
        wallet: Wallet,
        kind: str,
        balance_delta: int,
        reserved_delta: int,
        idempotency_key: str,
        reference_id: str,
    ) -> Wallet:
        wallet.balance += balance_delta
        wallet.reserved += reserved_delta
        wallet.version += 1
        wallet.updated_at = datetime.now(UTC)
        self.db.add(
            WalletTransaction(
                id=str(uuid4()),
                user_id=wallet.user_id,
                kind=kind,
                balance_delta=balance_delta,
                reserved_delta=reserved_delta,
                balance_after=wallet.balance,
                reserved_after=wallet.reserved,
                idempotency_key=idempotency_key,
                reference_type="mcp_call" if kind != "welcome_grant" else "user",
                reference_id=reference_id,
                created_at=datetime.now(UTC),
            )
        )
        await self.db.flush()
        return wallet

    async def ensure_welcome_grant(self, user_id: str) -> Wallet:
        key = f"welcome-grant:{user_id}"
        wallet = await self.db.get(Wallet, user_id)
        if wallet is None:
            wallet = Wallet(
                user_id=user_id, balance=0, reserved=0, version=0, updated_at=datetime.now(UTC)
            )
            self.db.add(wallet)
            await self.db.flush()
        if await self._already_applied(key):
            return wallet
        wallet = await self.get_wallet(user_id, for_update=True)
        return await self._record(wallet, "welcome_grant", 1000, 0, key, user_id)

    async def reserve(self, user_id: str, amount: int, key: str, reference_id: str) -> Wallet:
        if await self._already_applied(key):
            return await self.get_wallet(user_id)
        wallet = await self.get_wallet(user_id, for_update=True)
        if amount <= 0 or wallet.balance < amount:
            raise InsufficientPointsError()
        return await self._record(wallet, "reserve", -amount, amount, key, reference_id)

    async def settle(self, user_id: str, amount: int, key: str, reference_id: str) -> Wallet:
        if await self._already_applied(key):
            return await self.get_wallet(user_id)
        wallet = await self.get_wallet(user_id, for_update=True)
        if amount <= 0 or wallet.reserved < amount:
            raise ValueError("invalid_reserved_amount")
        return await self._record(wallet, "settle", 0, -amount, key, reference_id)

    async def release(self, user_id: str, amount: int, key: str, reference_id: str) -> Wallet:
        if await self._already_applied(key):
            return await self.get_wallet(user_id)
        wallet = await self.get_wallet(user_id, for_update=True)
        if amount <= 0 or wallet.reserved < amount:
            raise ValueError("invalid_reserved_amount")
        return await self._record(wallet, "release", amount, -amount, key, reference_id)
```

- [ ] **Step 4: 运行钱包测试**

Run: `cd backend && .venv/bin/pytest tests/billing/test_wallet_service.py -q`

Expected: 所有测试通过，重复欢迎赠送后余额仍为 1000。

- [ ] **Step 5: 提交钱包账本**

```bash
git add backend/app/billing backend/tests/billing
git commit -m "feat: add idempotent points ledger"
```

### Task 4: 实现模拟短信/微信登录和可恢复登录会话

**Files:**
- Create: `backend/app/core/security.py`
- Create: `backend/app/identity/providers.py`
- Create: `backend/app/identity/schemas.py`
- Create: `backend/app/identity/service.py`
- Create: `backend/app/identity/dependencies.py`
- Create: `backend/app/identity/router.py`
- Create: `backend/app/billing/router.py`
- Create: `backend/tests/identity/test_mock_auth.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Produces: `POST /api/v1/auth/mock/sms/code`、`POST /api/v1/auth/mock/sms/login`、`POST /api/v1/auth/mock/wechat/login`、`POST /api/v1/auth/refresh`、`POST /api/v1/auth/logout`、`GET /api/v1/users/me`、`GET /api/v1/wallet`。
- Consumes: `WalletService.ensure_welcome_grant()`；刷新 Cookie 名固定为 `kol_refresh`。

- [ ] **Step 1: 写模拟登录、刷新和赠送积分失败测试**

```python
# backend/tests/identity/test_mock_auth.py
import pytest


@pytest.mark.asyncio
async def test_new_sms_user_can_refresh_and_receives_1000_points(client) -> None:
    code_response = await client.post(
        "/api/v1/auth/mock/sms/code", json={"phone": "13812345678"}
    )
    assert code_response.status_code == 200
    assert code_response.json()["mock_code"] == "000000"

    login = await client.post(
        "/api/v1/auth/mock/sms/login",
        json={"phone": "13812345678", "code": "000000"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert "kol_refresh" in login.cookies

    me = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    wallet = await client.get("/api/v1/wallet", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["nickname"] == "手机用户_5678"
    assert wallet.json() == {"balance": 1000, "reserved": 0, "available": 1000}

    refreshed = await client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != token
```

- [ ] **Step 2: 运行测试并确认路由返回 404**

Run: `cd backend && .venv/bin/pytest tests/identity/test_mock_auth.py -q`

Expected: 首个认证端点返回 `404`。

- [ ] **Step 3: 实现固定开发验证码、JWT 与刷新令牌轮换**

```python
# backend/app/identity/providers.py
import re


class MockSmsAuthProvider:
    code = "000000"

    def request_code(self, phone: str) -> str:
        if re.fullmatch(r"1[3-9]\d{9}", phone) is None:
            raise ValueError("invalid_phone")
        return self.code

    def verify(self, phone: str, code: str) -> tuple[str, str]:
        if code != self.code:
            raise ValueError("invalid_code")
        return phone, f"手机用户_{phone[-4:]}"


class MockWechatAuthProvider:
    def verify(self, mock_ticket: str) -> tuple[str, str]:
        if mock_ticket != "mock-wechat-authorized":
            raise ValueError("invalid_wechat_ticket")
        return "mock-wechat-user", "微信快捷登录用户"
```

`core/security.py` 必须使用 `secrets.token_urlsafe(48)` 生成刷新令牌，只把 SHA-256 摘要写入 `user_sessions`；JWT 包含 `sub`、`sid`、`role`、`iat`、`exp`、`jti`。刷新时锁定登录会话、撤销旧记录、创建新记录并轮换 Cookie，登出时撤销当前刷新会话。

Identity Service 的用户创建事务顺序固定为：验证 provider → 按 `(provider, provider_subject)` 查询身份 → 新建用户与身份（如不存在）→ 创建渠道权限（小红书、抖音、B站）→ 调用欢迎积分服务 → 创建登录会话 → 签发访问令牌。重复登录不能再次赠送积分。

- [ ] **Step 4: 聚合路由并运行认证测试**

在 `backend/tests/conftest.py` 增加 `httpx.AsyncClient`：用 `ASGITransport(app=create_app())` 启动应用，并通过 `app.dependency_overrides[get_db]` 让 API 和断言共享测试事务；每个 client 拥有独立 CookieJar，避免不同模拟用户串用刷新 Cookie。

```python
# backend/app/api/router.py
from fastapi import APIRouter

from app.billing.router import router as billing_router
from app.identity.router import auth_router, users_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(billing_router, prefix="/wallet", tags=["wallet"])
```

Run: `cd backend && .venv/bin/pytest tests/identity/test_mock_auth.py -q`

Expected: 模拟登录、1000 积分、刷新和登出测试全部通过。

- [ ] **Step 5: 提交认证模块**

```bash
git add backend/app/core backend/app/identity backend/app/billing/router.py backend/app/api backend/tests/identity
git commit -m "feat: add mock auth and persistent login sessions"
```

### Task 5: 实现用户隔离的会话和消息历史

**Files:**
- Create: `backend/app/workspace/schemas.py`
- Create: `backend/app/workspace/service.py`
- Create: `backend/app/workspace/router.py`
- Create: `backend/tests/workspace/test_sessions.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Produces: `POST/GET /api/v1/sessions`、`GET/PATCH /api/v1/sessions/{id}`、`POST /api/v1/sessions/{id}/messages`。
- Consumes: `get_current_user()`；所有 service 方法第一个领域参数为 `user_id`。

- [ ] **Step 1: 写跨用户隔离和完整恢复失败测试**

```python
# backend/tests/workspace/test_sessions.py
import pytest


@pytest.mark.asyncio
async def test_session_history_is_owned_and_restorable(auth_client_factory) -> None:
    alice = await auth_client_factory("13800000001")
    bob = await auth_client_factory("13800000002")

    created = await alice.post(
        "/api/v1/sessions",
        json={
            "brand": "示例品牌",
            "campaign_name": "夏季防晒选人",
            "platforms": ["xiaohongshu", "douyin"],
            "category": "美妆护肤",
            "target_audience": "18-30 岁一二线女性",
            "budget_min": "30000.00",
            "budget_max": "80000.00",
            "initial_query": "寻找兼顾成分科普和转化的达人",
        },
    )
    session_id = created.json()["id"]

    message = await alice.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "互动率至少 4%，排除近期负面达人"},
    )
    assert message.status_code == 201

    restored = await alice.get(f"/api/v1/sessions/{session_id}")
    assert [item["content"] for item in restored.json()["messages"]] == [
        "寻找兼顾成分科普和转化的达人",
        "互动率至少 4%，排除近期负面达人",
    ]
    assert (await bob.get(f"/api/v1/sessions/{session_id}")).status_code == 404
```

- [ ] **Step 2: 运行测试并确认 sessions 路由不存在**

Run: `cd backend && .venv/bin/pytest tests/workspace/test_sessions.py -q`

Expected: 创建会话请求返回 `404`。

- [ ] **Step 3: 实现 DTO、CRUD 和所有权过滤**

`auth_client_factory(phone)` 必须新建独立 `AsyncClient`，调用短信登录后把返回的访问令牌写入该 client 的默认 `Authorization` header，并在测试结束时逐个关闭；不得让 Alice 和 Bob 复用 Cookie 或 header。

```python
# backend/app/workspace/service.py（核心查询约束）
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workspace.models import Message, WorkspaceSession


class WorkspaceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_owned_session(self, user_id: str, session_id: str) -> WorkspaceSession:
        statement = select(WorkspaceSession).where(
            WorkspaceSession.id == session_id,
            WorkspaceSession.user_id == user_id,
        )
        workspace = await self.db.scalar(statement)
        if workspace is None:
            raise LookupError("session_not_found")
        return workspace

    async def list_sessions(self, user_id: str) -> list[WorkspaceSession]:
        statement = (
            select(WorkspaceSession)
            .where(WorkspaceSession.user_id == user_id)
            .order_by(WorkspaceSession.last_accessed_at.desc())
        )
        return list((await self.db.scalars(statement)).all())
```

创建会话时在同一事务插入 `sessions` 和 sequence=1 的用户消息；追加消息时锁定所属会话，读取当前最大 sequence 后加一；详情端点按 sequence 升序返回完整消息。PATCH 只允许修改标题、品牌、活动名、筛选条件、收藏和归档状态，禁止修改 `user_id`。

- [ ] **Step 4: 运行 Workspace 测试**

Run: `cd backend && .venv/bin/pytest tests/workspace/test_sessions.py -q`

Expected: 两个用户只能访问自己的会话，消息顺序恢复一致。

- [ ] **Step 5: 提交 Workspace 模块**

```bash
git add backend/app/workspace backend/app/api/router.py backend/tests/workspace
git commit -m "feat: persist isolated user sessions and messages"
```

### Task 6: 建立前端 API 客户端和认证状态

**Files:**
- Create: `src/api/contracts.ts`
- Create: `src/api/client.ts`
- Create: `src/api/auth.ts`
- Create: `src/api/wallet.ts`
- Create: `src/auth/AuthProvider.tsx`
- Create: `src/auth/AuthProvider.test.tsx`
- Modify: `src/main.tsx`
- Modify: `src/components/LoginPage.tsx`
- Modify: `package.json`
- Modify: `vite.config.ts`

**Interfaces:**
- Produces: `apiClient.request<T>()`、`useAuth()`、`loginWithSms()`、`loginWithWechat()`、`logout()`。
- Consumes: 后端 HttpOnly `kol_refresh` Cookie 和短期访问令牌。

- [ ] **Step 1: 建立前端测试运行器**

Run: `npm install -D vitest jsdom @testing-library/react @testing-library/jest-dom`

在 `package.json` 增加 `"test": "vitest run"`，在 `vite.config.ts` 的 test 配置中设置 `environment: 'jsdom'`。先运行 `npm run test -- --passWithNoTests`，Expected: 命令成功且报告没有测试文件。

- [ ] **Step 2: 写启动刷新和短信登录失败测试**

```tsx
// src/auth/AuthProvider.test.tsx
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { AuthProvider, useAuth } from './AuthProvider';

function Probe() {
  const auth = useAuth();
  return <span>{auth.status}:{auth.user?.nickname ?? 'none'}</span>;
}

describe('AuthProvider', () => {
  it('restores a session with the refresh cookie', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: 'new-token' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: 'user-1', nickname: '手机用户_5678', role: 'user', channels: ['xiaohongshu']
      }), { status: 200 })));

    render(<AuthProvider><Probe /></AuthProvider>);

    await waitFor(() => expect(screen.getByText('authenticated:手机用户_5678')).toBeTruthy());
  });
});
```

- [ ] **Step 3: 运行测试并确认模块不存在**

Run: `npm run test -- src/auth/AuthProvider.test.tsx`

Expected: 无法解析 `./AuthProvider`。

- [ ] **Step 4: 实现单次刷新重试的 API 客户端**

```typescript
// src/api/client.ts
let accessToken: string | null = null;
let refreshPromise: Promise<string | null> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

async function refreshAccessToken(): Promise<string | null> {
  if (!refreshPromise) {
    refreshPromise = fetch('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
    })
      .then(async response => {
        if (!response.ok) return null;
        const body = await response.json() as { access_token: string };
        setAccessToken(body.access_token);
        return body.access_token;
      })
      .finally(() => { refreshPromise = null; });
  }
  return refreshPromise;
}

export async function request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
  const response = await fetch(path, { ...init, headers, credentials: 'include' });
  if (response.status === 401 && retry && await refreshAccessToken()) {
    return request<T>(path, init, false);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ code: 'HTTP_ERROR' }));
    throw new Error(body.code ?? `HTTP_${response.status}`);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
```

`AuthProvider` 启动时只尝试一次 refresh；成功后请求 `/users/me`，失败则进入 anonymous。访问令牌仅保存在模块内存，不写入 `localStorage`。LoginPage 保留全部 CSS、图标和动效，只把随机验证码、自动微信计时登录和 `setTimeout` 登录替换为 `auth.ts` API；页面必须显示“开发模拟登录”标记和后端返回的 `mock_code`。

- [ ] **Step 5: 配置 Vite 代理并运行测试**

`vite.config.ts` 把 `/api` 代理到 `http://127.0.0.1:8000`，不改变现有 Tailwind 和 React 插件。

将 `package.json` 的主脚本切换为 Python 后端配套模式：`"dev": "vite"`、`"build": "vite build"`、`"dev:legacy": "tsx server.ts"`。`server.ts` 只保留为原型回看入口，不再作为正式 API 或默认开发服务器。

Run: `npm run test -- src/auth/AuthProvider.test.tsx && npm run lint`

Expected: 前端认证测试通过且 TypeScript 无错误。

- [ ] **Step 6: 提交前端认证接入**

```bash
git add package.json package-lock.json vite.config.ts src/api src/auth src/main.tsx src/components/LoginPage.tsx
git commit -m "feat: connect frontend authentication API"
```

### Task 7: 把会话、消息和积分从 localStorage 迁移到后端

**Files:**
- Create: `src/api/sessions.ts`
- Create: `src/api/sessions.test.ts`
- Create: `src/hooks/useWorkspace.ts`
- Modify: `src/App.tsx`
- Modify: `src/types.ts`
- Modify: `src/components/SessionList.tsx`
- Modify: `src/components/NewSessionModal.tsx`
- Modify: `src/components/ChatArea.tsx`

**Interfaces:**
- Produces: `useWorkspace()` 返回 `sessions`、`activeSession`、`createSession()`、`appendMessage()`、`updateSession()`、`reload()`。
- Consumes: Task 5 REST API 和 Task 6 `request<T>()`。

- [ ] **Step 1: 写会话 DTO 映射和服务调用失败测试**

```typescript
// src/api/sessions.test.ts
import { describe, expect, it } from 'vitest';
import { toSession } from './sessions';

describe('toSession', () => {
  it('maps server history without MCN fields', () => {
    const session = toSession({
      id: 's-1', title: '示例品牌-夏季选人', brand: '示例品牌',
      campaign_name: '夏季选人', status: 'draft', platforms: ['xiaohongshu'],
      category: '美妆护肤', target_audience: '18-30 岁女性',
      budget_min: '30000.00', budget_max: '80000.00', is_starred: false,
      messages: [{ id: 'm-1', role: 'user', content: '寻找达人', sequence: 1, created_at: '2026-07-14T10:00:00Z' }],
      created_at: '2026-07-14T10:00:00Z', updated_at: '2026-07-14T10:00:00Z'
    });

    expect(session.platform).toBe('Xiaohongshu');
    expect(session.messages[0].text).toBe('寻找达人');
    expect('mcn' in session).toBe(false);
  });
});
```

- [ ] **Step 2: 运行测试并确认 sessions API 模块不存在**

Run: `npm run test -- src/api/sessions.test.ts`

Expected: 无法解析 `./sessions`。

- [ ] **Step 3: 定义服务端 DTO 和兼容前端类型**

```typescript
// src/types.ts（替换 Session 核心字段）
export interface Session {
  id: string;
  title: string;
  brand: string;
  campaignName: string;
  status: 'completed' | 'analyzing' | 'draft' | 'archived';
  platform: string;
  category: string;
  targetAudience: string;
  budgetMin?: string;
  budgetMax?: string;
  summary: string;
  messages: Message[];
  reportData?: ReportData;
  isStarred: boolean;
  createdAt: string;
  updatedAt: string;
}
```

`src/api/sessions.ts` 实现 list/get/create/patch/appendMessage，并把平台 slug 映射为当前组件使用的显示枚举。`useWorkspace` 在登录后加载列表，选中会话时加载详情；创建会话和发送消息以服务端返回对象为准，不生成临时业务 ID。

`App.tsx` 删除 `kol_mcn_analyst_user`、`kol_mcn_analyst_sessions`、`kol_mcn_analyst_accounts` 和 `kol_analyst_points` 的读写；只允许保留 `activeSessionId` 作为非权威 UI 偏好。初始 `initialSessions` 不再作为已登录用户的数据源。充值弹窗继续展示“暂未开放”，不能改变余额。

管理入口只对 `role=admin` 显示；本阶段点击后展示受控的只读空状态，不加载原型中的模拟账户，也不能修改用户或积分。普通用户不渲染管理按钮。

`NewSessionModal` 将 `mcn`、预置 KOL 名称字段替换为品类、目标受众和预算区间；保持现有 modal 尺寸、按钮样式、Lucide 图标和 Motion 动效。`ChatArea` 标题和辅助信息移除“MCN”，显示渠道、品类和预算。

- [ ] **Step 4: 运行前端单测、类型检查和构建**

Run: `npm run test && npm run lint && npm run build`

Expected: 测试全部通过、TypeScript 0 错误、Vite 构建成功。

- [ ] **Step 5: 提交 Workspace 前端迁移**

```bash
git add src/App.tsx src/types.ts src/api/sessions.ts src/api/sessions.test.ts src/hooks src/components
git commit -m "feat: persist frontend workspace through API"
```

### Task 8: 完成端到端验收、文档与安全检查

**Files:**
- Create: `playwright.config.ts`
- Create: `e2e/auth-session-recovery.spec.ts`
- Create: `backend/tests/billing/test_wallet_concurrency.py`
- Modify: `package.json`
- Modify: `README.md`
- Modify: `.env.example`

**Interfaces:**
- Produces: 可重复执行的本地启动、迁移、测试和 E2E 命令。
- Consumes: 本阶段所有 API、前端页面和本地 MySQL。

- [ ] **Step 1: 安装 Playwright 测试运行器**

Run: `npm install -D @playwright/test && npx playwright install chromium`

在 `package.json` 增加 `"test:e2e": "playwright test"`。创建 `playwright.config.ts`，设置 `baseURL=http://127.0.0.1:5173`，并用两个 webServer 分别启动 `backend/.venv/bin/uvicorn app.main:app --app-dir backend --port 8000` 与 `npm run dev -- --host 127.0.0.1`。

- [ ] **Step 2: 写 E2E 恢复流程和钱包并发测试**

```typescript
// e2e/auth-session-recovery.spec.ts
import { expect, test } from '@playwright/test';

test('mock user receives points and restores a session after reload', async ({ page }) => {
  await page.goto('/');
  await page.getByPlaceholder('请输入手机号').fill('13812345678');
  await page.getByRole('button', { name: '获取验证码' }).click();
  await page.getByPlaceholder('请输入验证码').fill('000000');
  await page.getByRole('button', { name: '安全登录' }).click();

  await expect(page.getByText('1,000')).toBeVisible();
  await page.getByTitle('创建新任务').click();
  await page.getByLabel('品牌').fill('示例品牌');
  await page.getByLabel('任务名称').fill('夏季防晒选人');
  await page.getByRole('button', { name: '创建并分析' }).click();
  await expect(page.getByText('示例品牌 - 夏季防晒选人')).toBeVisible();

  await page.reload();
  await expect(page.getByText('示例品牌 - 夏季防晒选人')).toBeVisible();
});
```

并发测试创建余额为 10 的钱包，使用两个独立数据库连接同时调用 `reserve(..., 10, ...)`，断言恰好一个成功、一个抛出 `InsufficientPointsError`，最终 `balance=0`、`reserved=10`，账本只有一条 reserve。

- [ ] **Step 3: 运行并发测试并确认实现满足 MySQL 行锁语义**

Run: `cd backend && .venv/bin/pytest tests/billing/test_wallet_concurrency.py -q`

Expected: `1 passed`，无负余额、无重复账本。

- [ ] **Step 4: 完成本地运行文档**

README 必须给出以下顺序：

```bash
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS kol_insight CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE DATABASE IF NOT EXISTS kol_insight_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
cp .env.example .env
cd backend && python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cd backend && .venv/bin/alembic upgrade head
cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000
npm install
npm run dev
```

README 明确 `.env` 中的密码、JWT、未来 DataTap Token 均不得提交；`.env.example` 只保留占位符，并明确 `AUTH_MODE=mock` 只允许 development/test。

- [ ] **Step 5: 运行完整验证矩阵**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q`

Run: `npm run test && npm run lint && npm run build`

Run: `npm run test:e2e`

Run: `rg -n "dt_[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9_-]{20,}" --glob '!package-lock.json' --glob '!.git/**' .`

Run: `rg -n "Google Trends" src backend .env.example package.json vite.config.ts`

Expected: 后端检查与测试、前端测试/类型检查/构建、E2E 全部通过；敏感信息扫描和 Google Trends 扫描无结果（设计文档中的明确排除说明除外）。

- [ ] **Step 6: 人工视觉回归并提交第一阶段**

在 1440×900、1024×768、390×844 三个视口保存登录页和主工作区截图，对比原型确认 Indigo/Slate、Lucide、紧凑按钮、三栏/抽屉结构未漂移；确认普通用户不显示管理员按钮。

```bash
git add README.md .env.example package.json package-lock.json playwright.config.ts e2e backend/tests
git commit -m "test: verify foundation vertical slice"
```

## 第一阶段完成定义

- 模拟短信和微信登录由 Python 后端处理，刷新后能恢复登录。
- 新用户只获得一次 1000 积分，钱包和账本位于 MySQL。
- 两个用户不能读取或修改对方会话。
- 会话和完整消息历史在刷新页面及重启服务后恢复。
- 前端不再把用户、账户、钱包、会话或消息保存在 `localStorage` 作为真实数据。
- 充值入口不能直接改变积分。
- 后端测试、前端测试、类型检查、构建、E2E 和钱包并发测试全部通过。
- 现有原型的配色、按钮、图标、密度和主要布局保持一致。
