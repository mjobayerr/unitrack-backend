"""drop gps_points (ES-only GPS)

Revision ID: b7f3c1a9d2e4
Revises: 24a04e22dd61
Create Date: 2026-07-14 23:10:00.000000

GPS history now lives only in Elasticsearch (index `gps_points`). Postgres keeps
identity/fleet/commerce; `buses` stays. This drops the relational gps_points
table, its index and FK. Downgrade recreates it (empty).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7f3c1a9d2e4"
down_revision: str | None = "24a04e22dd61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_gps_points_bus_id"), table_name="gps_points")
    op.drop_table("gps_points")


def downgrade() -> None:
    op.create_table(
        "gps_points",
        sa.Column("trip_id", sa.UUID(), nullable=True),
        sa.Column("bus_id", sa.UUID(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("speed", sa.Float(), nullable=True),
        sa.Column("heading", sa.Float(), nullable=True),
        sa.Column("accuracy", sa.Float(), nullable=True),
        sa.Column("matched_route_pct", sa.Float(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["bus_id"], ["buses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_gps_points_bus_id"), "gps_points", ["bus_id"], unique=False)
