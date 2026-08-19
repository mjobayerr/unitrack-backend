"""The live fleet as one picture, for the admin console (spec §10.2).

Read from **Redis, not Elasticsearch.** Both know where the buses are, and the
choice is not arbitrary:

- Redis holds one hash per bus, overwritten on every batch, expiring after 60 s.
  That is exactly "where is this bus now", and its *absence* is exactly "this
  bus has gone quiet" — the freshness indicator §10.2 asks for comes free.
- Elasticsearch holds the whole history. Asking it for current positions means a
  collapse-and-sort per query, and getting that subtly wrong is what made
  `/track/nearby` draw buses where they used to be.

So this endpoint is O(live trips) small Redis reads in one pipeline, and the
history store is left for the questions only it can answer.

Freshness is derived here rather than left to the client, because "is this pin
worth believing" is a judgement about the data, and three clients each inventing
their own threshold would disagree on screen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.schemas.admin import GpsFreshness

logger = logging.getLogger("unitrack.fleet_view")

# A fix older than this is no longer "now". Matches spec §10.2's amber
# threshold, and the helper app posts every 5 s — so twelve missed batches.
LIVE_MAX_AGE_S = 60


@dataclass(frozen=True, slots=True)
class Position:
    """A decoded `bus:{id}:pos` hash. Every field optional: the hash is written
    by a phone, and speed and heading are absent on a stationary first fix.

    `ts` is the fix time the *phone* reported; `ingested_at` is when the server
    received it. Freshness is measured against `ingested_at`, because a bus with
    a wrong clock is still live — see `age_seconds`."""

    lat: float
    lng: float
    ts: datetime
    heading: float | None = None
    speed_kmh: float | None = None
    ingested_at: datetime | None = None


def classify(age_s: float | None) -> GpsFreshness:
    """Turn a fix age into the three states the map can colour.

    `None` means no position at all, which is `lost` rather than "very old":
    the difference matters because a bus with no fix cannot be drawn, while a
    stale one can be drawn in amber where it last was.
    """
    if age_s is None:
        return GpsFreshness.lost
    return GpsFreshness.live if age_s <= LIVE_MAX_AGE_S else GpsFreshness.stale


def parse_position(raw: dict[str, str] | None) -> Position | None:
    """Decode a position hash, or None if it is missing or unusable.

    Everything arrived as a string from a phone via Redis, so a malformed value
    is a real possibility. A bad hash is treated as no position — the bus shows
    as `lost`, which is honest — rather than raising and taking the whole fleet
    response down with it.
    """
    if not raw:
        return None
    try:
        ingested_at = raw.get("ingested_at")
        return Position(
            lat=float(raw["lat"]),
            lng=float(raw["lng"]),
            ts=datetime.fromisoformat(raw["ts"]),
            heading=_opt_float(raw.get("heading")),
            # The helper app sends geolocator's metres per second; the console
            # shows km/h. Converted here so no client has to know that.
            speed_kmh=_ms_to_kmh(_opt_float(raw.get("speed"))),
            # Older fixes predate this field; freshness falls back to `ts` then.
            ingested_at=datetime.fromisoformat(ingested_at) if ingested_at else None,
        )
    except (KeyError, ValueError, TypeError):
        logger.warning("unusable position hash, treating bus as lost: %r", raw)
        return None


def parse_seats(raw: dict[str, str] | None) -> tuple[int | None, int | None]:
    """`(occupied, capacity)` from a `bus:{id}:seats` hash."""
    if not raw:
        return None, None
    try:
        return int(raw["occupied"]), int(raw["capacity"])
    except (KeyError, ValueError, TypeError):
        return None, None


def minutes_until(eta: datetime, now: datetime) -> int:
    """Whole minutes from `now` to `eta`, floored at zero.

    Every read path recomputes this rather than serving the `eta_minutes` the ETA
    engine wrote, because the engine runs once a minute and its payload outlives
    that run. Served verbatim, a bus reads "2 min away" for as long as the cache
    holds — including after it has already been and gone. The absolute `eta` is
    the only durable fact in the payload.

    Still rounded server-side, which is the reason `eta_minutes` exists: derive
    it in the browser and two phones with slightly different clocks show
    different numbers for the same bus.
    """
    if eta.tzinfo is None:
        eta = eta.replace(tzinfo=UTC)
    return max(round((eta - now).total_seconds() / 60), 0)


def next_stop_minutes(raw: str | None, now: datetime) -> int | None:
    """Minutes to the next stop, from the ETA engine's cached payload.

    The payload's own `eta_minutes` was computed when the engine last ran, up to
    a minute ago, so it is recomputed from the absolute `eta` instead —
    otherwise a bus would appear to be permanently the same distance away.
    """
    if not raw:
        return None
    try:
        arrivals = json.loads(raw).get("arrivals") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    if not arrivals:
        return None
    try:
        # Arrivals are written in route order, so the first is the next stop.
        eta = datetime.fromisoformat(arrivals[0]["eta"])
    except (KeyError, ValueError, TypeError):
        return None
    return minutes_until(eta, now)


def age_seconds(fix_ts: datetime | None, now: datetime) -> int | None:
    """Age of a fix in whole seconds, floored at zero.

    Clamped because the timestamp comes from the *device* clock: a phone running
    a little fast produces a fix dated in the future, and a negative age would
    render as a nonsense "-3s ago" on the console.
    """
    if fix_ts is None:
        return None
    if fix_ts.tzinfo is None:
        fix_ts = fix_ts.replace(tzinfo=UTC)
    return max(int((now - fix_ts).total_seconds()), 0)


def _opt_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _ms_to_kmh(value: float | None) -> float | None:
    return None if value is None else round(value * 3.6, 1)
