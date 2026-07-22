"""Seat reports and alerts — the two things the helper reports by hand."""

from __future__ import annotations

import datetime
import json
import uuid

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import SEATS_TTL_S, alerts_channel, bus_seats_key
from app.models.fleet import Bus
from app.models.ops import Alert, AlertSeverity, AlertSource, AlertType, SeatReport

# Severity is decided by the server, never sent by the client. A phone that can
# label its own alerts would label every one of them critical, and the admin
# console's triage order is only as good as this mapping.
_SEVERITY: dict[AlertType, AlertSeverity] = {
    AlertType.sos: AlertSeverity.critical,
    AlertType.accident: AlertSeverity.critical,
    AlertType.harassment: AlertSeverity.critical,
    AlertType.breakdown: AlertSeverity.critical,
    AlertType.traffic_delay: AlertSeverity.warning,
    AlertType.overcrowding: AlertSeverity.warning,
    AlertType.off_route: AlertSeverity.warning,
    AlertType.over_speed: AlertSeverity.warning,
    AlertType.gps_blackout: AlertSeverity.warning,
    AlertType.other: AlertSeverity.info,
}


def severity_for(alert_type: AlertType) -> AlertSeverity:
    return _SEVERITY.get(alert_type, AlertSeverity.info)


async def report_seats(
    db: AsyncSession,
    r: Redis,
    *,
    trip_id: uuid.UUID,
    helper_id: uuid.UUID,
    bus: Bus,
    occupied: int,
    reported_at: datetime.datetime | None,
) -> SeatReport:
    """Append an occupancy count and refresh the live value."""
    report = SeatReport(
        trip_id=trip_id,
        helper_id=helper_id,
        occupied=occupied,
        capacity_snapshot=bus.capacity,
        reported_at=reported_at or datetime.datetime.now(datetime.UTC),
    )
    db.add(report)
    await db.commit()

    key = bus_seats_key(str(bus.id))
    pipe = r.pipeline(transaction=False)
    pipe.hset(
        key,
        mapping={
            "occupied": str(occupied),
            "capacity": str(bus.capacity),
            "trip_id": str(trip_id),
            "ts": report.reported_at.astimezone(datetime.UTC).isoformat(),
        },
    )
    pipe.expire(key, SEATS_TTL_S)
    await pipe.execute()
    return report


async def raise_alert(
    db: AsyncSession,
    r: Redis,
    *,
    source: AlertSource,
    alert_type: AlertType,
    raised_by: uuid.UUID | None,
    trip_id: uuid.UUID | None,
    bus_id: uuid.UUID | None,
    message: str | None,
    lat: float | None,
    lng: float | None,
) -> Alert:
    """Record an alert and push it to the admin console.

    The database write happens first and the publish second: a dropped pub/sub
    message costs a console refresh, whereas a lost row loses the incident.
    """
    alert = Alert(
        source=source,
        type=alert_type,
        severity=severity_for(alert_type),
        raised_by=raised_by,
        trip_id=trip_id,
        bus_id=bus_id,
        message=message,
        lat=lat,
        lng=lng,
    )
    db.add(alert)
    await db.commit()

    try:
        await r.publish(
            alerts_channel(),
            json.dumps(
                {
                    "id": str(alert.id),
                    "type": alert.type,
                    "severity": alert.severity,
                    "bus_id": str(bus_id) if bus_id else None,
                    "trip_id": str(trip_id) if trip_id else None,
                    "lat": lat,
                    "lng": lng,
                }
            ),
        )
    except Exception:  # noqa: BLE001 — the incident is already durable
        pass
    return alert
