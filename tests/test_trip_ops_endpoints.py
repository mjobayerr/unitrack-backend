"""HTTP-level coverage for the helper command endpoints — trips, seats, alerts.

Each of these four handlers does a database write *and* a Redis side-effect:
cache the active trip, publish a live seat count, fan an alert out to the admin
console. Until now only the integration smoke test exercised them, so nothing in
the dependency-free suite caught a regression in that write-then-side-effect
sequence. These do — with a SQLite database and a stateful fake Redis wired
through `dependency_overrides`, the same approach as `test_bus_api` and
`test_live_track`.

They assert *observable* behaviour — what lands in the database and what reaches
Redis — not how the handler is wired, so they hold across a refactor of where
the commit and the side-effect live (see app/services/trip.py, ops.py).
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db, get_principal
from app.core.authz import Principal
from app.core.redis import bus_seats_key, get_redis, helper_trip_key
from app.db.base import Base
from app.main import create_app
from app.models.fleet import Bus, BusStatus, Route, RouteDirection, Trip, TripStatus
from app.models.ops import Alert, AlertSeverity, SeatReport
from app.models.user import HelperStatus, UserRole, UserStatus

HELPER_ID = uuid.uuid4()


class _FakePipeline:
    """Records the pipeline ops and applies them to the parent fake on execute()."""

    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    def hset(self, key: str, mapping: dict) -> "_FakePipeline":
        self._ops.append(("hset", key, mapping))
        return self

    def expire(self, key: str, ttl: int) -> "_FakePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    def publish(self, channel: str, message: str) -> "_FakePipeline":
        self._ops.append(("publish", channel, message))
        return self

    async def execute(self) -> list:
        out: list = []
        for kind, *rest in self._ops:
            if kind == "hset":
                self._redis.hashes[rest[0]] = {k: str(v) for k, v in rest[1].items()}
                out.append(1)
            elif kind == "expire":
                out.append(True)
            elif kind == "publish":
                self._redis.published.append((rest[0], rest[1]))
                out.append(1)
        return out


class _FakeRedis:
    """In-memory async Redis: enough for the active-trip cache, seats hash, pub/sub."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        for key in keys:
            self.store.pop(key, None)
            self.hashes.pop(key, None)
        return 1

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def seeded(db) -> tuple[Bus, Route]:
    """An active bus and route the helper can start a trip on."""
    bus = Bus(reg_no="DHK-9001", capacity=45, status=BusStatus.active)
    route = Route(name="Test Line", direction=RouteDirection.outbound, is_active=True)
    db.add_all([bus, route])
    await db.commit()
    return bus, route


@pytest.fixture
def redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def client(db, redis) -> AsyncClient:
    app = create_app()

    async def _get_db():
        yield db

    async def _get_redis():
        return redis

    async def _get_principal() -> Principal:
        return Principal(
            user_id=uuid.uuid4(),
            role=UserRole.helper,
            status=UserStatus.active,
            helper_id=HELPER_ID,
            helper_status=HelperStatus.approved,
        )

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    app.dependency_overrides[get_principal] = _get_principal
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _start(c: AsyncClient, bus: Bus, route: Route):
    return await c.post(
        "/api/v1/helper/trips/start",
        json={"bus_id": str(bus.id), "route_id": str(route.id)},
    )


async def test_start_trip_persists_a_live_trip_and_caches_it(client, db, redis, seeded):
    bus, route = seeded
    async with client as c:
        resp = await _start(c, bus, route)

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "live"
    assert body["bus_id"] == str(bus.id)

    trip = (await db.execute(select(Trip))).scalar_one()
    assert trip.status is TripStatus.live
    # The active-trip cache is warmed so GPS ingest can bind fixes from Redis.
    assert helper_trip_key(str(HELPER_ID)) in redis.store


async def test_active_trip_reports_the_started_trip(client, seeded):
    bus, route = seeded
    async with client as c:
        await _start(c, bus, route)
        active = await c.get("/api/v1/helper/trips/active")
    assert active.status_code == 200
    assert active.json()["bus_id"] == str(bus.id)


async def test_end_trip_completes_it_and_clears_the_cache(client, db, redis, seeded):
    bus, route = seeded
    async with client as c:
        await _start(c, bus, route)
        assert helper_trip_key(str(HELPER_ID)) in redis.store

        ended = await c.post("/api/v1/helper/trips/end")
        assert ended.status_code == 200
        assert ended.json()["status"] == "completed"
        # Cleared, so a finished trip stops binding a draining outbox's fixes.
        assert helper_trip_key(str(HELPER_ID)) not in redis.store

        after = await c.get("/api/v1/helper/trips/active")
    assert after.json() is None

    trip = (await db.execute(select(Trip))).scalar_one()
    assert trip.status is TripStatus.completed
    assert trip.actual_end is not None


async def test_start_trip_rejects_an_unknown_bus(client, seeded):
    _, route = seeded
    async with client as c:
        resp = await c.post(
            "/api/v1/helper/trips/start",
            json={"bus_id": str(uuid.uuid4()), "route_id": str(route.id)},
        )
    assert resp.status_code == 400


async def test_report_seats_persists_and_pushes_the_live_value(client, db, redis, seeded):
    bus, route = seeded
    async with client as c:
        await _start(c, bus, route)
        resp = await c.post("/api/v1/helper/seats", json={"occupied": 30})

    assert resp.status_code == 201
    body = resp.json()
    assert (body["occupied"], body["capacity"], body["free"]) == (30, 45, 15)

    report = (await db.execute(select(SeatReport))).scalar_one()
    assert report.occupied == 30
    # Live seat value pushed to the bus's seats hash for the fleet map.
    assert redis.hashes.get(bus_seats_key(str(bus.id)), {}).get("occupied") == "30"


async def test_report_seats_without_a_live_trip_is_409(client, seeded):
    async with client as c:
        resp = await c.post("/api/v1/helper/seats", json={"occupied": 10})
    assert resp.status_code == 409


async def test_raise_alert_sets_server_severity_and_publishes(client, db, redis, seeded):
    async with client as c:
        resp = await c.post("/api/v1/helper/alerts", json={"type": "sos"})

    assert resp.status_code == 201
    assert resp.json()["severity"] == "critical"

    alert = (await db.execute(select(Alert))).scalar_one()
    assert alert.severity is AlertSeverity.critical
    # Fanned out to the admin console's alerts channel.
    assert len(redis.published) == 1
