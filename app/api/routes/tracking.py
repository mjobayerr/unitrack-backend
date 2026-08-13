"""Read-side tracking: where the buses are, and when they arrive.

Two different questions with two different backing stores, which is the whole
point of the split:

- **"which buses are near me"** — Elasticsearch, via geo_distance. This is the
  payoff over Redis/Postgres that §5.1 was revisited for.
- **"when does one reach my stop"** — Redis, read straight from what the ETA
  engine precomputed. The answer depends on the bus, not the asker, so a
  hundred students watching one stop share one computation rather than each
  paying for a route query, a fix-history query and a distance walk.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from elasticsearch import AsyncElasticsearch
from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_authenticated
from app.core.elasticsearch import GPS_INDEX, get_es
from app.core.redis import get_redis, trip_eta_key
from app.db.session import get_db
from app.models.fleet import Bus, Route, RouteStop, Stop, Trip, TripStatus
from app.schemas.gps import BusHistoryPathOut, GpsPoint
from app.schemas.trip import BusArrivalOut, StopArrivalsOut, TripEtaOut
from app.services.fleet_view import minutes_until

# Any signed-in, active account may look up buses — students, helpers, admins
# all need it. Not public: live vehicle positions are the fleet's whereabouts,
# and an unauthenticated endpoint hands them to anyone who finds the URL.
router = APIRouter(
    prefix="/track",
    tags=["tracking"],
    dependencies=[Depends(require_authenticated)],
)


# How recent a fix has to be to count as "where a bus is now".
#
# The helper app posts a batch every 5 s, so two minutes is roughly twenty-four
# missed batches — comfortably past a tunnel or a dropped connection, while
# still short enough that a bus which has genuinely stopped reporting disappears
# from the map instead of lingering.
NEARBY_FRESH_S = 120


def build_nearby_query(
    origin: dict[str, float], radius_km: float, cutoff: datetime
) -> dict:
    """The Elasticsearch body for "which buses are near me, right now".

    Extracted so the two things that were once wrong here can be asserted
    without a running Elasticsearch — see tests/test_nearby_query.py.

    Both parts matter, and the original had neither:

    **The freshness filter.** Without a `range` on `ts` this searches the entire
    fix history, so a bus that stopped reporting days ago still answers. The
    endpoint's docstring always claimed "a recent fix"; nothing enforced it.

    **Sorting by `ts` before distance.** `collapse` keeps one document per bus,
    and *the first sort key decides which one*. Sorting by `_geo_distance` first
    therefore picked the closest point that bus had ever recorded — so a bus
    currently 4 km away, which happened to drive past this spot an hour earlier,
    was reported at that old position, 50 m away. Worse than stale: it sends a
    student to a stop for a bus that is not coming. Sorting by `ts` descending
    picks the latest fix, and the distance sort stays as a tiebreak and to make
    Elasticsearch compute the distance for us.
    """
    return {
        "query": {
            "bool": {
                # `filter`, not `must`: neither clause needs to contribute a
                # relevance score, and filters are cacheable.
                "filter": [
                    {
                        "geo_distance": {
                            "distance": f"{radius_km}km",
                            "location": origin,
                        }
                    },
                    {"range": {"ts": {"gte": cutoff.isoformat()}}},
                ]
            }
        },
        "collapse": {"field": "bus_id"},
        "sort": [
            {"ts": {"order": "desc"}},
            {"_geo_distance": {"location": origin, "order": "asc", "unit": "km"}},
        ],
    }


@router.get("/nearby")
async def nearby_buses(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=5, gt=0, le=50),
    limit: int = Query(default=20, ge=1, le=100),
    es: AsyncElasticsearch = Depends(get_es),
) -> dict:
    """Buses whose latest fix is within `radius_km` and newer than
    `NEARBY_FRESH_S`, closest first.
    """
    origin = {"lat": lat, "lon": lng}
    cutoff = datetime.now(UTC) - timedelta(seconds=NEARBY_FRESH_S)

    res = await es.search(
        index=GPS_INDEX,
        size=limit,
        **build_nearby_query(origin, radius_km, cutoff),
    )
    buses = [
        {
            "bus_id": h["_source"]["bus_id"],
            "location": h["_source"]["location"],
            "ts": h["_source"]["ts"],
            "speed": h["_source"].get("speed"),
            # Index 1: `sort` is [ts, distance], so the distance Elasticsearch
            # computed is the second value.
            "distance_km": round(h["sort"][1], 3),
        }
        for h in res["hits"]["hits"]
    ]
    # Elasticsearch ordered these newest-first because that is what `collapse`
    # needed. The endpoint promises closest-first, so reorder here — at most
    # `limit` (100) rows, so the cost is nothing.
    buses.sort(key=lambda b: b["distance_km"])
    return {"origin": origin, "radius_km": radius_km, "count": len(buses), "buses": buses}


# ---------------------------------------------------------------------------
# Arrival times (spec §7.4)
# ---------------------------------------------------------------------------


async def _cached_eta(r: Redis, trip_id: str) -> dict | None:
    """One trip's precomputed arrivals, or None if there are none to read.

    A miss is ordinary, not an error: the trip may have just started, the bus
    may have no fixes yet, or the engine may not have run since. Every caller
    treats absent as "nothing to say about this bus" rather than failing, so a
    Redis outage costs arrival times and nothing else.
    """
    try:
        raw = await r.get(trip_eta_key(trip_id))
    except Exception:  # noqa: BLE001 — ETAs are an enhancement, never a gate
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


@router.get("/trips/{trip_id}/eta", response_model=TripEtaOut)
async def trip_eta(
    trip_id: uuid.UUID,
    r: Redis = Depends(get_redis),
) -> TripEtaOut:
    """Every remaining arrival for one live trip — what the fleet map draws."""
    cached = await _cached_eta(r, str(trip_id))
    if cached is None:
        # Deliberately not an empty 200: "this bus has no estimate right now"
        # and "this bus is arriving nowhere" would otherwise look identical,
        # and a map would render the second as a bus that has finished.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No arrival estimate for this trip yet"
        )
    out = TripEtaOut.model_validate(cached)
    return _restate_minutes(out, datetime.now(UTC))


def _restate_minutes(out: TripEtaOut, now: datetime) -> TripEtaOut:
    """Recount every arrival's minutes against the clock now.

    The ETA engine runs once a minute and its payload is cached for longer, so
    the `eta_minutes` it wrote is stale by up to a minute in the good case and
    indefinitely if the engine stops. Served as-is, a bus reads "2 min away" for
    as long as the cache holds — including well after it has come and gone. The
    absolute `eta` is the durable fact, so the minutes are derived from it here.
    """
    for arrival in out.arrivals:
        arrival.eta_minutes = minutes_until(arrival.eta, now)
    return out


@router.get("/stops/{stop_id}/arrivals", response_model=StopArrivalsOut)
async def stop_arrivals(
    stop_id: uuid.UUID,
    limit: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> StopArrivalsOut:
    """The next buses reaching one stop, soonest first.

    The question a student standing at a stop actually has, and the reason the
    ETA engine exists. The live map answers "where is it", which is not the same
    thing — a bus two kilometres away on an open road and one two kilometres
    away in Farmgate traffic are twenty minutes apart.

    Reads only Redis for the estimates themselves. Postgres is touched once, for
    the names, because "4 min" is useless without knowing which route it is on.
    """
    stop = await db.get(Stop, stop_id)
    if stop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown stop")

    now = datetime.now(UTC)

    # Live trips whose route includes this stop. A trip on a route that does not
    # serve it can never arrive, so there is no point reading its estimate.
    stmt = (
        select(Trip.id, Trip.route_id, Trip.bus_id, Route.name)
        .join(Route, Route.id == Trip.route_id)
        .join(RouteStop, RouteStop.route_id == Route.id)
        .where(Trip.status == TripStatus.live, RouteStop.stop_id == stop_id)
    )
    candidates = (await db.execute(stmt)).all()

    arrivals: list[BusArrivalOut] = []
    for trip_id, route_id, bus_id, route_name in candidates:
        cached = await _cached_eta(r, str(trip_id))
        if cached is None:
            continue
        for item in cached.get("arrivals", []):
            if item.get("stop_id") != str(stop_id):
                continue
            eta = datetime.fromisoformat(item["eta"])
            arrivals.append(
                BusArrivalOut(
                    stop_id=stop_id,
                    seq=item["seq"],
                    eta=eta,
                    # Recounted, not read from the payload — see `_restate_minutes`.
                    # This is the endpoint a student reads while deciding whether
                    # to run for a bus, so a minute-old number is the wrong answer
                    # to the question they asked.
                    eta_minutes=minutes_until(eta, now),
                    basis=item["basis"],
                    distance_km=item["distance_km"],
                    trip_id=trip_id,
                    route_id=route_id,
                    route_name=route_name,
                    bus_id=bus_id,
                )
            )
            # A stop appears at most once per route, so the first match is the
            # only one this trip can offer.
            break

    arrivals.sort(key=lambda a: a.eta)
    return StopArrivalsOut(
        stop_id=stop.id,
        stop_name=stop.name,
        as_of=now,
        arrivals=arrivals[:limit],
    )


@router.get("/bus/{bus_id}/history", response_model=BusHistoryPathOut)
async def get_bus_history_path(
    bus_id: uuid.UUID,
    from_ts: datetime = Query(
        ..., alias="from_timestamp", description="Start timestamp (ISO 8601)"
    ),
    to_ts: datetime = Query(
        ..., alias="to_timestamp", description="End timestamp (ISO 8601)"
    ),
    trip_id: uuid.UUID | None = Query(None, description="Optional trip filter"),
    limit: int = Query(default=500, ge=1, le=5000, description="Max points to return"),
    es: AsyncElasticsearch = Depends(get_es),
    db: AsyncSession = Depends(get_db),
) -> BusHistoryPathOut:
    """Get complete GPS path (history) for a bus within time range.

    Example:
    GET /track/bus/550e8400.../history
        ?from_timestamp=2026-07-21T08:00:00Z&to_timestamp=2026-07-21T18:00:00Z
    """

    bus = await db.get(Bus, bus_id)
    if bus is None:
        raise HTTPException(status_code=404, detail=f"Bus {bus_id} not found")

    if from_ts >= to_ts:
        raise HTTPException(
            status_code=400, detail="from_timestamp must be before to_timestamp"
        )

    if trip_id:
        stmt = select(Trip).where(Trip.id == trip_id, Trip.bus_id == bus_id)
        trip = await db.scalar(stmt)
        if trip is None:
            raise HTTPException(
                status_code=404, detail=f"Trip {trip_id} not found for bus {bus_id}"
            )

    filters = [
        {"term": {"bus_id": str(bus_id)}},
        {"range": {"ts": {"gte": from_ts.isoformat(), "lte": to_ts.isoformat()}}},
    ]
    if trip_id:
        filters.append({"term": {"trip_id": str(trip_id)}})

    res = await es.search(
        index="gps_points",
        size=limit,
        query={"bool": {"must": filters}},
        sort=[{"ts": {"order": "asc"}}],
       _source=["bus_id", "trip_id", "ts", "location", "speed", "heading", "accuracy"],
    )

    points: list[GpsPoint] = []
    trip_id_from_data = None

    for hit in res["hits"]["hits"]:
        source = hit["_source"]

        if not trip_id_from_data and source.get("trip_id"):
            trip_id_from_data = source["trip_id"]

        ts_str = source.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        points.append(
            GpsPoint(
                timestamp=ts,
                latitude=float(source["location"]["lat"]),
                longitude=float(source["location"]["lon"]),
                speed=float(source["speed"]) if source.get("speed") else None,
                heading=float(source["heading"]) if source.get("heading") else None,
                accuracy=float(source["accuracy"]) if source.get("accuracy") else None,
            )
        )

    trip_id_result = None
    if trip_id_from_data:
        try:
            trip_id_result = uuid.UUID(trip_id_from_data)
        except (ValueError, TypeError):
            pass

    return BusHistoryPathOut(
        bus_id=bus_id,
        trip_id=trip_id_result,
        from_timestamp=from_ts,
        to_timestamp=to_ts,
        point_count=len(points),
        path=points,
    )
