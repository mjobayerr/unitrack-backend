"""Data access for operations: alerts.

Seat reports and alerts are *written* by app/services/ops.py, which stages the
row and lets the caller commit; the reads the admin console makes live here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ops import Alert, AlertStatus


async def get_alert(db: AsyncSession, alert_id: uuid.UUID) -> Alert | None:
    return await db.get(Alert, alert_id)


async def list_alerts(
    db: AsyncSession, status: AlertStatus | None, limit: int
) -> list[Alert]:
    """The emergency console's list — worst first, newest first within severity.

    Backed by `ix_alerts_status_severity`, so the default (open only) view stays
    an index scan rather than a sort of the whole table as history grows.
    """
    stmt = select(Alert)
    if status is not None:
        stmt = stmt.where(Alert.status == status)
    stmt = stmt.order_by(Alert.severity, Alert.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars())
