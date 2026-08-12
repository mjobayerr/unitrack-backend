"""Development traffic: drive the seeded buses along their route, over HTTP.

    python -m scripts.dev_simulate_gps            # loop until stopped
    python -m scripts.dev_simulate_gps --once     # one batch each, then exit

Or, as part of the stack:

    docker compose --profile demo up -d
    docker compose logs -f simulator

Why it speaks HTTP instead of writing Redis
-------------------------------------------
Both maps read positions that arrive by one path: the helper app posts a batch
to `POST /helper/gps`, which writes `bus:{id}:pos` for the admin console and
XADDs to the `gps_ingest` stream, from which `app.worker.gps_es_indexer` feeds
the Elasticsearch index the student map queries. Writing the two stores directly
would light up both maps *and prove nothing* — every bug in ingest, in the trip
binding, in the stream, in the indexer, would still be there, hidden behind
plausible-looking pins. So this signs in as a real helper and posts real
batches. If the maps show buses, the pipeline works.

That also makes it a load generator and an end-to-end smoke test, not only a
demo: run it and the ETA engine has fixes to chew on, the fleet endpoint has
freshness to classify, and `/track/nearby` has geo documents to sort.

Not for production. It authenticates with the seeded helper passwords, which is
exactly as safe as those passwords are.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import logging
import math
import os
import random
import sys
from datetime import UTC, datetime, timedelta

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulate")
# httpx logs every request at INFO, which at one batch per bus every five seconds
# buries the one line per tick that says where the bus actually is.
logging.getLogger("httpx").setLevel(logging.WARNING)

API_URL = os.environ.get("SIM_API_URL", "http://localhost:8000").rstrip("/")

# `email:password` pairs. Defaults match scripts/seed.py. One bus per helper is
# not a simplification: `uq_trips_one_live_per_helper` allows a helper exactly
# one live trip, so a second bus needs a second account.
HELPERS = os.environ.get(
    "SIM_HELPERS",
    "helper1@buscrew.com.bd:Helper@1234,helper2@buscrew.com.bd:Helper@1234",
)

# Seconds between batches. Matches the helper app's own 5 s cadence, so the
# freshness the console shows here is the freshness it shows in the field.
TICK_S = float(os.environ.get("SIM_TICK_S", "5"))

# Average speed along the corridor. 22 km/h is a Dhaka arterial with traffic —
# fast enough to see a pin move between polls, slow enough to be believable.
SPEED_KMH = float(os.environ.get("SIM_SPEED_KMH", "22"))

# Fixes per batch. A phone buffers about one a second and uploads every five.
FIXES_PER_BATCH = int(os.environ.get("SIM_FIXES_PER_BATCH", "5"))

EARTH_RADIUS_M = 6_371_000.0


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two (lat, lng) points."""
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Compass heading from `a` towards `b`, 0-360."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlng = math.radians(b[1] - a[1])
    y = math.sin(dlng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class Path:
    """A stop sequence turned into something you can stand at a distance along.

    Straight segments between consecutive stops. A real bus follows roads, and
    the `routes.polyline` column exists to hold that shape — but it is empty for
    seeded routes, and a straight line between two Dhaka stops is within a few
    hundred metres of the road for this purpose. If a polyline is ever populated,
    feed its decoded points in here instead and nothing else changes.
    """

    def __init__(self, points: list[tuple[float, float]]) -> None:
        if len(points) < 2:
            raise ValueError("a path needs at least two points")
        self.points = points
        # Cumulative distance to each point, so locating a position is a bisect
        # rather than a walk from the start on every tick.
        self.cumulative = [0.0]
        for prev, nxt in itertools.pairwise(points):
            self.cumulative.append(self.cumulative[-1] + haversine_m(prev, nxt))

    @property
    def length_m(self) -> float:
        return self.cumulative[-1]

    def at(self, distance_m: float) -> tuple[float, float, float]:
        """`(lat, lng, heading)` at `distance_m` from the start.

        Clamped, not wrapped: the caller decides what the end of a route means,
        and silently teleporting a bus back to the depot is not it.
        """
        d = min(max(distance_m, 0.0), self.length_m)

        # Last index whose cumulative distance is <= d. Linear, but over seven
        # stops; a bisect here would be more code than it saves.
        i = 0
        for idx in range(len(self.cumulative) - 1):
            if self.cumulative[idx] <= d:
                i = idx
        start, end = self.points[i], self.points[i + 1]
        span = self.cumulative[i + 1] - self.cumulative[i]
        # Two stops recorded at the same coordinates would divide by zero here.
        t = 0.0 if span == 0 else (d - self.cumulative[i]) / span
        return (
            start[0] + (end[0] - start[0]) * t,
            start[1] + (end[1] - start[1]) * t,
            bearing_deg(start, end),
        )


def jitter(lat: float, lng: float, metres: float = 12.0) -> tuple[float, float]:
    """Nudge a point randomly, so fixes do not fall on a mathematical line.

    Consumer GPS is accurate to roughly 10 m in a city, and the ETA engine reads
    consecutive fixes to infer speed. Perfectly collinear input is not a gentler
    test than noisy input, it is a different one — and the noisy one is what the
    system will actually get.
    """
    dlat = (random.uniform(-1, 1) * metres) / 111_320.0
    dlng = (random.uniform(-1, 1) * metres) / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lng + dlng


# ---------------------------------------------------------------------------
# One simulated bus
# ---------------------------------------------------------------------------


class SimulatedBus:
    """A signed-in helper, a live trip, and a cursor along its route."""

    def __init__(self, client: httpx.AsyncClient, email: str, password: str) -> None:
        self.client = client
        self.email = email
        self.password = password
        self.token: str | None = None
        self.bus_id: str | None = None
        self.trip_id: str | None = None
        self.route_name = "?"
        self.path: Path | None = None
        self.travelled_m = 0.0
        self.direction = 1  # +1 along the route, -1 back
        self.capacity = 40
        self.occupied = 0

    # --- HTTP helpers ------------------------------------------------------

    async def login(self) -> None:
        res = await self.client.post(
            f"{API_URL}/auth/login",
            json={"email": self.email, "password": self.password},
        )
        if res.status_code != 200:
            raise RuntimeError(
                f"login failed for {self.email}: {res.status_code} {res.text.strip()[:200]} "
                "— has the database been seeded?"
            )
        self.token = res.json()["access_token"]

    async def request(self, method: str, path: str, **kw) -> httpx.Response:
        """One call, retried once after a fresh login on 401.

        Access tokens last 15 minutes and this process runs for hours, so
        expiry is the normal case rather than an error. Re-authenticating on 401
        is what the helper app's interceptor does too.
        """
        headers = {"authorization": f"Bearer {self.token}"}
        res = await self.client.request(method, f"{API_URL}{path}", headers=headers, **kw)
        if res.status_code == 401:
            await self.login()
            headers = {"authorization": f"Bearer {self.token}"}
            res = await self.client.request(method, f"{API_URL}{path}", headers=headers, **kw)
        return res

    # --- setup -------------------------------------------------------------

    async def attach(self, shapes: dict[str, dict]) -> None:
        """Find this helper's live trip, or start one, and load its route shape."""
        res = await self.request("GET", "/helper/trips/active")
        res.raise_for_status()
        active = res.json()

        if active is None:
            active = await self._start_trip(shapes)

        self.bus_id = active["bus_id"]
        self.trip_id = active["trip_id"]

        shape = shapes.get(active["route_id"])
        if shape is None:
            raise RuntimeError(
                f"trip {self.trip_id} is on route {active['route_id']}, which has no "
                "stops — seed one with: python -m scripts.seed stops routes"
            )
        self.route_name = f"{shape['name']} {shape['direction']}"
        self.path = Path([(s["stop"]["lat"], s["stop"]["lng"]) for s in shape["stops"]])

        # Start somewhere random rather than all at the first stop, so the two
        # buses are not a single overlapping pin for the first few minutes.
        self.travelled_m = random.uniform(0, self.path.length_m)

        bus = await self._bus_details(self.bus_id)
        self.capacity = bus.get("capacity", 40) if bus else 40
        self.occupied = random.randint(4, max(4, self.capacity // 2))

        logger.info(
            "%s -> bus %s on %s (%.1f km route), starting at %.1f km",
            self.email,
            bus.get("reg_no", self.bus_id[:8]) if bus else self.bus_id[:8],
            self.route_name,
            self.path.length_m / 1000,
            self.travelled_m / 1000,
        )

    async def _start_trip(self, shapes: dict[str, dict]) -> dict:
        """No live trip: pick a free bus and an active route and begin one."""
        buses = (await self.request("GET", "/fleet/buses")).json()
        if not buses:
            raise RuntimeError("no active buses — run: python -m scripts.seed buses")
        if not shapes:
            raise RuntimeError("no active routes — run: python -m scripts.seed stops routes")

        # Try each bus: another simulated helper may already hold the first one,
        # which the API answers with a 409 rather than a silent double-booking.
        last_error = ""
        for bus in buses:
            route_id = random.choice(list(shapes))
            res = await self.request(
                "POST",
                "/helper/trips/start",
                json={"bus_id": bus["id"], "route_id": route_id},
            )
            if res.status_code == 201:
                trip = res.json()
                logger.info("%s started a trip on %s", self.email, bus["reg_no"])
                return {
                    "trip_id": trip["id"],
                    "bus_id": trip["bus_id"],
                    "route_id": trip["route_id"],
                }
            last_error = f"{res.status_code} {res.text.strip()[:120]}"
        raise RuntimeError(f"could not start a trip for {self.email}: {last_error}")

    async def _bus_details(self, bus_id: str) -> dict | None:
        buses = (await self.request("GET", "/fleet/buses")).json()
        return next((b for b in buses if b["id"] == bus_id), None)

    # --- the tick ----------------------------------------------------------

    def advance(self) -> list[dict]:
        """Move along the route and return the fixes covering that movement.

        One fix per second of the tick, timestamped across it, because that is
        the shape ingest is written for: `newest` decides the live position and
        the whole batch goes to history. A single fix per tick would leave the
        ETA engine nothing to derive speed from.
        """
        assert self.path is not None
        step_m = SPEED_KMH * 1000 / 3600 * TICK_S
        now = datetime.now(UTC)
        points: list[dict] = []

        spacing_s = TICK_S / FIXES_PER_BATCH
        for i in range(FIXES_PER_BATCH):
            share = step_m * (i + 1) / FIXES_PER_BATCH
            distance = self.travelled_m + self.direction * share
            lat, lng, heading = self.path.at(distance)
            lat, lng = jitter(lat, lng)
            points.append(
                {
                    "lat": round(lat, 6),
                    "lng": round(lng, 6),
                    # Spread back across the tick that just elapsed, so the batch
                    # reads as a second of driving each rather than five fixes
                    # taken at the same instant. The last one lands on `now`,
                    # which is the fix ingest treats as the live position.
                    "ts": _iso_z(now, -(FIXES_PER_BATCH - 1 - i) * spacing_s),
                    # Metres per second: what geolocator reports on the device,
                    # and what fleet_view converts to km/h for the console.
                    "speed": round(SPEED_KMH * 1000 / 3600, 2),
                    "heading": round(heading if self.direction > 0 else (heading + 180) % 360, 1),
                    "accuracy": round(random.uniform(4, 14), 1),
                }
            )

        self.travelled_m += self.direction * step_m

        # At either end, turn around instead of stopping. A real bus would end
        # the trip and start the opposite route; reversing keeps one trip alive
        # for hours, which is what a long-running demo wants.
        if self.travelled_m >= self.path.length_m:
            self.travelled_m = self.path.length_m
            self.direction = -1
            logger.info("%s reached the end of %s, turning back", self.email, self.route_name)
        elif self.travelled_m <= 0:
            self.travelled_m = 0.0
            self.direction = 1
            logger.info("%s reached the start of %s, turning back", self.email, self.route_name)

        return points

    async def post_fixes(self) -> str:
        points = self.advance()
        res = await self.request(
            "POST", "/helper/gps", json={"bus_id": self.bus_id, "points": points}
        )
        if res.status_code != 202:
            return f"gps rejected: {res.status_code} {res.text.strip()[:160]}"
        last = points[-1]
        return (
            f"{self.route_name}  {last['lat']:.5f},{last['lng']:.5f}  "
            f"{self.travelled_m / 1000:5.2f} km  {len(points)} fixes"
        )

    async def post_seats(self) -> None:
        """Occupancy, so the console's popup has a seat count to show.

        Drifts by a few passengers per report rather than jumping randomly: the
        console shows the latest value, and a bus that swings between 4 and 40
        every minute looks broken rather than busy.
        """
        self.occupied = max(0, min(self.capacity, self.occupied + random.randint(-4, 5)))
        await self.request("POST", "/helper/seats", json={"occupied": self.occupied})


def _iso_z(now: datetime, offset_s: float) -> str:
    """`now + offset_s` as RFC 3339 with a `Z`, which is what a device sends."""
    return (now + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def load_shapes(bus: SimulatedBus) -> dict[str, dict]:
    """Active routes with their ordered stops, keyed by route id."""
    res = await bus.request("GET", "/fleet/route-shapes")
    res.raise_for_status()
    return {r["id"]: r for r in res.json() if len(r["stops"]) >= 2}


async def run(once: bool) -> int:
    credentials = [pair.split(":", 1) for pair in HELPERS.split(",") if ":" in pair]
    if not credentials:
        logger.error("SIM_HELPERS is empty or malformed — expected email:password,email:password")
        return 1

    async with httpx.AsyncClient(timeout=15) as client:
        buses = [SimulatedBus(client, email.strip(), password) for email, password in credentials]

        for bus in buses:
            await bus.login()
        shapes = await load_shapes(buses[0])
        for bus in buses:
            await bus.attach(shapes)

        logger.info(
            "driving %d bus(es) at %.0f km/h, one batch every %.0fs — Ctrl-C to stop",
            len(buses),
            SPEED_KMH,
            TICK_S,
        )

        # Seat counts are reported far less often than positions: it is a number
        # a helper taps in, not a sensor reading, and `bus:{id}:seats` holds it
        # for 15 minutes.
        seats_every = max(1, int(60 / TICK_S))

        for tick in itertools.count():
            for bus in buses:
                try:
                    logger.info("  %-28s %s", bus.email, await bus.post_fixes())
                    if tick % seats_every == 0:
                        await bus.post_seats()
                except httpx.HTTPError as exc:
                    # The API restarting mid-run is ordinary in development. Log
                    # it and try again on the next tick rather than exiting and
                    # leaving the maps to go stale.
                    logger.warning("  %-28s network error: %s", bus.email, exc)
            if once:
                return 0
            await asyncio.sleep(TICK_S)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.dev_simulate_gps", description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Post one batch per bus and exit, instead of looping.",
    )
    args = parser.parse_args()

    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(run(args.once)))


if __name__ == "__main__":
    main()
