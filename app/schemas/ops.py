import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.ops import AlertSeverity, AlertSource, AlertStatus, AlertType


class SeatReportIn(BaseModel):
    """What the helper's counter screen sends.

    `occupied` is bounded generously rather than at the bus capacity: a bus does
    run over capacity in Dhaka, and rejecting the report would lose the very
    data point that proves overcrowding.
    """

    occupied: int = Field(ge=0, le=200)
    # Device clock, so a report queued while offline keeps the time it was taken.
    reported_at: datetime.datetime | None = None


class SeatStateOut(BaseModel):
    """The dashboard's headline numbers."""

    trip_id: uuid.UUID
    occupied: int
    capacity: int
    free: int
    reported_at: datetime.datetime


class AlertRaiseIn(BaseModel):
    type: AlertType
    message: str | None = Field(default=None, max_length=2000)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: AlertSource
    type: AlertType
    severity: AlertSeverity
    status: AlertStatus
    message: str | None
    lat: float | None
    lng: float | None
    trip_id: uuid.UUID | None
    bus_id: uuid.UUID | None
    created_at: datetime.datetime


class AlertResolveIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)
