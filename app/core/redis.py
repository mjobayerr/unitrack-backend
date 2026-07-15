from redis.asyncio import Redis

from app.core.config import settings

_client: Redis | None = None


def get_redis_client() -> Redis:
    """Process-wide async Redis client (decoded str responses)."""
    global _client
    if _client is None:
        _client = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
    return _client


async def get_redis() -> Redis:
    """FastAPI dependency."""
    return get_redis_client()


# --- keyspace helpers (spec §6 Redis keyspace) ---

GPS_STREAM = "gps_ingest"
# ES is the sole GPS store; its indexer is the only consumer group on the stream.
GPS_ES_CONSUMER_GROUP = "es_indexers"


def bus_pos_key(bus_id: str) -> str:
    return f"bus:{bus_id}:pos"


def fleet_channel() -> str:
    return "fleet:ch"
