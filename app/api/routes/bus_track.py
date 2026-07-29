from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.redis import bus_pos_key, get_redis

if TYPE_CHECKING:
    from redis.asyncio import Redis
else:
    Redis = Any

router = APIRouter(prefix="/bus-track", tags=["bus-track"])


@router.get("", status_code=status.HTTP_200_OK)
async def get_bus_current_location(
    bus_id: str = Query(..., description="Bus identifier to look up"),
    r: Redis = Depends(get_redis),
) -> dict[str, str | None]:
    """Return the most recently cached GPS position for a bus from Redis."""
    pos = await r.hgetall(bus_pos_key(bus_id))
    if not pos:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bus location not found")

    return {
        "bus_id": bus_id,
        "lat": pos.get("lat"),
        "lng": pos.get("lng"),
        "ts": pos.get("ts"),
    }
