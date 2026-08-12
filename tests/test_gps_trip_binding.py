"""Which fixes `POST /helper/gps` will attribute, and which it refuses.

Ingest used to accept a batch with no live trip at all, storing it with an empty
`trip_id`. That was a documented transition allowance for a helper build with no
trip UI. The UI shipped; the allowance stayed, and it meant **any approved helper
could put any bus anywhere** — on the console's live map and in the fix history —
for a bus they had never been near, by naming its id in the request body. On a
product whose entire value is knowing where the buses are, that is the hole that
matters most.

Requiring a live trip alone would have broken the honest case it was hiding: the
app stops the sensor *before* ending a trip, so fixes already queued on the device
upload after the trip is closed. Those belong to a real journey, and the client
leaves a rejected batch queued — so refusing them would both lose the tail of the
route and jam every later fix behind it.

So a batch with no live trip is matched against a trip this helper ended **on that
same bus** within `GPS_DRAIN_GRACE`. This file pins the query that decides it.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql

from app.models.fleet import Trip, TripStatus
from app.services.trip import GPS_DRAIN_GRACE, recently_ended_trip


class _Recorder:
    """Captures the statement instead of running it, so the filter can be read."""

    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return self

    def scalars(self):
        return self

    def first(self):
        return None


async def _compiled(**kwargs) -> str:
    db = _Recorder()
    await recently_ended_trip(db, **kwargs)
    return str(
        db.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


async def test_the_lookup_is_scoped_to_the_helper_and_the_bus() -> None:
    """Both halves are the security property.

    Drop the helper and any helper may report for a bus someone else drove; drop
    the bus and a helper may report for the whole fleet on the strength of one
    trip of their own. Either one restores the hole this closed.
    """
    helper = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    bus = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    sql = await _compiled(
        helper_id=helper, bus_id=bus, now=datetime.now(UTC)
    )
    assert f"trips.helper_id = '{helper}'" in sql
    assert f"trips.bus_id = '{bus}'" in sql


async def test_only_completed_trips_are_considered() -> None:
    """A live trip is handled on the other branch, and a cancelled one never ran."""
    sql = await _compiled(
        helper_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        bus_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        now=datetime.now(UTC),
    )
    assert f"trips.status = '{TripStatus.completed.value}'" in sql


async def test_the_window_is_bounded() -> None:
    """Without the lower bound this would accept fixes against any trip the helper
    ever drove on that bus, which turns the grace window into a licence to
    rewrite last month's history."""
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    sql = await _compiled(
        helper_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        bus_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        now=now,
    )
    # Rendered with a space rather than the ISO "T" by the postgresql dialect.
    cutoff = (now - GPS_DRAIN_GRACE).isoformat(sep=" ")
    assert f"trips.actual_end >= '{cutoff}'" in sql
    assert "trips.actual_end IS NOT NULL" in sql


async def test_the_newest_trip_wins() -> None:
    """Two trips on the same bus in one shift is normal — out and back. The fixes
    still draining belong to the one that just ended."""
    sql = await _compiled(
        helper_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        bus_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        now=datetime.now(UTC),
    )
    assert "ORDER BY trips.actual_end DESC" in sql
    assert "LIMIT 1" in sql


def test_the_grace_window_covers_a_shift_not_a_coffee_break() -> None:
    """A phone with no signal for most of a route uploads at the depot, hours
    later. Minutes here would throw those fixes away — and because a 409 has to be
    final for the client to drop the batch, it would throw away everything queued
    behind them too.
    """
    assert GPS_DRAIN_GRACE >= timedelta(hours=8)


def test_the_trip_model_can_express_an_open_end() -> None:
    """`actual_end` is nullable, so the query has to exclude nulls explicitly
    rather than rely on the comparison — in SQL a null bound is not false, it is
    unknown, and the row would be dropped for the wrong reason."""
    assert Trip.__table__.c.actual_end.nullable
