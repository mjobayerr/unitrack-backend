"""What "which buses are near me" actually asks Elasticsearch.

Two bugs lived in this query, and both were invisible from the outside: the
endpoint returned buses and a plausible distance for each, so nothing looked
wrong until you noticed the bus was not there.

1. No freshness filter, so the whole fix history answered — a bus that stopped
   reporting last week still appeared on a live map.
2. `collapse` kept one fix per bus and the **first sort key** chose which one.
   Sorting by distance first picked the closest point that bus had ever
   recorded, not its current one.

Together those meant a bus 4 km away that drove past this spot an hour earlier
was drawn 50 m from the student. That is worse than a stale pin — it sends
someone to a stop for a bus that is not coming.

These assertions are on the query body rather than on live results, because the
bug was in the query's *semantics*: a request against real Elasticsearch happily
returned a wrong answer that looked right.
"""

from datetime import UTC, datetime, timedelta

from app.api.routes.tracking import NEARBY_FRESH_S, build_nearby_query

_ORIGIN = {"lat": 23.7461, "lon": 90.3742}


def _query(radius_km: float = 5, age_s: int = NEARBY_FRESH_S) -> dict:
    cutoff = datetime.now(UTC) - timedelta(seconds=age_s)
    return build_nearby_query(_ORIGIN, radius_km, cutoff)


def test_the_latest_fix_decides_which_position_is_reported() -> None:
    """`ts` descending must be the FIRST sort key.

    This is the whole fix. `collapse` returns the top document per bus according
    to the sort, so whichever key comes first decides which fix represents the
    bus. Distance first means "closest ever"; `ts` first means "where it is now".
    """
    sort = _query()["sort"]

    assert "ts" in sort[0], "ts must be the first sort key, or collapse picks the wrong fix"
    assert sort[0]["ts"]["order"] == "desc", "newest first, not oldest"


def test_distance_is_still_computed_as_a_secondary_sort() -> None:
    """The geo sort has to stay, and stay second.

    It is no longer choosing the representative fix; it is there so
    Elasticsearch returns the distance in `sort[1]`, which the handler reads
    rather than recomputing a haversine in Python.
    """
    sort = _query()["sort"]

    assert len(sort) == 2
    assert "_geo_distance" in sort[1]
    assert sort[1]["_geo_distance"]["unit"] == "km"


def test_stale_fixes_are_excluded() -> None:
    """A `range` on `ts` must be present, or the query searches all history."""
    filters = _query()["query"]["bool"]["filter"]
    ranges = [f for f in filters if "range" in f]

    assert ranges, "without a ts range a bus that stopped reporting never disappears"
    assert "gte" in ranges[0]["range"]["ts"]


def test_the_radius_still_applies() -> None:
    filters = _query(radius_km=3)["query"]["bool"]["filter"]
    geo = [f for f in filters if "geo_distance" in f]

    assert geo, "the radius filter must survive the rewrite"
    assert geo[0]["geo_distance"]["distance"] == "3km"


def test_both_conditions_are_filters_not_scoring_clauses() -> None:
    """Neither clause should contribute to relevance.

    Ordering comes entirely from the explicit sort, so scoring would be wasted
    work — and `filter` results are cacheable by Elasticsearch.
    """
    bool_query = _query()["query"]["bool"]

    assert "must" not in bool_query
    assert len(bool_query["filter"]) == 2


def test_one_fix_per_bus() -> None:
    assert _query()["collapse"]["field"] == "bus_id"


def test_the_freshness_window_outlasts_a_dropped_connection() -> None:
    """The helper app batches every 5 s, so this is ~24 missed batches.

    Too short and a bus blinks off the map in a tunnel; too long and one that
    has genuinely stopped lingers. Two minutes is the compromise, and anything
    under 30 s would be shorter than a realistic mobile dead spot.
    """
    assert NEARBY_FRESH_S >= 60
    assert NEARBY_FRESH_S <= 600
