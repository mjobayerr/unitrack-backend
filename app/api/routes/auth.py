import logging
import uuid

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_access_claims, get_current_user
from app.core.config import settings
from app.core.email import send_verification_email
from app.core.redis import get_redis
from app.core.revocation import claim_rotation, is_revoked_strict, recall_rotation, revoke
from app.core.security import (
    create_access_token,
    create_email_verify_token,
    create_refresh_token,
    decode_token,
    hash_password,
    spend_dummy_verify,
    verify_password,
)
from app.db.session import get_db
from app.models.user import Helper, HelperStatus, Student, User, UserRole, UserStatus
from app.schemas.auth import (
    HelperRegister,
    LoginRequest,
    LogoutRequest,
    MeOut,
    RefreshRequest,
    ResendVerification,
    StudentProfileOut,
    StudentRegister,
    TokenPair,
    UserOut,
)

logger = logging.getLogger("unitrack.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# One message for every way a refresh can fail — expired, malformed, revoked,
# replayed. Telling them apart would confirm to a holder of a stolen token that
# the token was real and had merely been rotated.
_INVALID_REFRESH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
)


# One confirmation mail per address per two minutes. Long enough that a bomb is
# not worth attempting, short enough that someone who genuinely did not receive
# the first one is not left waiting.
RESEND_COOLDOWN_S = 120

_EMAIL_TAKEN = HTTPException(
    status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
)


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


async def _get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


