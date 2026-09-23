"""Data access for the fleet: buses, routes, stops, and live trips.

Reads and the joins behind them live here; callers stage writes and commit.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import Row, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.fleet import Bus, BusStatus, Route, RouteStop, Stop, Trip, TripStatus
from app.models.user import Helper, User

# --- buses ---------------------------------------------------------------


async def get_bus(db: AsyncSession, bus_id: uuid.UUID) -> Bus | None:
    return await db.get(Bus, bus_id)


async def bus_by_reg_no(db: AsyncSession, reg_no: str) -> Bus | None:
    return await db.scalar(select(Bus).where(Bus.reg_no == reg_no))


async def existing_reg_nos(db: AsyncSession, reg_nos: Iterable[str]) -> set[str]:
    """Which of these registrations are already taken — one IN query, not a probe
    per bus, for the batch-create pre-check."""
    stmt = select(Bus.reg_no).where(Bus.reg_no.in_(list(reg_nos)))
    return set((await db.execute(stmt)).scalars())


async def list_buses(db: AsyncSession, status: BusStatus | None = None) -> list[Bus]:
    """Buses ordered by registration; filtered to one status when given, else the
    whole fleet (retired and in-maintenance included)."""
    stmt = select(Bus)
    if status is not None:
        stmt = stmt.where(Bus.status == status)
    return list((await db.execute(stmt.order_by(Bus.reg_no))).scalars())


# --- routes and stops ----------------------------------------------------


async def get_route(db: AsyncSession, route_id: uuid.UUID) -> Route | None:
    return await db.get(Route, route_id)


async def get_stop(db: AsyncSession, stop_id: uuid.UUID) -> Stop | None:
    return await db.get(Stop, stop_id)


async def list_routes(db: AsyncSession, only_active: bool = True) -> list[Route]:
    stmt = select(Route)
    if only_active:
        stmt = stmt.where(Route.is_active.is_(True))
    return list((await db.execute(stmt.order_by(Route.name, Route.direction))).scalars())


async def list_routes_with_stops(db: AsyncSession, only_active: bool = True) -> list[Route]:
    """Routes with their ordered stops eager-loaded — `selectinload` fetches the
    stops in two extra queries instead of an N+1 storm, on an endpoint every map
    load hits."""
    stmt = select(Route).options(selectinload(Route.stops).selectinload(RouteStop.stop))
    if only_active:
        stmt = stmt.where(Route.is_active.is_(True))
    return list((await db.execute(stmt.order_by(Route.name, Route.direction))).scalars())


async def get_route_with_stops(
    db: AsyncSession, route_id: uuid.UUID, *, populate_existing: bool = False
) -> Route | None:
    """A route with its ordered stops eager-loaded.

    `populate_existing` refreshes a Route already in the session's identity map —
    load-bearing after a bulk stop replace, whose `DELETE` the identity map never
    saw, so a re-read would otherwise hand back the stale `stops` collection.
    """
    stmt = (
        select(Route)
        .where(Route.id == route_id)
        .options(selectinload(Route.stops).selectinload(RouteStop.stop))
    )
    if populate_existing:
        stmt = stmt.execution_options(populate_existing=True)
    return await db.scalar(stmt)


async def list_stops(db: AsyncSession) -> list[Stop]:
    return list((await db.execute(select(Stop).order_by(Stop.name))).scalars())


async def routes_using_stop(db: AsyncSession, stop_id: uuid.UUID) -> int:
    """How many routes reference this stop — the guard before deleting it."""
    rows = (
        await db.execute(select(RouteStop.route_id).where(RouteStop.stop_id == stop_id))
    ).all()
    return len(rows)


async def existing_stop_ids(db: AsyncSession, stop_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    return set((await db.execute(select(Stop.id).where(Stop.id.in_(list(stop_ids))))).scalars())


async def clear_route_stops(db: AsyncSession, route_id: uuid.UUID) -> None:
    """Delete a route's stop rows. Staged only — the caller flushes and commits;
    a bulk DELETE the identity map never sees, so re-reads want populate_existing."""
    await db.execute(delete(RouteStop).where(RouteStop.route_id == route_id))


# --- live trips ----------------------------------------------------------


async def live_trips_with_context(db: AsyncSession) -> Sequence[Row]:
    """Every live trip joined to its bus, route, helper and user — the admin
    fleet map's one Postgres query before Redis fills in positions."""
    stmt = (
        select(Trip, Bus, Route, Helper, User)
        .join(Bus, Bus.id == Trip.bus_id)
        .join(Route, Route.id == Trip.route_id)
        .join(Helper, Helper.id == Trip.helper_id)
        .join(User, User.id == Helper.user_id)
        .where(Trip.status == TripStatus.live)
        .order_by(Trip.actual_start)
    )
    return (await db.execute(stmt)).all()


async def live_trip_columns(db: AsyncSession) -> Sequence[Row]:
    """Live trips with just the bus and route columns the public `/track/live`
    board needs — positions and seats come from Redis, not this query."""
    stmt = (
        select(
            Trip.id,
            Bus.id,
            Bus.reg_no,
            Bus.nickname,
            Bus.capacity,
            Route.id,
            Route.name,
            Route.direction,
        )
        .join(Bus, Bus.id == Trip.bus_id)
        .join(Route, Route.id == Trip.route_id)
        .where(Trip.status == TripStatus.live)
        .order_by(Trip.actual_start)
    )
    return (await db.execute(stmt)).all()


async def live_trips_serving_stop(db: AsyncSession, stop_id: uuid.UUID) -> Sequence[Row]:
    """Live trips whose route includes this stop — a trip that never passes it
    can never arrive, so its estimate is not worth reading."""
    stmt = (
        select(Trip.id, Trip.route_id, Trip.bus_id, Route.name)
        .join(Route, Route.id == Trip.route_id)
        .join(RouteStop, RouteStop.route_id == Route.id)
        .where(Trip.status == TripStatus.live, RouteStop.stop_id == stop_id)
    )
    return (await db.execute(stmt)).all()


async def trip_for_bus(
    db: AsyncSession, trip_id: uuid.UUID, bus_id: uuid.UUID
) -> Trip | None:
    """A specific trip, but only if it belongs to this bus — the history
    endpoint's ownership check on a caller-supplied trip id."""
    return await db.scalar(select(Trip).where(Trip.id == trip_id, Trip.bus_id == bus_id))
