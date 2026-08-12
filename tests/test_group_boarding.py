"""How many people one code may board.

`passenger_count` is inside the signed QR payload, and the key that signs it is
the **student's own** — `GET /shop/tickets/{id}/qr-material` hands it to their
device so codes work with no signal. The signature therefore proves who produced
the number; it says nothing about whether the number is honest.

Nothing bounded it. A 10-ride ticket signed with `passenger_count=40` boarded
forty people, the deduction clamped at zero, and the boarding was filed `ok` —
thirty fares gone, with no flag for anyone to look at. Confirmed against a
running stack before the fix.

Two bounds now, at the two layers that can each know something the other cannot:

- `MAX_PASSENGERS` in `parse_qr`, from the code alone, so it holds identically on
  a helper's phone with no signal.
- the ticket's remaining rides in `_check_rides_cover_group`, which needs the
  ticket and so belongs on the server (and in the helper's synced manifest).
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.commerce import TicketStatus
from app.services.boarding import (
    MAX_PASSENGERS,
    QrPayload,
    build_qr,
    current_slice,
    generate_keypair,
    new_nonce,
    parse_qr,
    verify_qr,
)
from app.services.redemption import RedemptionRejected, _check_rides_cover_group


class _Ticket:
    """Only the field the group check reads."""

    def __init__(self, rides_remaining: int | None):
        now = datetime.now(UTC)
        self.rides_remaining = rides_remaining
        self.status = TicketStatus.active
        self.valid_from = now - timedelta(days=1)
        self.valid_to = now + timedelta(days=7)


# --- the ride bound -------------------------------------------------------


def test_a_group_within_the_remaining_rides_is_allowed() -> None:
    _check_rides_cover_group(_Ticket(rides_remaining=10), 3)


def test_a_group_of_exactly_the_remaining_rides_is_allowed() -> None:
    """The boundary is inclusive: four friends may spend the last four rides."""
    _check_rides_cover_group(_Ticket(rides_remaining=4), 4)


def test_a_group_larger_than_the_remaining_rides_is_refused() -> None:
    """The fault this file exists for.

    Before the fix this was accepted and the ticket was drained to zero, so the
    system recorded ten rides against forty passengers and never flagged it.
    """
    with pytest.raises(RedemptionRejected, match="40 passengers but 10 ride"):
        _check_rides_cover_group(_Ticket(rides_remaining=10), 40)


def test_an_unlimited_pass_is_not_measured_against_rides() -> None:
    """`None` rides is a monthly pass, not zero rides.

    There is no count to compare a group against, so this check has nothing to
    say — `MAX_PASSENGERS` is what bounds an unlimited pass.
    """
    _check_rides_cover_group(_Ticket(rides_remaining=None), MAX_PASSENGERS)


# --- the absolute bound, checkable offline --------------------------------


def _code(count: int) -> tuple[str, str]:
    private, public = generate_keypair()
    code = build_qr(
        private,
        QrPayload(
            ticket_id="11111111-1111-1111-1111-111111111111",
            passenger_count=count,
            time_slice=current_slice(datetime.now(UTC).timestamp()),
            nonce=new_nonce(),
        ),
    )
    return code, public


def test_a_plausible_group_size_parses() -> None:
    code, public = _code(4)
    assert verify_qr(code, public, datetime.now(UTC).timestamp()).passenger_count == 4


def test_the_ceiling_itself_is_allowed() -> None:
    code, public = _code(MAX_PASSENGERS)
    assert verify_qr(code, public, datetime.now(UTC).timestamp()).passenger_count == (
        MAX_PASSENGERS
    )


def test_a_count_above_the_ceiling_is_refused_before_any_database_work() -> None:
    """Rejected in `parse_qr`, which is what makes it hold offline too.

    A signature check would pass — the student signed it with their own key — so
    if this were left to the server the passengers would already have ridden by
    the time the boarding was refused at sync.
    """
    code, _ = _code(MAX_PASSENGERS + 1)
    with pytest.raises(Exception, match=f"above the {MAX_PASSENGERS} limit"):
        parse_qr(code)


def test_a_signed_but_absurd_count_is_still_refused() -> None:
    """The signature is valid. That is the point: it is not the same as honest."""
    code, public = _code(500)
    with pytest.raises(Exception, match="limit"):
        verify_qr(code, public, datetime.now(UTC).timestamp())


def test_the_ceiling_exceeds_any_bus_in_the_fleet() -> None:
    """Set above real capacity on purpose.

    Refusing a genuine group is worse than accepting one, because the ride bound
    above already stops it being free. This constant is only here to kill the
    absurd cases cheaply.
    """
    assert MAX_PASSENGERS > 45
