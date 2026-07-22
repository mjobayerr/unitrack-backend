"""Seat reports and alerts (spec §6, §7.6).

Revision ID: d5b2f7c94e18
Revises: c4d8e1f60a35
Create Date: 2026-07-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5b2f7c94e18"
down_revision: str | None = "c4d8e1f60a35"
branch_labels: str | None = None
depends_on: str | None = None

_TIMESTAMPS = (
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)


def upgrade() -> None:
    op.create_table(
        "seat_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trip_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "helper_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("helpers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("occupied", sa.Integer(), nullable=False),
        sa.Column("capacity_snapshot", sa.Integer(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        *_TIMESTAMPS,
    )
    op.create_index("ix_seat_reports_trip_reported", "seat_reports", ["trip_id", "reported_at"])

    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source", sa.Enum("helper", "student", "system", name="alert_source"), nullable=False
        ),
        sa.Column(
            "raised_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "trip_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trips.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "bus_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("buses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "type",
            sa.Enum(
                "sos",
                "breakdown",
                "traffic_delay",
                "accident",
                "harassment",
                "overcrowding",
                "off_route",
                "over_speed",
                "gps_blackout",
                "other",
                name="alert_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Enum("critical", "warning", "info", name="alert_severity"),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("open", "acknowledged", "resolved", "dismissed", name="alert_status"),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "acknowledged_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_note", sa.String(500), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_TIMESTAMPS,
    )
    op.create_index("ix_alerts_status_severity", "alerts", ["status", "severity"])
    op.create_index("ix_alerts_trip_id", "alerts", ["trip_id"])


def downgrade() -> None:
    op.drop_table("alerts")
    op.drop_table("seat_reports")
    for enum_name in ("alert_status", "alert_severity", "alert_type", "alert_source"):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
