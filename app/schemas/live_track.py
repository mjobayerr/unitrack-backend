"""Wire format for the live-tracking WebSocket (`/ws/track/{route_id}`, spec §7.3).

One `TrackFrame` is pushed to every subscriber of a route on each tick. It is the
same picture the admin fleet map draws (`FleetBusOut`), narrowed to the buses on
one route and stripped of the columns a student does not need — helper identity,
the route fields they already know from the path they subscribed to.

`GpsFreshness` is reused rather than re-invented so a bus reads the same three
states — live / stale / lost — on the student map and the admin console.
"""

import datetime
import uuid

from pydantic import BaseModel

from app.models.fleet import RouteDirection
from app.schemas.admin import GpsFreshness


class TrackBusFrame(BaseModel):
    """One bus on the route, as the live map draws it right now."""

    trip_id: uuid.UUID
    bus_id: uuid.UUID
    reg_no: str
    nickname: str | None = None

    # Absent when freshness is `lost`: the fix expired or never arrived, so there
    # is no position to place a pin at.
    lat: float | None = None
    lng: float | None = None
    heading: float | None = None
    speed_kmh: float | None = None
    fix_ts: datetime.datetime | None = None
    fix_age_s: int | None = None
    freshness: GpsFreshness

    occupied: int | None = None
    capacity: int | None = None

    # Minutes to the next stop, recomputed from the ETA engine's cached payload.
    next_stop_eta_minutes: int | None = None


class TrackFrame(BaseModel):
    """Every live bus on one route in a single frame.

    Counts ride along so the client can render "2 of 3 buses have gone quiet"
    without walking the list, exactly as `FleetOut` does for the admin console.
    """

    type: str = "positions"
    route_id: uuid.UUID
    generated_at: datetime.datetime
    total: int
    live: int
    stale: int
    lost: int
    buses: list[TrackBusFrame]


class LiveFleetBus(TrackBusFrame):
    """A live bus plus the route it runs.

    The per-route frame leaves route out — the subscriber already knows it from
    the path. The dashboard has picked no route, so it needs the route named on
    every bus to say "Bus 3 · Campus Shuttle" without a second lookup."""

    route_id: uuid.UUID
    route_name: str
    route_direction: RouteDirection


class LiveFleetOut(BaseModel):
    """Every live bus across every route in one snapshot.

    Feeds a dashboard that features whatever is actually running before the
    student has chosen a route — unlike the per-route WebSocket, which only ever
    knows about the one route it was opened for."""

    generated_at: datetime.datetime
    total: int
    live: int
    stale: int
    lost: int
    buses: list[LiveFleetBus]
