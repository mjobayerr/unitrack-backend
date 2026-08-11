import datetime
import enum
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.user import HelperStatus, UserStatus


class HelperOut(BaseModel):
    """A helper account as the admin panel sees it."""

    model_config = ConfigDict(from_attributes=True)

    helper_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    email: str
    phone: str | None
    helper_status: HelperStatus
    user_status: UserStatus
    approved_by: uuid.UUID | None


class GpsFreshness(enum.StrEnum):
    """How much to trust a bus's position (spec §10.2).

    The distinction the console actually needs is not "when was the last fix"
    but "is this pin worth believing". Three states rather than a raw age,
    because that is what a colour on a map can express.
    """

    # A fix within the last minute. Draw it normally.
    live = "live"
    # Between a minute and the Redis TTL. The bus is probably fine and the
    # phone is in a tunnel — spec §10.2 calls for amber here, not alarm.
    stale = "stale"
    # No position at all: the key expired, or this trip has never reported.
    # Either the helper's app is not running or the phone is off the network.
    lost = "lost"


class FleetBusOut(BaseModel):
    """One live bus, as the admin fleet map draws it."""

    trip_id: uuid.UUID
    bus_id: uuid.UUID
    reg_no: str
    nickname: str | None
    route_id: uuid.UUID
    route_name: str
    route_direction: str
    helper_id: uuid.UUID
    helper_name: str
    started_at: datetime.datetime | None

    # Absent when freshness is `lost` — there is simply no position to draw.
    lat: float | None = None
    lng: float | None = None
    heading: float | None = None
    speed_kmh: float | None = None
    fix_ts: datetime.datetime | None = None
    fix_age_s: int | None = None
    freshness: GpsFreshness

    occupied: int | None = None
    capacity: int | None = None

    # Next stop from the ETA engine's cache, when it has run for this trip.
    next_stop_eta_minutes: int | None = None


class FleetOut(BaseModel):
    """The whole live fleet in one response.

    Counts are computed server-side so every client agrees on them, and so the
    console can show "3 of 7 buses have gone quiet" without walking the list.
    """

    generated_at: datetime.datetime
    total: int
    live: int
    stale: int
    lost: int
    buses: list[FleetBusOut]
