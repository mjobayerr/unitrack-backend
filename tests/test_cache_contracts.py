"""The two objects that survive a round trip through Redis.

Both are cached as JSON strings, which means a field added to the dataclass but
forgotten in `from_json` fails silently — the value simply comes back as the
default and authorization or trip binding quietly uses stale data. These tests
pin the round trip so that mistake is a red build.
"""

import datetime
import uuid
from zoneinfo import ZoneInfo

from app.core.authz import Principal
from app.models.user import HelperStatus, UserRole, UserStatus
from app.services.trip import ActiveTrip, service_date_now


def test_principal_round_trips_through_json() -> None:
    original = Principal(
        user_id=uuid.uuid4(),
        role=UserRole.helper,
        status=UserStatus.active,
        helper_id=uuid.uuid4(),
        helper_status=HelperStatus.approved,
    )
    assert Principal.from_json(original.to_json()) == original


def test_principal_round_trips_without_a_helper_row() -> None:
    """Students and admins have NULLs in the helper half of the join."""
    original = Principal(
        user_id=uuid.uuid4(),
        role=UserRole.student,
        status=UserStatus.active,
    )
    restored = Principal.from_json(original.to_json())
    assert restored == original
    assert restored.helper_id is None
    assert not restored.is_approved_helper


def test_only_an_approved_helper_counts_as_one() -> None:
    pending = Principal(
        user_id=uuid.uuid4(),
        role=UserRole.helper,
        status=UserStatus.active,
        helper_id=uuid.uuid4(),
        helper_status=HelperStatus.pending,
    )
    assert not pending.is_approved_helper

    # An admin is not a helper, however approved anything else looks.
    admin = Principal(
        user_id=uuid.uuid4(), role=UserRole.admin, status=UserStatus.active
    )
    assert not admin.is_approved_helper


def test_active_trip_round_trips_through_json() -> None:
    original = ActiveTrip(trip_id=uuid.uuid4(), bus_id=uuid.uuid4(), route_id=uuid.uuid4())
    assert ActiveTrip.from_json(original.to_json()) == original


def test_service_date_is_local_not_utc() -> None:
    """A trip belongs to the local service day.

    Dhaka is UTC+6, so between 18:00 and 24:00 UTC the local date is already
    tomorrow. Deriving the service date from UTC would file an evening trip
    under the previous day and split the evening's ridership across two dates.
    """
    local_today = datetime.datetime.now(ZoneInfo("Asia/Dhaka")).date()
    assert service_date_now() == local_today
