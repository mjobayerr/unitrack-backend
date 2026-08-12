"""Arrival minutes are recounted on read, never served as the engine wrote them.

The ETA engine runs once a minute and caches its payload for longer than that, so
`eta_minutes` inside the cache is stale the moment it is written and stays stale
for as long as the key lives. Served verbatim it produces the worst possible
failure for a live-arrivals screen: a bus that reads "2 min away" indefinitely,
including long after it has been and gone, so a student keeps waiting for a bus
that already left.

The absolute `eta` is the only durable fact in the payload. `minutes_until` is
what every read path derives from it, and it stays server-side so two phones with
slightly different clocks still show the same number for the same bus.
"""

from datetime import UTC, datetime, timedelta

from app.services.fleet_view import minutes_until, next_stop_minutes


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def test_minutes_are_counted_from_now() -> None:
    now = datetime.now(UTC)
    assert minutes_until(now + timedelta(minutes=4), now) == 4


def test_a_bus_already_due_reads_zero_not_negative() -> None:
    """"-3 min" is not a thing a student can act on, and it is what a raw
    subtraction produces once the estimate has passed."""
    now = datetime.now(UTC)
    assert minutes_until(now - timedelta(minutes=3), now) == 0


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Everything is stored in UTC, but a payload that lost its offset in transit
    must not be treated as local time — that is a six-hour error in Dhaka."""
    now = datetime.now(UTC)
    naive = (now + timedelta(minutes=5)).replace(tzinfo=None)
    assert minutes_until(naive, now) == 5


def test_rounding_is_to_the_nearest_minute() -> None:
    now = datetime.now(UTC)
    assert minutes_until(now + timedelta(seconds=100), now) == 2
    assert minutes_until(now + timedelta(seconds=80), now) == 1


def test_the_stale_minutes_in_the_payload_are_ignored() -> None:
    """A payload whose own `eta_minutes` disagrees with its `eta`.

    This is what a cached payload looks like a minute after the engine ran, and
    the whole point is that the absolute time wins.
    """
    eta = _iso(timedelta(minutes=2))
    raw = (
        f'{{"arrivals": [{{"stop_id": "s", "seq": 1, "eta": "{eta}",'
        ' "eta_minutes": 9, "basis": "live", "distance_km": 1.0}]}'
    )
    assert next_stop_minutes(raw, datetime.now(UTC)) == 2


def test_no_cached_payload_means_no_estimate() -> None:
    assert next_stop_minutes(None, datetime.now(UTC)) is None
    assert next_stop_minutes("", datetime.now(UTC)) is None


def test_a_corrupt_payload_costs_the_estimate_and_nothing_else() -> None:
    """One bad Redis value must not take the whole fleet response down with it."""
    assert next_stop_minutes("{not json", datetime.now(UTC)) is None
    assert next_stop_minutes('{"arrivals": [{"seq": 1}]}', datetime.now(UTC)) is None
    assert next_stop_minutes('{"arrivals": []}', datetime.now(UTC)) is None