@router.post("/register/student", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register_student(
    payload: StudentRegister,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> User:
    # Server-side varsity-email gate (spec §8) — enforced at the API, not just the UI.
    if _email_domain(payload.email) not in settings.student_email_domains:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email domain not allowed for student registration",
        )
    if await _get_user_by_email(db, payload.email):
        raise _EMAIL_TAKEN

    # `students.student_id_no` is unique, and a clash is ordinary: two people
    # mistype the same roll number, or someone registers with a classmate's.
    # Unchecked it reached the database as an IntegrityError and answered 500,
    # which tells the student nothing about what to correct.
    taken = (
        await db.execute(
            select(Student.id).where(Student.student_id_no == payload.student_id_no)
        )
    ).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That student ID is already registered",
        )

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
    try:
        await db.commit()
    except IntegrityError as exc:
        # Both checks above are check-then-insert, so a concurrent identical
        # signup — a double-tapped button on a slow connection — can still land
        # here. The database is the authority; this just turns its refusal into
        # the same answer the sequential path gives instead of a 500.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email or student ID is already registered",
        ) from exc
    await db.refresh(user)

    # After the response, not before it. A mail relay can take seconds or hang;
    # the account is already committed, and making the student wait on SMTP —
    # or fail because of it — would trade the valuable thing for the cheap one.
    background.add_task(
        send_verification_email,
        to=user.email,
        name=user.name,
        token=create_email_verify_token(str(user.id), user.role),
    )
    return user


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(
    payload: ResendVerification,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Send the confirmation link again.

    Without this, one email lost to a spam filter is an account its owner can
    never use and can never re-register, because the address is already taken.

    Always answers 202, whatever is true of the address. Replying "no such
    account" would turn this into a membership oracle for the whole university —
    and an unauthenticated one, since anyone who has not verified cannot log in
    to prove anything. An already-active account is also silently ignored rather
    than confirmed; the same reasoning applies.

    Rate limited **per address**, which is the part nginx cannot do. Its limit is
    per IP, so it slows one attacker down without stopping them: five a minute at
    one inbox is three hundred an hour, and from a handful of IPs it is a mail
    bomb sent by the university's own relay. The cooldown below is keyed on the
    address, so the volume any single inbox can receive is fixed no matter how
    many machines ask. A rejected resend still answers 202 — telling the caller
    they were throttled would confirm the address is real.
    """
    user = await _get_user_by_email(db, payload.email)
    if user is not None and user.status is UserStatus.pending_email:
        if await _claim_resend(r, user.email):
            background.add_task(
                send_verification_email,
                to=user.email,
                name=user.name,
                token=create_email_verify_token(str(user.id), user.role),
            )
        else:
            logger.info("resend for %s suppressed by cooldown", user.email)
    else:
        logger.info("resend requested for %s — nothing to send", payload.email)

    return {"detail": "If that address needs confirming, a link is on its way."}


async def _claim_resend(r: Redis, email: str) -> bool:
    """True if this address may be mailed now, taking the slot if so.

    `SET NX EX` is the whole limiter: it succeeds only if no key exists, so the
    check and the claim are one atomic operation and concurrent requests cannot
    both win.

    Fails **open** on a Redis outage, deliberately. The opposite choice would
    make a cache problem into "nobody can finish signing up", and the cost of
    being wrong here is some duplicate email — while nginx's per-IP limit is
    still standing.
    """
    try:
        return bool(await r.set(f"resend:{email}", "1", ex=RESEND_COOLDOWN_S, nx=True))
    except Exception:  # noqa: BLE001
        logger.warning("resend cooldown unavailable; allowing the send")
        return True


@router.post("/register/helper", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register_helper(payload: HelperRegister, db: AsyncSession = Depends(get_db)) -> User:
    if await _get_user_by_email(db, payload.email):
        raise _EMAIL_TAKEN

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
    try:
        await db.commit()
    except IntegrityError as exc:
        # Lost the race on users.email. Registration is the one screen a helper
        # uses on a bad connection with a slow thumb, so the double submit is not
        # hypothetical.
        await db.rollback()
        raise _EMAIL_TAKEN from exc
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


def _issue_pair(user: User) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role),
        refresh_token=create_refresh_token(str(user.id), user.role),
        expires_in=settings.access_token_ttl_min * 60,
    )


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    user = await _get_user_by_email(db, payload.email)
    if user is None:
        # Spend the same argon2 time as the found-user branch before failing, so
        # response latency does not reveal which addresses have accounts.
        spend_dummy_verify()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if user.status is UserStatus.pending_email:
        # Named specifically, unlike the other inactive states. The caller has
        # just proved they know this password, so nothing is leaked that they
        # did not already have — and "Account is not active" sends someone who
        # merely needs to check their inbox looking for an administrator.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Confirm your email address first — check your inbox",
        )
    if user.status != UserStatus.active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is not active",
        )
    return _issue_pair(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> TokenPair:
    """Exchange a refresh token for a new pair, invalidating the one presented.

    Rotation matters: without it every refresh token ever issued stays usable
    for its full 30 days, so a single leaked token is a month of silent access
    that no logout can end. Here the presented token is denied-listed as part of
    the exchange, which also means replaying an old one is *detectable* — and a
    replay is either a stolen token or a broken client, never normal traffic.

    Rotation is softened by a short grace window, because "never normal traffic"
    is not quite true for this app: the helper's UI and its GPS service isolate
    hold the same refresh token and reach 15-minute expiry together, so they
    legitimately refresh at the same instant. See `claim_rotation` for how both
    end up with the same pair instead of one of them being signed out mid-trip.
    """
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
        user_id = uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise _INVALID_REFRESH from exc

    # Strict: a Redis outage makes this report "revoked" rather than guess. The
    # caller keeps a working access token for up to 15 minutes meanwhile.
    if await is_revoked_strict(r, claims["jti"]):
        # Already rotated moments ago? Hand back the same replacement pair.
        if (replacement := await recall_rotation(r, claims["jti"])) is not None:
            return TokenPair.model_validate_json(replacement)
        logger.warning("refresh token replay for user %s (jti=%s)", user_id, claims["jti"])
        raise _INVALID_REFRESH

    user = await db.get(User, user_id)
    if user is None or user.status != UserStatus.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not active")

    pair = _issue_pair(user)

    # Claim the rotation before revoking, so a caller racing us either loses the
    # claim and receives our pair, or wins and we receive theirs. Revoking first
    # would leave a window where the loser sees "revoked" with no record yet.
    try:
        if (winner := await claim_rotation(r, claims["jti"], pair.model_dump_json())) is not None:
            return TokenPair.model_validate_json(winner)
    except Exception:  # noqa: BLE001
        # Redis refused the claim. Fall through and rotate anyway: a failed
        # grace window costs a re-login, a skipped revoke costs 30 days.
        logger.error("rotation claim failed for jti=%s", claims["jti"])

    await revoke(r, claims)
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: LogoutRequest,
    claims: dict = Depends(get_access_claims),
    r: Redis = Depends(get_redis),
) -> None:
    """End this session: deny-list the access token and the refresh token.

    Deleting tokens client-side was never logout — the tokens stayed valid on
    any device that had copied them. Revoking the refresh token is the part that
    matters, since it is the one worth 30 days.

    A refresh token belonging to a *different* user is ignored rather than
    revoked, so this cannot be used to sign other people out.
    """
    await revoke(r, claims)

    if payload.refresh_token:
        try:
            refresh_claims = decode_token(payload.refresh_token, expected_type="refresh")
        except jwt.InvalidTokenError:
            return  # Already unusable; nothing to revoke.
        if refresh_claims.get("sub") == claims.get("sub"):
            await revoke(r, refresh_claims)


@router.get("/me", response_model=MeOut)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeOut:
    """The signed-in account plus the profile registration collected.

    The student row is a second SELECT rather than an eager join because
    `get_current_user` is shared by every authenticated route and most do not
    need it — the cost belongs here, at the one endpoint that returns a profile.
    Only students have a row; helpers and admins fall through with `student`
    null.
    """
    student: Student | None = None
    if user.role == UserRole.student:
        student = await db.scalar(select(Student).where(Student.user_id == user.id))

    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        status=user.status,
        phone=user.phone,
        student=StudentProfileOut.model_validate(student) if student else None,
    )
