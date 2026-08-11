"""What the admin fleet map is allowed to claim about a bus.

Everything here is decoded from a Redis hash a **phone** wrote, which is the
reason these tests exist: every value arrived as a string, from a device with its
own clock, over a network that drops. The console draws a pin from it, and a pin
that lies is worse than a pin that is missing — an admin acts on it.

The freshness split is the point. §10.2 wants a bus with no recent fix shown
amber rather than treated as normal, because that is a helper-connectivity
problem the console is there to surface, not a bus that vanished.
"""

import json
from datetime import UTC, datetime, timedelta

from app.schemas.admin import GpsFreshness
from app.services.fleet_view import (
    LIVE_MAX_AGE_S,
    age_seconds,
    classify,
    next_stop_minutes,
    parse_position,
    parse_seats,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _pos(**overrides) -> dict[str, str]:
    """A position hash shaped exactly as app/api/routes/helper.py writes it."""
    raw = {
        "lat": "23.7461",
        "lng": "90.3742",
        "speed": "8.5",
        "heading": "270",
        "ts": NOW.isoformat(),
        "trip_id": "t1",
        "ingested_at": NOW.isoformat(),
    }
    raw.update(overrides)
    return raw


# --- freshness -------------------------------------------------------------


def test_a_recent_fix_is_live() -> None:
    assert classify(0) is GpsFreshness.live
    assert classify(LIVE_MAX_AGE_S) is GpsFreshness.live


def test_an_old_fix_is_stale_not_lost() -> None:
    """Stale still has coordinates, so it can be drawn amber where it last was.

    Collapsing stale into lost would throw away the last known position — the
    single most useful thing to show about a bus that has gone quiet.
    """
    assert classify(LIVE_MAX_AGE_S + 1) is GpsFreshness.stale
    assert classify(3600) is GpsFreshness.stale


def test_no_fix_at_all_is_lost() -> None:
    """Distinct from stale: there is nothing to draw, not something old."""
    assert classify(None) is GpsFreshness.lost


def test_the_live_threshold_matches_the_spec() -> None:
    """§10.2 names 60 s, and the Redis position key expires at 60 s too.

    A longer threshold here would mark a bus live whose position Redis has
    already dropped, which cannot happen coherently.
    """
    assert LIVE_MAX_AGE_S == 60


# --- fix age ---------------------------------------------------------------


def test_age_is_whole_seconds_from_the_fix_timestamp() -> None:
    assert age_seconds(NOW - timedelta(seconds=42), NOW) == 42


def test_a_device_clock_running_fast_does_not_produce_a_negative_age() -> None:
    """The timestamp comes from the phone, not the server.

    A device a few seconds ahead dates its fix in the future. Unclamped that
    renders as "-3s ago" on the console, which reads as a bug in the console.
    """
    assert age_seconds(NOW + timedelta(seconds=30), NOW) == 0


def test_a_naive_timestamp_is_treated_as_utc() -> None:
    """Everything is stored UTC, so a missing tzinfo is a serialisation quirk
    rather than a local time — subtracting it must not raise."""
    naive = (NOW - timedelta(seconds=10)).replace(tzinfo=None)

    assert age_seconds(naive, NOW) == 10


# --- position decoding -----------------------------------------------------


def test_a_full_position_decodes() -> None:
    position = parse_position(_pos())

    assert position is not None
    assert position.lat == 23.7461
    assert position.heading == 270


def test_speed_is_converted_from_metres_per_second() -> None:
    """The helper app sends geolocator's `Position.speed`, which is m/s.

    Showing that number as km/h would under-report a bus by 3.6x — a bus doing
    30 km/h would display as 8.
    """
    position = parse_position(_pos(speed="10"))

    assert position is not None
    assert position.speed_kmh == 36.0


def test_a_stationary_first_fix_has_no_speed_or_heading() -> None:
    """Both are written as empty strings when the device has no value yet, and
    `float("")` raises — so this is the ordinary case, not an edge case."""
    position = parse_position(_pos(speed="", heading=""))

    assert position is not None
    assert position.speed_kmh is None
    assert position.heading is None


def test_a_missing_hash_is_no_position() -> None:
    assert parse_position(None) is None
    assert parse_position({}) is None


def test_a_corrupt_hash_reads_as_no_position_rather_than_raising() -> None:
    """One unusable hash must not take the whole fleet response down.

    The bus shows as `lost`, which is an honest description of what is known
    about it, and the other buses still draw.
    """
    assert parse_position({"lat": "not-a-number", "lng": "90.3", "ts": NOW.isoformat()}) is None
    assert parse_position({"lat": "23.7", "lng": "90.3"}) is None  # no ts


# --- seats -----------------------------------------------------------------


def test_seats_decode() -> None:
    assert parse_seats({"occupied": "42", "capacity": "45"}) == (42, 45)


def test_absent_seats_are_unknown_not_zero() -> None:
    """Zero occupancy and "the helper has not counted yet" are different facts,
    and an empty bus is a reasonable thing to report."""
    assert parse_seats(None) == (None, None)
    assert parse_seats({"occupied": "x", "capacity": "45"}) == (None, None)


# --- next stop -------------------------------------------------------------


def test_the_next_stop_eta_is_recomputed_from_the_absolute_time() -> None:
    """The cached `eta_minutes` was computed when the engine last ran.

    Reusing it would leave a bus permanently "5 minutes away" between passes,
    so the minutes are re-derived from the absolute `eta` on every read.
    """
    eta = (NOW + timedelta(minutes=4)).isoformat()
    # eta_minutes is deliberately absurd: if it were being trusted, this passes
    # back 99 instead of 4.
    payload = json.dumps(
        {"arrivals": [{"stop_id": "s1", "seq": 1, "eta": eta, "eta_minutes": 99}]}
    )

    assert next_stop_minutes(payload, NOW) == 4


def test_a_bus_already_past_due_reports_zero_not_a_negative() -> None:
    eta = (NOW - timedelta(minutes=3)).isoformat()
    payload = json.dumps({"arrivals": [{"stop_id": "s1", "seq": 1, "eta": eta}]})

    assert next_stop_minutes(payload, NOW) == 0


def test_no_eta_cached_is_not_an_error() -> None:
    """A trip that just started has no ETAs yet, and the map still has to draw
    it — so every unusable shape is None rather than an exception."""
    assert next_stop_minutes(None, NOW) is None
    assert next_stop_minutes("", NOW) is None
    assert next_stop_minutes('{"arrivals": []}', NOW) is None
    assert next_stop_minutes("not json", NOW) is None
    assert next_stop_minutes('{"arrivals": [{"seq": 1}]}', NOW) is None
