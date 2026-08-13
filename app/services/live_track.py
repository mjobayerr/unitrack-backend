"""The live-tracking fan-out engine behind `/ws/track/{route_id}` (spec §7.3).

The read side of tracking has two shapes already: `/track/nearby` (geo, ES) and
`GET /admin/fleet` (the whole fleet, one Redis pipeline). This is the third: the
student live map for **one route**, pushed over a WebSocket every few seconds
instead of polled. The frame is assembled exactly as the admin map's is —
`parse_position`, `parse_seats`, `next_stop_minutes` from `fleet_view` — so a bus
reads identically on both maps and there is one place that decodes a `pos` hash.

Where the work happens
----------------------
The map is O(routes), not O(clients). Two thousand students watching one route
need the same answer, so the cost that scales is "how many routes have anyone
watching", not "how many people are watching". Each connection reads Redis for
its own frame (cheap — a handful of keys), but the **live-trip roster** for a
route — which trips are running, on which buses — is a Postgres query, and that
is shared through a short-TTL cache so a busy route hits the database once every
few seconds rather than once per connection per tick.

The roster cache is why a just-started trip can take up to `roster_ttl_s` to
appear on the map, and a just-ended one that long to drop off. That is the
deliberate trade for not querying Postgres on every frame; the window is seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import bus_pos_key, bus_seats_key, trip_eta_key
from app.models.fleet import Bus, Route, Trip, TripStatus
from app.schemas.admin import GpsFreshness
from app.schemas.live_track import TrackBusFrame, TrackFrame
from app.services.fleet_view import (
    age_seconds,
    classify,
    next_stop_minutes,
    parse_position,
    parse_seats,
)

logger = logging.getLogger("unitrack.live_track")


@dataclass(frozen=True, slots=True)
class LiveTripRef:
    """The Postgres half of one bus on the map: identity that Redis does not hold.

    Redis knows where the bus is; it does not know the bus's registration or its
    configured capacity, which the frame needs (the latter as the fallback shown
    before the helper reports a seat count). Fetched once per roster refresh.
    """

    trip_id: uuid.UUID
    bus_id: uuid.UUID
    reg_no: str
    nickname: str | None
    capacity: int


async def route_exists(db: AsyncSession, route_id: uuid.UUID) -> bool:
    """Whether the route is real, so a subscription to a typo closes cleanly
    rather than streaming an empty frame forever."""
    return (await db.scalar(select(Route.id).where(Route.id == route_id))) is not None


async def live_trips_on_route(db: AsyncSession, route_id: uuid.UUID) -> list[LiveTripRef]:
    """The buses currently running this route — one indexed query, no Redis.

    Ordered by start so the map's bus order is stable between frames rather than
    reshuffling on every roster refresh.
    """
    rows = (
        await db.execute(
            select(Trip.id, Bus.id, Bus.reg_no, Bus.nickname, Bus.capacity)
            .join(Bus, Bus.id == Trip.bus_id)
            .where(Trip.route_id == route_id, Trip.status == TripStatus.live)
            .order_by(Trip.actual_start)
        )
    ).all()
    return [
        LiveTripRef(
            trip_id=trip_id,
            bus_id=bus_id,
            reg_no=reg_no,
            nickname=nickname,
            capacity=capacity,
        )
        for trip_id, bus_id, reg_no, nickname, capacity in rows
    ]


async def _read_redis(r: Redis, refs: list[LiveTripRef]) -> list:
    """Position, seats and cached ETA for every bus in the roster, one pipeline.

    Three reads per bus issued together rather than serially, the same shape as
    the admin fleet map. On a Redis error every bus reads as `lost` — truthful,
    and it keeps the map up on stale data rather than dropping the connection.
    """
    if not refs:
        return []
    pipe = r.pipeline(transaction=False)
    for ref in refs:
        pipe.hgetall(bus_pos_key(str(ref.bus_id)))
        pipe.hgetall(bus_seats_key(str(ref.bus_id)))
        pipe.get(trip_eta_key(str(ref.trip_id)))
    try:
        return await pipe.execute()
    except Exception:  # noqa: BLE001 — a cache miss must not kill the stream
        logger.warning("live-track: Redis read failed for route frame; reporting lost")
        return [None] * (len(refs) * 3)


def assemble_frame(
    route_id: uuid.UUID,
    refs: list[LiveTripRef],
    redis_results: list,
    now: datetime,
) -> TrackFrame:
    """Turn a roster + its Redis reads into one frame. Pure — no I/O, so tested
    directly without a database or a broker.

    `redis_results` is the flat pipeline output: three entries per ref, in
    roster order — `(pos_hash, seats_hash, eta_json)` repeated.
    """
    buses: list[TrackBusFrame] = []
    tally = {GpsFreshness.live: 0, GpsFreshness.stale: 0, GpsFreshness.lost: 0}

    for index, ref in enumerate(refs):
        raw_pos, raw_seats, raw_eta = redis_results[index * 3 : index * 3 + 3]

        position = parse_position(raw_pos)
        age = age_seconds(position.ts if position else None, now)
        freshness = classify(age)
        tally[freshness] += 1
        occupied, capacity = parse_seats(raw_seats)

        buses.append(
            TrackBusFrame(
                trip_id=ref.trip_id,
                bus_id=ref.bus_id,
                reg_no=ref.reg_no,
                nickname=ref.nickname,
                lat=position.lat if position else None,
                lng=position.lng if position else None,
                heading=position.heading if position else None,
                speed_kmh=position.speed_kmh if position else None,
                fix_ts=position.ts if position else None,
                fix_age_s=age,
                freshness=freshness,
                occupied=occupied,
                # Fall back to the bus's configured capacity so the client can
                # show "— / 45" before any seat report lands.
                capacity=capacity if capacity is not None else ref.capacity,
                next_stop_eta_minutes=next_stop_minutes(raw_eta, now),
            )
        )

    return TrackFrame(
        route_id=route_id,
        generated_at=now,
        total=len(buses),
        live=tally[GpsFreshness.live],
        stale=tally[GpsFreshness.stale],
        lost=tally[GpsFreshness.lost],
        buses=buses,
    )


async def build_frame(
    db: AsyncSession,
    r: Redis,
    route_id: uuid.UUID,
    now: datetime,
    refs: list[LiveTripRef] | None = None,
) -> TrackFrame:
    """One frame for one route. Pass `refs` to reuse a cached roster; omit it to
    fetch a fresh one from Postgres."""
    if refs is None:
        refs = await live_trips_on_route(db, route_id)
    redis_results = await _read_redis(r, refs)
    return assemble_frame(route_id, refs, redis_results, now)


class RosterCache:
    """Per-route live-trip rosters, shared across connections with a short TTL.

    Collapses the Postgres roster query from "once per connection per tick" to
    "once per watched route per TTL". A stampede on a cold key lets a few
    connections query at once rather than serializing them behind a lock held
    across the database call — cheap and rare, and the result is identical.
    """

    def __init__(self, ttl_s: float) -> None:
        self._ttl_s = ttl_s
        self._entries: dict[uuid.UUID, tuple[float, list[LiveTripRef]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, db: AsyncSession, route_id: uuid.UUID) -> list[LiveTripRef]:
        now = time.monotonic()
        async with self._lock:
            entry = self._entries.get(route_id)
            if entry is not None and now - entry[0] < self._ttl_s:
                return entry[1]

        refs = await live_trips_on_route(db, route_id)

        async with self._lock:
            self._entries[route_id] = (time.monotonic(), refs)
        return refs

    def clear(self) -> None:
        """Drop all cached rosters. For tests, and for a future trip-lifecycle
        hook that wants a started trip to appear without waiting out the TTL."""
        self._entries.clear()


# Process-wide: every WebSocket connection shares these rosters.
roster_cache = RosterCache(settings.track_ws_roster_ttl_s)
