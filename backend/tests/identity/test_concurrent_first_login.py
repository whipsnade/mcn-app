"""并发首登唯一键冲突的幂等收敛（顺序模拟竞争输家路径）。

生产场景：同一手机号两个并发首登请求，`auth_identities (provider, subject)`
唯一键让其中一个 INSERT 失败。输家必须回滚本事务后重读既有身份，按已存在
用户正常返回登录会话——不重试 INSERT、不造重复用户、欢迎赠送只归首建者。
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.identity.models import AuthIdentity, LoginSession
from app.identity.service import IdentityService

PHONE = "13712345678"


@pytest.mark.asyncio
async def test_concurrent_first_login_loser_converges_on_existing_user(
    db_session, monkeypatch
) -> None:
    # 第一登（首建者）：真实建户路径，随后提交 savepoint 模拟「另一请求已提交」。
    first = await IdentityService(db_session).login(
        provider="sms", subject=PHONE, nickname="手机用户_5678"
    )
    await db_session.commit()

    # 竞争输家：初始查找被强制 miss（另一事务未可见），INSERT 撞唯一键。
    real_find = IdentityService._find_identity
    seen: dict[str, bool] = {"missed": False}

    async def miss_once(*args, **kwargs):
        if not seen["missed"]:
            seen["missed"] = True
            return None
        return await real_find(*args, **kwargs)

    async def conflict(*args, **kwargs):
        raise IntegrityError("insert auth_identities", {}, Exception("Duplicate entry"))

    monkeypatch.setattr(IdentityService, "_find_identity", miss_once)
    monkeypatch.setattr(IdentityService, "_create_user", conflict)

    second = await IdentityService(db_session).login(
        provider="sms", subject=PHONE, nickname="手机用户_5678"
    )

    # 幂等收敛：同一用户、正常登录会话。
    assert second.user.id == first.user.id
    assert second.access_token
    assert second.refresh_token

    # 不重试 INSERT、不造重复用户：本 subject 的身份恰一行（测试库存在其他
    # 历史身份数据，计数必须限定到本测试的 subject）。
    identity_count = await db_session.scalar(
        select(func.count())
        .select_from(AuthIdentity)
        .where(
            AuthIdentity.provider == "sms",
            AuthIdentity.provider_subject == PHONE,
        )
    )
    assert identity_count == 1
    identity = await db_session.scalar(
        select(AuthIdentity).where(
            AuthIdentity.provider == "sms",
            AuthIdentity.provider_subject == PHONE,
        )
    )
    assert identity is not None and identity.user_id == first.user.id

    # 两次登录各有一条会话记录（收敛者按已存在用户正常发会话）。
    session_count = await db_session.scalar(
        select(func.count()).select_from(LoginSession).where(LoginSession.user_id == first.user.id)
    )
    assert session_count == 2
