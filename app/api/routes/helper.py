from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_helper
from app.core.redis import GPS_STREAM, bus_pos_key, fleet_channel, get_redis
from app.db.session import get_db
from app.models.fleet import Bus
from app.models.user import Helper
from app.schemas.gps import GpsAccepted, GpsBatch

router = APIRouter(prefix="/helper", tags=["helper"])


@router.post("/gps", response_model=GpsAccepted, status_code=status.HTTP_202_ACCEPTED)
async def ingest_gps(
    batch: GpsBatch,
    helper: Helper = Depends(get_current_helper),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> GpsAccepted:
    """Receive a batch of GPS fixes from a helper device (spec §7.3).

    Writes the newest fix to Redis (`bus:{id}:pos`, TTL 60s), publishes to the
    fleet channel, and XADDs every point to the `gps_ingest` stream. The worker
    consumes the stream and persists to `gps_points` — so the flow is
    helper -> Redis -> Postgres.
    """
    bus = await db.get(Bus, batch.bus_id)
    if bus is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown bus")

    bus_id = str(batch.bus_id)
    newest = max(batch.points, key=lambda p: p.ts)

    pos = {
        "lat": str(newest.lat),
        "lng": str(newest.lng),
        "speed": str(newest.speed) if newest.speed is not None else "",
        "heading": str(newest.heading) if newest.heading is not None else "",
        "ts": newest.ts.astimezone(UTC).isoformat(),
        "ingested_at": datetime.now(UTC).isoformat(),
    }
    await r.hset(bus_pos_key(bus_id), mapping=pos)
    await r.expire(bus_pos_key(bus_id), 60)
    await r.publish(fleet_channel(), f"{bus_id}:{newest.lat},{newest.lng}")

    for p in batch.points:
        await r.xadd(
            GPS_STREAM,
            {
                "bus_id": bus_id,
                "helper_id": str(helper.id),
                "ts": p.ts.astimezone(UTC).isoformat(),
                "lat": str(p.lat),
                "lng": str(p.lng),
                "speed": str(p.speed) if p.speed is not None else "",
                "heading": str(p.heading) if p.heading is not None else "",
                "accuracy": str(p.accuracy) if p.accuracy is not None else "",
            },
        )

    return GpsAccepted(accepted=len(batch.points), bus_id=batch.bus_id)
