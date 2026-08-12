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
    # The bus the fixes were filed against, which is the *trip's* bus — not
    # necessarily the one the client named. The server decides.
    bus_id: uuid.UUID
    # The trip the fixes were filed under: the live one, or the trip this helper
    # just ended on this bus if a queued batch is still draining. Never null now
    # — ingest refuses a batch it cannot attribute to a trip. Kept optional only
    # so an older client still parses the response.
    trip_id: uuid.UUID | None = None
