import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class GpsPointIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    ts: datetime  # device clock, ISO 8601 (e.g. 2026-07-14T10:00:00Z)
    speed: float | None = None
    heading: float | None = None
    accuracy: float | None = None


class GpsBatch(BaseModel):
    """A batch of buffered fixes from the helper device (spec §7.3: ~1-10 per ~5s)."""

    bus_id: uuid.UUID
    points: list[GpsPointIn] = Field(min_length=1, max_length=50)


class GpsAccepted(BaseModel):
    accepted: int
    bus_id: uuid.UUID
    # Null means the fixes were stored trip-agnostically because the helper has
    # no live trip. The client should treat that as "start a trip".
    trip_id: uuid.UUID | None = None
