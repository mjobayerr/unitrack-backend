from redis.asyncio import Redis

from app.core.config import settings

_client: Redis | None = None

# How long the GPS indexer parks in XREADGROUP waiting for fixes.
GPS_BLOCK_MS = 5000

# redis-py 8 defaults socket_timeout to 5s, which a blocking read is measured
# against. At the default it ties GPS_BLOCK_MS exactly, so an idle stream raises
# TimeoutError and kills the consumer. Stay clear of the block window.
SOCKET_TIMEOUT_S = GPS_BLOCK_MS / 1000 + 5


def get_redis_client() -> Redis:
    """Process-wide async Redis client (decoded str responses)."""
    global _client
    if _client is None:
        _client = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT_S,
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


def helper_trip_key(helper_id: str) -> str:
    """The helper's live trip, cached so GPS ingest never queries Postgres.

    Ingest runs every 5 s per bus and needs one fact: which trip do these fixes
    belong to. Reading it from Redis keeps the hottest write path in the system
    free of database round trips.
    """
    return f"helper:{helper_id}:trip"


# A trip longer than this is abandoned, not running — a helper who force-closed
# the app mid-route. The key expiring stops a stale trip from silently
# collecting fixes for days; the row in Postgres stays the source of truth.
ACTIVE_TRIP_TTL_S = 16 * 60 * 60
