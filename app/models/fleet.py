"""Fleet and routing: buses, stops, routes, and the trips that tie them together.

A **trip** is one bus running one route once, driven by one helper. It is the
spine of the whole system: spec §6 requires every GPS point, redemption and seat
report to hang off a trip, because "where was bus 7 at 08:15" is unanswerable
without one.
"""

import datetime
import enum
import uuid

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class BusStatus(enum.StrEnum):
    active = "active"
    inactive = "inactive"
    maintenance = "maintenance"


class Bus(Base):
    __tablename__ = "buses"

    reg_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    status: Mapped[BusStatus] = mapped_column(
        SAEnum(BusStatus, name="bus_status"), nullable=False, default=BusStatus.active
    )


class Stop(Base):
    """A boarding point. Plain lat/lng — geo search lives in Elasticsearch."""

    __tablename__ = "stops"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)


class RouteDirection(enum.StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class Route(Base):
    """An ordered path between stops, in one direction.

    `polyline` is a Google-encoded polyline — the drawn shape of the road, which
    is not derivable from the stop list. Encoded rather than a coordinate array
    because a Dhaka route is a few thousand points and the student PWA fetches
    it on every map load.
    """

    __tablename__ = "routes"
    __table_args__ = (UniqueConstraint("name", "direction", name="uq_routes_name_direction"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[RouteDirection] = mapped_column(
        SAEnum(RouteDirection, name="route_direction"), nullable=False
    )
    polyline: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    stops: Mapped[list["RouteStop"]] = relationship(
        back_populates="route", order_by="RouteStop.seq", cascade="all, delete-orphan"
    )


class RouteStop(Base):
    """One stop's position within one route.

    `scheduled_offset_min` is minutes from trip start, which is what makes a
    per-stop ETA possible before any traffic data exists.
    """

    __tablename__ = "route_stops"
    __table_args__ = (
        UniqueConstraint("route_id", "seq", name="uq_route_stops_route_seq"),
        UniqueConstraint("route_id", "stop_id", name="uq_route_stops_route_stop"),
    )

    route_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stops.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_offset_min: Mapped[int | None] = mapped_column(Integer, nullable=True)

    route: Mapped[Route] = relationship(back_populates="stops")
    stop: Mapped[Stop] = relationship()


class TripStatus(enum.StrEnum):
    scheduled = "scheduled"
    live = "live"
    completed = "completed"
    cancelled = "cancelled"


class Trip(Base):
    """One bus running one route once (spec §6).

    No `schedule_id` yet — the `schedules` table is not built, so trips are
    helper-initiated. The column arrives with recurring timetables; nothing here
    changes when it does.

    Partial unique indexes enforce the two rules that matter, in the database
    rather than in application code: **a bus can be on at most one live trip,
    and so can a helper.** Enforcing this in Python would be a check-then-insert
    race — two taps of Start half a second apart would both pass the check.
    """

    __tablename__ = "trips"
    __table_args__ = (
        # `postgresql_where` makes these apply only to live rows, so a bus may
        # have thousands of completed trips but never two live ones.
        Index(
            "uq_trips_one_live_per_bus",
            "bus_id",
            unique=True,
            postgresql_where="status = 'live'",
        ),
        Index(
            "uq_trips_one_live_per_helper",
            "helper_id",
            unique=True,
            postgresql_where="status = 'live'",
        ),
        # The admin fleet map's query: today's trips, newest first.
        Index("ix_trips_service_date_status", "service_date", "status"),
    )

    route_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bus_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    helper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("helpers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Local service day, not a timestamp: a trip that crosses midnight still
    # belongs to the day it started, which is how ridership is reported.
    service_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    scheduled_start: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_start: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_end: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[TripStatus] = mapped_column(
        SAEnum(TripStatus, name="trip_status"), nullable=False, default=TripStatus.scheduled
    )

    route: Mapped[Route] = relationship()
    bus: Mapped[Bus] = relationship()
