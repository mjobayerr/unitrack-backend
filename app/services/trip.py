"""Trip lifecycle: start, end, and "what is this helper driving right now".

Every GPS fix, redemption and seat report hangs off a trip (spec §6), so this
module sits on the hottest path in the API. `get_active_trip` is designed to
answer from Redis alone.
"""

from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import ACTIVE_TRIP_TTL_S, helper_trip_key
from app.models.fleet import Bus, BusStatus, Route, Trip, TripStatus


class TripConflictError(Exception):
    """The bus or the helper is already on a live trip."""


class TripNotFoundError(Exception):
    """No such live trip for this helper."""


class InvalidTripTargetError(Exception):
    """The requested bus or route cannot be driven."""


@dataclass(frozen=True, slots=True)
class ActiveTrip:
    """The cached shape of a live trip — everything GPS ingest needs, nothing more."""

    trip_id: uuid.UUID
    bus_id: uuid.UUID
    route_id: uuid.UUID

    def to_json(self) -> str:
        return json.dumps({k: str(v) for k, v in asdict(self).items()})

    @classmethod
    def from_json(cls, raw: str) -> ActiveTrip:
        d = json.loads(raw)
        return cls(
            trip_id=uuid.UUID(d["trip_id"]),
            bus_id=uuid.UUID(d["bus_id"]),
            route_id=uuid.UUID(d["route_id"]),
        )

    @classmethod
    def from_model(cls, trip: Trip) -> ActiveTrip:
        return cls(trip_id=trip.id, bus_id=trip.bus_id, route_id=trip.route_id)


def service_date_now() -> datetime.date:
    """Today in the fleet's local timezone — see `settings.service_timezone`."""
    return datetime.datetime.now(ZoneInfo(settings.service_timezone)).date()


async def start_trip(
    db: AsyncSession, r: Redis, *, helper_id: uuid.UUID, bus_id: uuid.UUID, route_id: uuid.UUID
) -> Trip:
    """Put a helper on the road. Raises on a bad target or a double-start."""
    bus = await db.get(Bus, bus_id)
    if bus is None or bus.status is not BusStatus.active:
        raise InvalidTripTargetError("Bus is unknown or not in service")

    route = await db.get(Route, route_id)
    if route is None or not route.is_active:
        raise InvalidTripTargetError("Route is unknown or not active")

    trip = Trip(
        route_id=route_id,
        bus_id=bus_id,
        helper_id=helper_id,
        service_date=service_date_now(),
        actual_start=datetime.datetime.now(datetime.UTC),
        status=TripStatus.live,
    )
    db.add(trip)
    try:
        await db.commit()
    except IntegrityError as exc:
        # One of the partial unique indexes fired: this bus or this helper is
        # already live. Letting the database decide makes a double-tap of Start
        # a clean 409 instead of two trips racing through a Python check.
        await db.rollback()
        raise TripConflictError("Bus or helper is already on a live trip") from exc

    await _cache_active(r, helper_id, ActiveTrip.from_model(trip))
    return trip


async def end_trip(db: AsyncSession, r: Redis, *, helper_id: uuid.UUID) -> Trip:
    """Close the helper's live trip. Idempotent from the caller's view: ending a
    trip that is already ended raises, so the client can treat it as done."""
    trip = await _live_trip_from_db(db, helper_id)
    if trip is None:
        raise TripNotFoundError("No live trip for this helper")

    trip.status = TripStatus.completed
    trip.actual_end = datetime.datetime.now(datetime.UTC)
    await db.commit()

    await r.delete(helper_trip_key(str(helper_id)))
    return trip


async def get_active_trip(db: AsyncSession, r: Redis, helper_id: uuid.UUID) -> ActiveTrip | None:
    """Cache-aside, same contract as the auth Principal cache.

    A Redis miss or outage falls back to Postgres rather than pretending the
    helper has no trip — losing a trip binding would silently orphan GPS fixes.
    """
    key = helper_trip_key(str(helper_id))
    try:
        if (raw := await r.get(key)) is not None:
            return ActiveTrip.from_json(raw)
    except Exception:  # noqa: BLE001 — cache is an optimization, never a gate
        pass

    trip = await _live_trip_from_db(db, helper_id)
    if trip is None:
        return None

    active = ActiveTrip.from_model(trip)
    await _cache_active(r, helper_id, active)
    return active


async def _live_trip_from_db(db: AsyncSession, helper_id: uuid.UUID) -> Trip | None:
    stmt = select(Trip).where(Trip.helper_id == helper_id, Trip.status == TripStatus.live)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _cache_active(r: Redis, helper_id: uuid.UUID, active: ActiveTrip) -> None:
    try:
        await r.set(helper_trip_key(str(helper_id)), active.to_json(), ex=ACTIVE_TRIP_TTL_S)
    except Exception:  # noqa: BLE001
        pass
