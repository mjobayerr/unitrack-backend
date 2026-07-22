"""Helper (on-bus) endpoints: trip lifecycle and GPS ingest.

Guarded at the router — helper role **and** `helpers.status = 'approved'`.
Helpers self-register as pending, so without the approval check anyone who
completed signup could start trips and inject positions for any bus.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_approved_helper
from app.core.authz import Principal
from app.core.redis import GPS_STREAM, bus_pos_key, fleet_channel, get_redis
from app.db.session import get_db
from app.models.fleet import Bus
from app.models.ops import AlertSource
from app.schemas.gps import GpsAccepted, GpsBatch
from app.schemas.ops import AlertOut, AlertRaiseIn, SeatReportIn, SeatStateOut
from app.schemas.trip import ActiveTripOut, TripOut, TripStartRequest
from app.services import ops as ops_service
from app.services import trip as trip_service

router = APIRouter(
    prefix="/helper",
    tags=["helper"],
    dependencies=[Depends(require_approved_helper)],
)


# --------------------------------------------------------------------------
# Trip lifecycle
# --------------------------------------------------------------------------


@router.post("/trips/start", response_model=TripOut, status_code=status.HTTP_201_CREATED)
async def start_trip(
    body: TripStartRequest,
    helper: Principal = Depends(require_approved_helper),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> TripOut:
    """Begin a trip. Everything the bus produces from now on binds to it."""
    try:
        trip = await trip_service.start_trip(
            db, r, helper_id=helper.helper_id, bus_id=body.bus_id, route_id=body.route_id
        )
    except trip_service.InvalidTripTargetError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except trip_service.TripConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return TripOut.model_validate(trip)


@router.post("/trips/end", response_model=TripOut)
async def end_trip(
    helper: Principal = Depends(require_approved_helper),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> TripOut:
    """Close the caller's live trip.

    No trip id in the path: a helper has at most one live trip, enforced by a
    partial unique index, so there is nothing to disambiguate — and no id for a
    client to get wrong or forge.
    """
    try:
        trip = await trip_service.end_trip(db, r, helper_id=helper.helper_id)
    except trip_service.TripNotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return TripOut.model_validate(trip)


@router.get("/trips/active", response_model=ActiveTripOut | None)
async def get_active_trip(
    helper: Principal = Depends(require_approved_helper),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> ActiveTripOut | None:
    """What the app calls on launch to recover state after a restart or crash."""
    active = await trip_service.get_active_trip(db, r, helper.helper_id)
    return None if active is None else ActiveTripOut(**vars(active))


# --------------------------------------------------------------------------
# Seat counts and alerts
# --------------------------------------------------------------------------


@router.post("/seats", response_model=SeatStateOut, status_code=status.HTTP_201_CREATED)
async def report_seats(
    body: SeatReportIn,
    helper: Principal = Depends(require_approved_helper),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> SeatStateOut:
    """Report how full the bus is (spec §6 `seat_reports`).

    Requires a live trip: an occupancy count with no trip cannot be attributed
    to a route or a time window, which is the only thing that makes it useful.
    """
    active = await trip_service.get_active_trip(db, r, helper.helper_id)
    if active is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Start a trip before reporting seats")

    bus = await db.get(Bus, active.bus_id)
    if bus is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown bus")

    report = await ops_service.report_seats(
        db,
        r,
        trip_id=active.trip_id,
        helper_id=helper.helper_id,
        bus=bus,
        occupied=body.occupied,
        reported_at=body.reported_at,
    )
    return SeatStateOut(
        trip_id=active.trip_id,
        occupied=report.occupied,
        capacity=report.capacity_snapshot,
        # Clamped at zero: an over-capacity bus has no negative free seats, and
        # the student app would render "-3 seats available".
        free=max(report.capacity_snapshot - report.occupied, 0),
        reported_at=report.reported_at,
    )


@router.post("/alerts", response_model=AlertOut, status_code=status.HTTP_201_CREATED)
async def raise_alert(
    body: AlertRaiseIn,
    helper: Principal = Depends(require_approved_helper),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> AlertOut:
    """Raise an alert from the emergency screen (spec §7.6).

    Works without a live trip on purpose — a breakdown on the way to the depot
    is still a breakdown, and an SOS must never be refused on a technicality.

    Severity is assigned by the server from the type; the client does not get a
    say, or every alert would arrive critical.
    """
    active = await trip_service.get_active_trip(db, r, helper.helper_id)
    alert = await ops_service.raise_alert(
        db,
        r,
        source=AlertSource.helper,
        alert_type=body.type,
        raised_by=helper.user_id,
        trip_id=active.trip_id if active else None,
        bus_id=active.bus_id if active else None,
        message=body.message,
        lat=body.lat,
        lng=body.lng,
    )
    return AlertOut.model_validate(alert)


# --------------------------------------------------------------------------
# GPS ingest
# --------------------------------------------------------------------------


@router.post("/gps", response_model=GpsAccepted, status_code=status.HTTP_202_ACCEPTED)
async def ingest_gps(
    batch: GpsBatch,
    helper: Principal = Depends(require_approved_helper),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> GpsAccepted:
    """Receive a batch of GPS fixes from a helper device (spec §7.3).

    Writes the newest fix to `bus:{id}:pos` (TTL 60 s) for "where is bus 7 right
    now", publishes to the fleet channel for the admin live map, and XADDs every
    fix to the `gps_ingest` stream. The worker drains that stream into
    Elasticsearch.

    Trip binding
    ------------
    If the helper has a live trip, its id rides along on every fix and the
    trip's bus wins over whatever the client sent — the server decides which bus
    a helper is driving, not the phone.

    Fixes with no live trip are still accepted, with an empty `trip_id`. That is
    a **transition allowance** for the current helper build, which has no trip
    UI yet; it is why `trip_id` is nullable downstream. Once the app ships trip
    lifecycle, make this a 409 and delete this paragraph.
    """
    active = await trip_service.get_active_trip(db, r, helper.helper_id)

    if active is not None:
        if batch.bus_id != active.bus_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Live trip is on a different bus — end the trip before switching",
            )
        bus_id = str(active.bus_id)
        trip_id = str(active.trip_id)
    else:
        # No trip: the bus is unverified beyond "it exists", so this costs a
        # query. The trip path above skips it — the trip already proved the bus.
        if await db.get(Bus, batch.bus_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown bus")
        bus_id = str(batch.bus_id)
        trip_id = ""

    newest = max(batch.points, key=lambda p: p.ts)
    pos_key = bus_pos_key(bus_id)

    # One pipeline instead of 2 + len(points) sequential round trips. At 50
    # fixes that is 52 network waits collapsed into one, on the endpoint every
    # bus hits every 5 seconds.
    pipe = r.pipeline(transaction=False)
    pipe.hset(
        pos_key,
        mapping={
            "lat": str(newest.lat),
            "lng": str(newest.lng),
            "speed": str(newest.speed) if newest.speed is not None else "",
            "heading": str(newest.heading) if newest.heading is not None else "",
            "ts": newest.ts.astimezone(UTC).isoformat(),
            "trip_id": trip_id,
            "ingested_at": datetime.now(UTC).isoformat(),
        },
    )
    pipe.expire(pos_key, 60)
    pipe.publish(fleet_channel(), f"{bus_id}:{newest.lat},{newest.lng}")
    for p in batch.points:
        pipe.xadd(
            GPS_STREAM,
            {
                "bus_id": bus_id,
                "helper_id": str(helper.helper_id),
                "trip_id": trip_id,
                "ts": p.ts.astimezone(UTC).isoformat(),
                "lat": str(p.lat),
                "lng": str(p.lng),
                "speed": str(p.speed) if p.speed is not None else "",
                "heading": str(p.heading) if p.heading is not None else "",
                "accuracy": str(p.accuracy) if p.accuracy is not None else "",
            },
        )
    await pipe.execute()

    return GpsAccepted(
        accepted=len(batch.points),
        bus_id=batch.bus_id if active is None else active.bus_id,
        trip_id=active.trip_id if active else None,
    )
