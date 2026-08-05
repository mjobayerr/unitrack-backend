from fastapi.testclient import TestClient

from app.api.routes import bus_track as bus_track_routes
from app.main import create_app


class FakeRedis:
    def __init__(self, payload: dict[str, str]):
        self.payload = payload

    async def hgetall(self, key: str) -> dict[str, str]:
        return self.payload


async def fake_get_redis() -> FakeRedis:
    return FakeRedis({"lat": "23.7801", "lng": "90.4123", "ts": "2025-01-01T00:00:00Z"})


def test_bus_track_returns_current_location_from_redis() -> None:
    app = create_app()
    app.dependency_overrides[bus_track_routes.get_redis] = fake_get_redis

    with TestClient(app) as client:
        response = client.get("/bus-track", params={"bus_id": "bus-123"})

    assert response.status_code == 200
    assert response.json() == {
        "bus_id": "bus-123",
        "lat": "23.7801",
        "lng": "90.4123",
        "ts": "2025-01-01T00:00:00Z",
    }
    assert response.json()["bus_id"] == "bus-123"
