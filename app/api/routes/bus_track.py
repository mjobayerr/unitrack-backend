from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.redis import bus_pos_key, get_redis

if TYPE_CHECKING:
    from redis.asyncio import Redis
else:
    Redis = Any


class BusLocationOut(BaseModel):
    bus_id: str
    lat: str | None = None
    lng: str | None = None
    ts: str | None = None


def _decode_redis_value(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


router = APIRouter(prefix="/bus-track", tags=["bus-track"])


@router.get("", response_model=BusLocationOut, status_code=status.HTTP_200_OK)
async def get_bus_current_location(
    bus_id: str = Query(..., description="Bus identifier to look up"),
    r: Redis = Depends(get_redis),
) -> BusLocationOut:
    """Return the most recently cached GPS position for a bus from Redis."""
    pos = await r.hgetall(bus_pos_key(bus_id))
    if not pos:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bus location not found")

    return BusLocationOut(
        bus_id=bus_id,
        lat=_decode_redis_value(pos.get("lat")),
        lng=_decode_redis_value(pos.get("lng")),
        ts=_decode_redis_value(pos.get("ts")),
    )
