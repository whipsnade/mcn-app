"""登录路由对事务级 DB 错误（1305/1213 类）的单次幂等重试。"""

import pytest
from sqlalchemy.exc import OperationalError

from app.identity import router as identity_router
from app.identity.service import IdentityService

PHONE = "13799990001"


def _mysql_error(code: int) -> OperationalError:
    # 真实 asyncmy 错误的 args[0] 为 int 错误码
    return OperationalError("INSERT ...", {}, Exception(code, "transient"))


@pytest.mark.asyncio
async def test_transient_savepoint_error_is_retried_once_and_converges(
    db_session, monkeypatch
) -> None:
    """1305（SAVEPOINT 消失，锁竞争使事务被隐式回滚的表层形态）首登失败后，
    回滚会话并单次重试：第二次按既有/新建用户正常返回，不产生重复数据。"""
    real_login = IdentityService.login
    calls = {"n": 0}

    async def fail_once_then_real(self, *, provider, subject, nickname):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _mysql_error(1305)
        return await real_login(self, provider=provider, subject=subject, nickname=nickname)

    monkeypatch.setattr(IdentityService, "login", fail_once_then_real)

    result = await identity_router.login_with_retry(
        db_session, provider="sms", subject=PHONE, nickname="手机用户_0001"
    )

    assert calls["n"] == 2
    assert result.user.id
    assert result.access_token


@pytest.mark.asyncio
async def test_deadlock_error_is_retried_once(db_session, monkeypatch) -> None:
    real_login = IdentityService.login
    calls = {"n": 0}

    async def fail_once_then_real(self, *, provider, subject, nickname):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _mysql_error(1213)
        return await real_login(self, provider=provider, subject=subject, nickname=nickname)

    monkeypatch.setattr(IdentityService, "login", fail_once_then_real)

    result = await identity_router.login_with_retry(
        db_session, provider="sms", subject="13799990002", nickname="手机用户_0002"
    )
    assert calls["n"] == 2
    assert result.user.id


@pytest.mark.asyncio
async def test_non_transient_operational_error_is_not_retried(db_session, monkeypatch) -> None:
    async def always_fail(self, *, provider, subject, nickname):
        raise _mysql_error(2003)

    monkeypatch.setattr(IdentityService, "login", always_fail)

    with pytest.raises(OperationalError):
        await identity_router.login_with_retry(
            db_session, provider="sms", subject="13799990003", nickname="x"
        )


@pytest.mark.asyncio
async def test_retry_happens_at_most_once(db_session, monkeypatch) -> None:
    """第二次仍失败则原样上抛：绝不第三次重试。"""
    calls = {"n": 0}

    async def always_deadlock(self, *, provider, subject, nickname):
        calls["n"] += 1
        raise _mysql_error(1213)

    monkeypatch.setattr(IdentityService, "login", always_deadlock)

    with pytest.raises(OperationalError):
        await identity_router.login_with_retry(
            db_session, provider="sms", subject="13799990004", nickname="x"
        )
    assert calls["n"] == 2
