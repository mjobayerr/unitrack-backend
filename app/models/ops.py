"""Operational telemetry the helper reports by hand: seat counts and alerts.

Both hang off a trip (spec §6). Neither can be derived from GPS — the helper's
eyes are the sensor, which is the same constraint that put the phone on the bus
in the first place.
"""

import datetime
import enum
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SeatReport(Base):
    """A point-in-time occupancy count for one trip.

    Append-only rather than a mutable counter on `trips`: "how full was the
    07:30 bus at Farmgate" is a reporting question, and overwriting a single
    column would destroy the answer. The latest value lives in Redis for the
    live view; this table is the history.
    """

    __tablename__ = "seat_reports"
    __table_args__ = (
        # The read pattern: this trip's counts, newest first.
        Index("ix_seat_reports_trip_reported", "trip_id", "reported_at"),
    )

    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trips.id", ondelete="CASCADE"), nullable=False
    )
    helper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("helpers.id", ondelete="RESTRICT"), nullable=False
    )
    occupied: Mapped[int] = mapped_column(Integer, nullable=False)
    # Snapshotted, not joined: a bus re-seated from 50 to 45 next year must not
    # silently rewrite what last year's occupancy percentages meant.
    capacity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    reported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AlertSource(enum.StrEnum):
    helper = "helper"
    student = "student"
    system = "system"


class AlertType(enum.StrEnum):
    sos = "sos"
    breakdown = "breakdown"
    traffic_delay = "traffic_delay"
    accident = "accident"
    harassment = "harassment"
    overcrowding = "overcrowding"
    off_route = "off_route"
    over_speed = "over_speed"
    gps_blackout = "gps_blackout"
    other = "other"


class AlertSeverity(enum.StrEnum):
    critical = "critical"
    warning = "warning"
    info = "info"


class AlertStatus(enum.StrEnum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    dismissed = "dismissed"


class Alert(Base):
    """Something went wrong (spec §7.6).

    Raised by a helper from the emergency screen, or later by a worker that
    notices a bus off-route or gone dark. `raised_by` is null for those.

    Position is copied in rather than looked up from the GPS store: an SOS is
    about where the bus was *when the button was pressed*, and the admin console
    must still answer that if the phone dies immediately afterwards.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        # The admin console's only hot query: open alerts, worst first.
        Index("ix_alerts_status_severity", "status", "severity"),
        Index("ix_alerts_trip_id", "trip_id"),
    )

    source: Mapped[AlertSource] = mapped_column(
        SAEnum(AlertSource, name="alert_source"), nullable=False
    )
    raised_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trips.id", ondelete="SET NULL"), nullable=True
    )
    bus_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buses.id", ondelete="SET NULL"), nullable=True
    )

    type: Mapped[AlertType] = mapped_column(SAEnum(AlertType, name="alert_type"), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(
        SAEnum(AlertSeverity, name="alert_severity"), nullable=False
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[AlertStatus] = mapped_column(
        SAEnum(AlertStatus, name="alert_status"), nullable=False, default=AlertStatus.open
    )
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
