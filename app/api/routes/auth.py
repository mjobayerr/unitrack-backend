import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_email_verify_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.session import get_db
from app.models.user import Helper, HelperStatus, Student, User, UserRole, UserStatus
from app.schemas.auth import (
    HelperRegister,
    LoginRequest,
    RefreshRequest,
    StudentRegister,
    TokenPair,
    UserOut,
)

logger = logging.getLogger("unitrack.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


async def _get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


@router.post("/register/student", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register_student(payload: StudentRegister, db: AsyncSession = Depends(get_db)) -> User:
    # Server-side varsity-email gate (spec §8) — enforced at the API, not just the UI.
    if _email_domain(payload.email) not in settings.student_email_domains:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email domain not allowed for student registration",
        )
    if await _get_user_by_email(db, payload.email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        role=UserRole.student,
        name=payload.name,
        phone=payload.phone,
        status=UserStatus.pending_email,
    )
    user.student = Student(
        student_id_no=payload.student_id_no,
        department=payload.department,
        batch=payload.batch,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_email_verify_token(str(user.id), user.role)
    # TODO(P4): send via SMTP relay. For now, log the verification link.
    logger.info("Email verification link: /auth/verify-email?token=%s", token)
    return user


@router.post("/register/helper", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register_helper(payload: HelperRegister, db: AsyncSession = Depends(get_db)) -> User:
    if await _get_user_by_email(db, payload.email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    # Helper accounts are pending until an admin approves (spec §8).
    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        role=UserRole.helper,
        name=payload.name,
        phone=payload.phone,
        status=UserStatus.pending_approval,
    )
    user.helper = Helper(status=HelperStatus.pending)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/verify-email", response_model=UserOut)
async def verify_email(token: str = Query(...), db: AsyncSession = Depends(get_db)) -> User:
    try:
        payload = decode_token(token, expected_type="email_verify")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token"
        ) from exc

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.status == UserStatus.pending_email:
        user.status = UserStatus.active
        await db.commit()
        await db.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    user = await _get_user_by_email(db, payload.email)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if user.status != UserStatus.active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account not active (status={user.status})",
        )
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role),
        refresh_token=create_refresh_token(str(user.id), user.role),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
        user_id = uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        ) from exc

    user = await db.get(User, user_id)
    if user is None or user.status != UserStatus.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not active")
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role),
        refresh_token=create_refresh_token(str(user.id), user.role),
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
