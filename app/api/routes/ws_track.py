"""Live-tracking WebSocket: `/ws/track/{route_id}` (spec §7.3 step 4).

Replaces the client poll. A student subscribes to a route and is pushed a
`TrackFrame` — every live bus's position, freshness, seats and next-stop ETA —
every `track_ws_interval_s`, instead of asking `GET /admin/fleet`-style over HTTP
on a timer and paying a request per tick.

Auth on a WebSocket
-------------------
A browser's `WebSocket` cannot set an `Authorization` header, so the access
token arrives as `?token=` on the handshake URL and is validated here by hand —
the same three checks the HTTP guard makes (`app/api/deps.py`): decode as an
*access* token, reject if its `jti` is revoked, resolve the cached `Principal`
and require an active account. Any signed-in role may watch (spec §8: students
their routes, helpers their trip, admins the fleet); the positions are the
fleet's whereabouts, so this is never public.

This route is intentionally **not** in `PUBLIC_PATHS`. The auth-coverage test
only inspects HTTP `APIRoute`s, so a WebSocket route is neither checked nor
allowed-listed there — the guarding is this handler's own responsibility, which
`tests/test_live_track.py` pins.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import jwt
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketState

from app.core.authz import Principal, get_principal_cached
from app.core.config import settings
from app.core.redis import get_redis
from app.core.revocation import is_revoked
from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import UserStatus
from app.services import live_track

logger = logging.getLogger("unitrack.ws.track")

# Application close codes (the 4000–4999 range is reserved for the app). The
# client reads these to tell "log in again" from "no such route" from a normal
# close, which a bare 1000/1006 cannot express.
WS_UNAUTHORIZED = 4401
WS_ROUTE_NOT_FOUND = 4404

router = APIRouter(prefix="/ws", tags=["tracking"])


async def authenticate(websocket: WebSocket, r: Redis, db: AsyncSession) -> Principal | None:
    """Resolve the `?token=` query param to an active `Principal`, or `None`.

    Mirrors `get_access_claims` + `get_principal` from the HTTP guard, kept
    deliberately in step with it: decode as access, reject a revoked `jti`, load
    the cached snapshot and require it to be active. Returns `None` on any
    failure — the caller closes with `WS_UNAUTHORIZED`, and the reason is never
    disclosed, for the same enumeration reason the HTTP `_CREDENTIALS_EXC` is.
    """
    token = websocket.query_params.get("token")
    if not token:
        return None
    try:
        claims = decode_token(token, expected_type="access")
    except jwt.InvalidTokenError:
        return None
    if await is_revoked(r, claims["jti"]):
        return None
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        return None
    principal = await get_principal_cached(r, db, user_id)
    if principal is None or principal.status is not UserStatus.active:
        return None
    return principal


@router.websocket("/track/{route_id}")
async def track_route(
    websocket: WebSocket,
    route_id: uuid.UUID,
    r: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Stream live positions for one route until the client goes away.

    Closes before `accept()` on a bad token or an unknown route, so a rejected
    client sees a close code on the handshake rather than an open socket that
    never sends. After accepting it pushes an immediate first frame — no waiting
    a whole interval for the map to populate — then one per tick.
    """
    principal = await authenticate(websocket, r, db)
    if principal is None:
        await websocket.close(code=WS_UNAUTHORIZED)
        return
    if not await live_track.route_exists(db, route_id):
        await websocket.close(code=WS_ROUTE_NOT_FOUND)
        return

    await websocket.accept()
    try:
        await _stream(websocket, r, db, route_id)
    except WebSocketDisconnect:
        # The ordinary way a stream ends: the student closed the tab. Nothing to
        # clean up — the session's teardown runs via the get_db dependency.
        pass


async def _stream(websocket: WebSocket, r: Redis, db: AsyncSession, route_id: uuid.UUID) -> None:
    """Send a frame, then wait one interval — or until the client disconnects.

    The wait is a `receive()` with a timeout, which does double duty: it paces
    the stream to `track_ws_interval_s` and it notices the client leaving the
    instant it happens, rather than a whole interval later on the next send.
    `receive()` returns the disconnect *message* rather than raising, so the loop
    checks for it explicitly; anything else the client sends is ignored — this is
    a one-way feed — and simply wakes the loop to send the next frame early.

    Frames are sent every tick without diffing: a position's age advances each
    second, so a "nothing changed" suppression would almost never fire, and the
    frame doubles as the keepalive that holds the socket open through proxies.
    """
    interval = settings.track_ws_interval_s
    while websocket.client_state == WebSocketState.CONNECTED:
        now = datetime.now(UTC)
        refs = await live_track.roster_cache.get(db, route_id)
        frame = await live_track.build_frame(db, r, route_id, now, refs=refs)
        await websocket.send_json(frame.model_dump(mode="json"))

        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=interval)
        except TimeoutError:
            # No client message within the interval — the normal case. Loop and
            # send the next frame.
            continue
        if message["type"] == "websocket.disconnect":
            return
