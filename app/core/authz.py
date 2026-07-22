"""The authorization snapshot every request is resolved against.

A `Principal` is everything the API needs in order to answer *"may this caller
do this?"* — the user's id, role and status, plus (for helpers) their helper row
id and approval status. It is deliberately flat and immutable: no lazy loads, no
ORM session attached, safe to cache.

Why a snapshot instead of hitting Postgres on every request
-----------------------------------------------------------
The naive guard does `SELECT users` + `SELECT helpers` per request — two round
trips, ~1–2 ms, on *every* authenticated call. `POST /helper/gps` fires every
5 s per bus and the live-map WebSocket will be worse, so that cost is paid
thousands of times an hour to re-read rows that almost never change.

So the snapshot is cached in Redis (~0.15 ms) and the database is touched only
on a cache miss. Correctness is preserved by **explicit invalidation**: any code
path that changes a user's role/status or a helper's approval calls
`invalidate_principal()`, so a suspension takes effect on the very next request
rather than after a TTL. The TTL is only a backstop for changes made outside the
API (a manual `UPDATE` in psql, a seed script).

That is the whole trade: revocation stays immediate *because* every mutation
invalidates, and only because of that. **If you write an endpoint that mutates
`users` or `helpers` and you do not invalidate, you have created a security
bug** — a suspended account keeps working for up to `PRINCIPAL_TTL_S`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Helper, HelperStatus, User, UserRole, UserStatus

# Backstop only — the correctness mechanism is invalidate_principal(), not this.
PRINCIPAL_TTL_S = 300


def principal_key(user_id: uuid.UUID | str) -> str:
    return f"authz:principal:{user_id}"


@dataclass(frozen=True, slots=True)
class Principal:
    """Immutable auth snapshot of one user. Cheap to build, safe to cache."""

    user_id: uuid.UUID
    role: UserRole
    status: UserStatus
    helper_id: uuid.UUID | None = None
    helper_status: HelperStatus | None = None

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.active

    @property
    def is_approved_helper(self) -> bool:
        return self.role is UserRole.helper and self.helper_status is HelperStatus.approved

    # --- serialization for the Redis cache ---

    def to_json(self) -> str:
        d = asdict(self)
        d["user_id"] = str(self.user_id)
        d["helper_id"] = str(self.helper_id) if self.helper_id else None
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> Principal:
        d = json.loads(raw)
        return cls(
            user_id=uuid.UUID(d["user_id"]),
            role=UserRole(d["role"]),
            status=UserStatus(d["status"]),
            helper_id=uuid.UUID(d["helper_id"]) if d["helper_id"] else None,
            helper_status=HelperStatus(d["helper_status"]) if d["helper_status"] else None,
        )


async def load_principal_from_db(db: AsyncSession, user_id: uuid.UUID) -> Principal | None:
    """One query, LEFT JOINed — a non-helper simply has NULLs in the helper half."""
    stmt = (
        select(
            User.id,
            User.role,
            User.status,
            Helper.id.label("helper_id"),
            Helper.status.label("helper_status"),
        )
        .outerjoin(Helper, Helper.user_id == User.id)
        .where(User.id == user_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    return Principal(
        user_id=row.id,
        role=row.role,
        status=row.status,
        helper_id=row.helper_id,
        helper_status=row.helper_status,
    )


async def get_principal_cached(
    r: Redis, db: AsyncSession, user_id: uuid.UUID
) -> Principal | None:
    """Cache-aside read. Redis on the hot path, Postgres only on a miss.

    A Redis outage degrades to "every request hits Postgres" rather than
    "nobody can log in" — availability beats the latency win here.
    """
    try:
        if (raw := await r.get(principal_key(user_id))) is not None:
            return Principal.from_json(raw)
    except Exception:  # noqa: BLE001 — cache is optional, never fail the request on it
        pass

    principal = await load_principal_from_db(db, user_id)
    if principal is None:
        return None

    try:
        await r.set(principal_key(user_id), principal.to_json(), ex=PRINCIPAL_TTL_S)
    except Exception:  # noqa: BLE001
        pass
    return principal


async def invalidate_principal(r: Redis, user_id: uuid.UUID | str) -> None:
    """Call after ANY write to `users` or `helpers` for this user.

    Approving a helper, suspending an account, changing a role — all of them.
    Cheap (one DEL) and idempotent, so when in doubt, call it.
    """
    try:
        await r.delete(principal_key(user_id))
    except Exception:  # noqa: BLE001
        pass
