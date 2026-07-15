import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import Helper, HelperStatus, User, UserRole, UserStatus

_bearer = HTTPBearer(auto_error=True)

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = decode_token(creds.credentials, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise _CREDENTIALS_EXC from exc

    user = await db.get(User, user_id)
    if user is None or user.status == UserStatus.suspended:
        raise _CREDENTIALS_EXC
    return user


def require_role(
    *roles: UserRole,
) -> Callable[[User], Coroutine[Any, Any, User]]:
    async def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role"
            )
        return user

    return _guard


async def get_current_helper(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Helper:
    """Current user must be a helper whose account is approved (spec §8)."""
    if user.role != UserRole.helper:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Helpers only")
    result = await db.execute(select(Helper).where(Helper.user_id == user.id))
    helper = result.scalar_one_or_none()
    if helper is None or helper.status != HelperStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Helper account not approved"
        )
    return helper
