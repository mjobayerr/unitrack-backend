"""Forgot-password and reset, and the ways each could leak or be abused.

A reset endpoint is reachable by someone who cannot log in — that is the whole
point — so it is unauthenticated, and an unauthenticated endpoint that answers
differently for a real address than a fake one is an account-enumeration oracle
for the whole university. These cover that, plus the token being single-use and
the new password actually taking effect.
"""

import time
import uuid

import jwt
import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db
from app.core.config import settings
from app.core.redis import get_redis
from app.core.security import (
    ALGORITHM,
    PASSWORD_RESET_TTL,
    create_access_token,
    create_password_reset_token,
)
from app.db.base import Base
from app.main import create_app

REGISTRATION = {
    "email": "Reset.Me@ulab.edu.bd",
    "password": "correct horse battery",
    "name": "Reset Me",
    "student_id_no": "221019001",
}
NEW_PASSWORD = "a whole new passphrase"


class _FakeRedis:
    """Enough of Redis for `SET [NX] [EX]` and `EXISTS`, no TTL clock.

    Real enough that a denied-listed jti actually reads back as revoked, which
    is what lets the single-use test observe a second use being refused.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def exists(self, key):
        return 1 if key in self.store else 0


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def verify_sent(monkeypatch):
    """Capture verification tokens so the test can activate the account."""
    captured: list[dict] = []

    async def _fake(*, to: str, name: str, token: str) -> bool:
        captured.append({"to": to, "name": name, "token": token})
        return True

    monkeypatch.setattr("app.api.routes.auth.send_verification_email", _fake)
    return captured


@pytest.fixture
def reset_sent(monkeypatch):
    """Capture reset tokens instead of emailing them.

    Patched where it is *used* — auth.py bound the name at import — so patching
    app.core.email would leave the route holding the original.
    """
    captured: list[dict] = []

    async def _fake(*, to: str, name: str, token: str) -> bool:
        captured.append({"to": to, "name": name, "token": token})
        return True

    monkeypatch.setattr("app.api.routes.auth.send_password_reset_email", _fake)
    return captured


@pytest.fixture
async def client(db, verify_sent, reset_sent):
    app = create_app()
    fake_redis = _FakeRedis()

    async def _db():
        yield db

    async def _redis():
        return fake_redis

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _register_active(client, verify_sent) -> None:
    """A registered, email-confirmed student — the normal state of an account
    whose owner then forgets the password."""
    await client.post("/auth/register/student", json=REGISTRATION)
    await client.get("/auth/verify-email", params={"token": verify_sent[0]["token"]})


async def _login(client, password: str):
    return await client.post(
        "/auth/login", json={"email": REGISTRATION["email"], "password": password}
    )


# --- the happy path ---------------------------------------------------------


async def test_reset_changes_the_password_and_old_one_stops_working(
    client, verify_sent, reset_sent
) -> None:
    await _register_active(client, verify_sent)

    asked = await client.post("/auth/forgot-password", json={"email": REGISTRATION["email"]})
    assert asked.status_code == status.HTTP_202_ACCEPTED
    assert len(reset_sent) == 1

    reset = await client.post(
        "/auth/reset-password",
        json={"token": reset_sent[0]["token"], "password": NEW_PASSWORD},
    )
    assert reset.status_code == status.HTTP_204_NO_CONTENT

    assert (await _login(client, NEW_PASSWORD)).status_code == status.HTTP_200_OK
    # The old password must not still open the account.
    assert (await _login(client, REGISTRATION["password"])).status_code == (
        status.HTTP_401_UNAUTHORIZED
    )


# --- enumeration safety -----------------------------------------------------


async def test_forgot_answers_identically_for_a_missing_address(
    client, verify_sent, reset_sent
) -> None:
    await _register_active(client, verify_sent)
    reset_sent.clear()

    real = await client.post("/auth/forgot-password", json={"email": REGISTRATION["email"]})
    fake = await client.post("/auth/forgot-password", json={"email": "nobody@ulab.edu.bd"})

    assert real.status_code == fake.status_code == status.HTTP_202_ACCEPTED
    assert real.json() == fake.json()
    # Only the real address is actually mailed — the part that must not be
    # observable from outside.
    assert len(reset_sent) == 1


# --- token rules ------------------------------------------------------------


async def test_a_forged_wrong_type_or_expired_token_is_refused(client, verify_sent) -> None:
    await _register_active(client, verify_sent)

    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "student",
            "type": "password_reset",
            "jti": uuid.uuid4().hex,
            "iat": int(time.time()) - 10,
            "exp": int(time.time()) - 1,
        },
        settings.jwt_secret,
        algorithm=ALGORITHM,
    )
    bad_tokens = [
        "not-a-token",
        create_access_token(str(uuid.uuid4()), "student"),  # right signature, wrong type
        create_password_reset_token(str(uuid.uuid4()), "student"),  # valid token, no such user
        expired,
    ]
    for token in bad_tokens:
        response = await client.post(
            "/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, token

    # The account is untouched; the real password still works after activation.
    assert (await _login(client, REGISTRATION["password"])).status_code == status.HTTP_200_OK


async def test_a_reset_token_is_single_use(client, verify_sent, reset_sent) -> None:
    """A link that leaks from an inbox or a mail log must not be replayable to
    change the password a second time."""
    await _register_active(client, verify_sent)
    await client.post("/auth/forgot-password", json={"email": REGISTRATION["email"]})
    token = reset_sent[0]["token"]

    first = await client.post(
        "/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}
    )
    second = await client.post(
        "/auth/reset-password", json={"token": token, "password": "yet another password"}
    )
    assert first.status_code == status.HTTP_204_NO_CONTENT
    assert second.status_code == status.HTTP_400_BAD_REQUEST

    # The second attempt did not take: the first new password still logs in.
    assert (await _login(client, NEW_PASSWORD)).status_code == status.HTTP_200_OK


async def test_a_short_new_password_is_refused(client, verify_sent, reset_sent) -> None:
    await _register_active(client, verify_sent)
    await client.post("/auth/forgot-password", json={"email": REGISTRATION["email"]})

    response = await client.post(
        "/auth/reset-password", json={"token": reset_sent[0]["token"], "password": "short"}
    )
    assert response.status_code == 422


# --- the link ---------------------------------------------------------------


def test_the_reset_link_points_at_the_student_app(monkeypatch) -> None:
    from app.core.email import reset_link

    monkeypatch.setattr(settings, "student_app_url", "https://unitrack.example.edu/")
    assert reset_link("abc").startswith("https://unitrack.example.edu/reset-password?token=")


def test_reset_ttl_is_short() -> None:
    """A reset link is a bearer credential that lingers in inboxes; it should
    die in an hour, not the two days a verification link gets."""
    assert PASSWORD_RESET_TTL.total_seconds() <= 3600
