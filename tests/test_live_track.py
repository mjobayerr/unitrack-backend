"""Live-tracking WebSocket + frame assembly (`/ws/track/{route_id}`, spec §7.3).

Dependency-free like the rest of `tests/`: the frame builder is a pure function
tested directly, and the WebSocket is exercised with fakes wired through
`dependency_overrides` and `monkeypatch`, so no Postgres, Redis or broker runs.
The auth-coverage test cannot see a WebSocket route, so its guarding is pinned
here instead.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api.routes import ws_track
from app.core.authz import Principal
from app.core.security import create_access_token
from app.main import create_app
from app.models.user import UserRole, UserStatus
from app.schemas.admin import GpsFreshness
from app.services import live_track
from app.services.live_track import LiveTripRef, assemble_frame, build_frame

ROUTE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _ref(capacity: int = 45) -> LiveTripRef:
    return LiveTripRef(
        trip_id=uuid.uuid4(),
        bus_id=uuid.uuid4(),
        reg_no="DHK-01",
        nickname="Green Line",
        capacity=capacity,
    )


# --------------------------------------------------------------------------
# assemble_frame — pure, the heart of every frame
# --------------------------------------------------------------------------


def test_assemble_frame_classifies_live_and_lost_and_tallies() -> None:
    now = datetime(2026, 7, 21, 10, 0, 0, tzinfo=UTC)
    live_ref = _ref()
    lost_ref = _ref(capacity=50)

    pos = {
        "lat": "23.78",
        "lng": "90.40",
        "speed": "5.0",  # m/s → 18.0 km/h
        "heading": "90",
        "ts": (now - timedelta(seconds=10)).isoformat(),
    }
    seats = {"occupied": "30", "capacity": "45"}
    eta = f'{{"arrivals":[{{"eta":"{(now + timedelta(minutes=3)).isoformat()}"}}]}}'

    # Flat pipeline output: (pos, seats, eta) per ref, in roster order. The lost
    # bus has no position at all — the key expired or never reported.
    redis_results = [pos, seats, eta, None, None, None]

    frame = assemble_frame(ROUTE_ID, [live_ref, lost_ref], redis_results, now)

    assert frame.total == 2
    assert frame.live == 1
    assert frame.lost == 1
    assert frame.stale == 0

    live_bus, lost_bus = frame.buses
    assert live_bus.freshness is GpsFreshness.live
    assert (live_bus.lat, live_bus.lng) == (23.78, 90.40)
    assert live_bus.speed_kmh == 18.0
    assert live_bus.occupied == 30
    assert live_bus.capacity == 45
    assert live_bus.next_stop_eta_minutes == 3

    assert lost_bus.freshness is GpsFreshness.lost
    assert lost_bus.lat is None
    # Falls back to the bus's configured capacity before any seat report.
    assert lost_bus.capacity == 50


def test_assemble_frame_empty_roster_is_an_idle_frame() -> None:
    now = datetime.now(UTC)
    frame = assemble_frame(ROUTE_ID, [], [], now)
    assert frame.total == 0
    assert frame.buses == []
    assert (frame.live, frame.stale, frame.lost) == (0, 0, 0)


# --------------------------------------------------------------------------
# build_frame — orchestration over a fake Redis pipeline
# --------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self, results: list) -> None:
        self._results = results

    def hgetall(self, key: str) -> None:  # recorded shape only; results are canned
        return None

    def get(self, key: str) -> None:
        return None

    async def execute(self) -> list:
        return self._results


class _FakeRedis:
    """Minimal async Redis: a canned pipeline plus the two reads auth makes."""

    def __init__(self, pipeline_results: list, principal_json: str | None = None) -> None:
        self._pipeline_results = pipeline_results
        self._principal_json = principal_json

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self._pipeline_results)

    async def exists(self, key: str) -> int:
        return 0  # nothing is revoked

    async def get(self, key: str) -> str | None:
        if self._principal_json and key.startswith("authz:principal:"):
            return self._principal_json
        return None

    async def set(self, *args, **kwargs) -> None:
        return None


async def test_build_frame_reads_the_pipeline_for_each_bus() -> None:
    now = datetime.now(UTC)
    ref = _ref()
    pos = {"lat": "23.7", "lng": "90.4", "ts": now.isoformat()}
    redis = _FakeRedis([pos, {"occupied": "10", "capacity": "45"}, None])

    frame = await build_frame(db=None, r=redis, route_id=ROUTE_ID, now=now, refs=[ref])

    assert frame.total == 1
    (bus,) = frame.buses
    assert bus.bus_id == ref.bus_id
    assert bus.occupied == 10
    assert bus.freshness is GpsFreshness.live


# --------------------------------------------------------------------------
# WebSocket auth — the guarding the coverage test cannot see
# --------------------------------------------------------------------------


def _active_student() -> tuple[uuid.UUID, str, str]:
    """A user id, a valid access token for it, and its cached-Principal JSON."""
    user_id = uuid.uuid4()
    token = create_access_token(sub=str(user_id), role=UserRole.student.value)
    principal_json = Principal(
        user_id=user_id, role=UserRole.student, status=UserStatus.active
    ).to_json()
    return user_id, token, principal_json


def _client_with_redis(redis: _FakeRedis) -> TestClient:
    app = create_app()

    async def _fake_get_redis() -> _FakeRedis:
        return redis

    async def _fake_get_db():
        # Every DB access is monkeypatched away in these tests; the session is a
        # sentinel that must never be queried.
        yield object()

    app.dependency_overrides[ws_track.get_redis] = _fake_get_redis
    app.dependency_overrides[ws_track.get_db] = _fake_get_db
    return TestClient(app)


def test_ws_rejects_missing_token() -> None:
    client = _client_with_redis(_FakeRedis([]))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/track/{ROUTE_ID}"):
            pass
    assert exc.value.code == ws_track.WS_UNAUTHORIZED


def test_ws_rejects_invalid_token() -> None:
    client = _client_with_redis(_FakeRedis([]))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/track/{ROUTE_ID}?token=not-a-jwt"):
            pass
    assert exc.value.code == ws_track.WS_UNAUTHORIZED


def test_ws_rejects_unknown_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _, token, principal_json = _active_student()
    monkeypatch.setattr(live_track, "route_exists", _async_return(False))

    client = _client_with_redis(_FakeRedis([], principal_json=principal_json))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/track/{ROUTE_ID}?token={token}"):
            pass
    assert exc.value.code == ws_track.WS_ROUTE_NOT_FOUND


def test_ws_streams_a_frame_for_a_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _, token, principal_json = _active_student()
    ref = _ref()
    now_iso = datetime.now(UTC).isoformat()
    pos = {"lat": "23.78", "lng": "90.40", "ts": now_iso}
    redis = _FakeRedis(
        [pos, {"occupied": "12", "capacity": "45"}, None],
        principal_json=principal_json,
    )

    monkeypatch.setattr(live_track, "route_exists", _async_return(True))
    monkeypatch.setattr(live_track.roster_cache, "get", _async_return([ref]))

    client = _client_with_redis(redis)
    with client.websocket_connect(f"/ws/track/{ROUTE_ID}?token={token}") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "positions"
    assert frame["route_id"] == str(ROUTE_ID)
    assert frame["total"] == 1
    (bus,) = frame["buses"]
    assert bus["bus_id"] == str(ref.bus_id)
    assert bus["occupied"] == 12
    assert bus["freshness"] == "live"


def _async_return(value):
    """Build an async function that ignores its args and returns `value` —
    a stand-in for the DB-backed helpers these tests monkeypatch out."""

    async def _fn(*args, **kwargs):
        return value

    return _fn
