import datetime
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.fleet import TripStatus


class TripStartRequest(BaseModel):
    """What the helper picks in the app before pressing Start."""

    bus_id: uuid.UUID
    route_id: uuid.UUID


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    route_id: uuid.UUID
    bus_id: uuid.UUID
    helper_id: uuid.UUID
    service_date: datetime.date
    status: TripStatus
    actual_start: datetime.datetime | None
    actual_end: datetime.datetime | None


class ActiveTripOut(BaseModel):
    """The helper app's answer to "am I already tracking?" after a restart."""

    trip_id: uuid.UUID
    bus_id: uuid.UUID
    route_id: uuid.UUID
