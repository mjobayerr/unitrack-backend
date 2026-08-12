"""Turning a scanned code into a recorded ride.

`app/services/boarding.py` answers "is this code genuine and fresh?" using
nothing but the code, a public key and a clock — deliberately, so the same
logic can run on a helper's phone with no signal.

This module answers the questions that need the database: has this exact code
already been used, is the ticket still good, and how many rides are left. It is
the server's half of spec §7.2, and it is also where offline boarding's
inevitable ambiguity gets resolved.

**Why a duplicate is recorded rather than refused.** Two helper phones, both
offline, cannot see each other's nonce logs, so the same code can legitimately
be accepted twice before either syncs. Rejecting the second sync would delete
the only evidence that it happened. So the boarding is written with
`duplicate_suspect` and the ride is *not* deducted twice — the passenger is not
charged for the fraud, and the admin gets something to look at.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commerce import Redemption, RedemptionFlag, Ticket, TicketStatus
from app.services.boarding import InvalidQr, verify_qr

logger = logging.getLogger("unitrack.redemption")


class RedemptionRejected(Exception):
    """The boarding cannot be recorded at all, with a reason for the helper."""


async def _ticket_for(db: AsyncSession, ticket_id: str) -> Ticket:
    try:
        ticket = await db.get(Ticket, ticket_id)
    except Exception as exc:  # noqa: BLE001 - a malformed uuid is just a bad code
        raise RedemptionRejected("unknown ticket") from exc
    if ticket is None:
        raise RedemptionRejected("unknown ticket")
    return ticket


def _check_valid_window(ticket: Ticket, now: datetime) -> None:
    """Reasons a ticket is not a ticket at all, regardless of rides.

    Separate from the ride count on purpose: these are absolute, while running
    out of rides is only decisive for a *first* sighting. See `redeem`.
    """
    if ticket.status is TicketStatus.revoked:
        raise RedemptionRejected("ticket revoked")
    # Suspension has to bite here, not only in the manifest. `GET
    # /helper/manifest` already omits everything that is not `active`, so an
    # online helper never receives a suspended ticket's public key — but a helper
    # holding a manifest downloaded *before* the sweep ran still validates the
    # code offline and syncs the boarding afterwards. Without this check that
    # sync deducts a ride and is filed as a clean boarding, which defeats the
    # suspension for the one attacker it was raised against.
    if ticket.status is TicketStatus.suspended:
        raise RedemptionRejected("ticket suspended pending review")
    if ticket.status is TicketStatus.expired or ticket.valid_to < now:
        raise RedemptionRejected("ticket expired")
    if ticket.valid_from > now:
        raise RedemptionRejected("ticket not yet valid")


def _check_rides_cover_group(ticket: Ticket, passenger_count: int) -> None:
    """The group must fit inside what is left on the ticket.

    This is the check that makes a ticket cost what it claims to, and it was
    missing.

    `passenger_count` lives inside the signed payload, and the signing key
    belongs to the **student** — `GET /shop/tickets/{id}/qr-material` hands it to
    their own device so codes can be produced with no signal. So the signature
    proves the count came from the ticket holder and nothing more; anyone who
    reads their own key out of the app can sign any number they like.

    Without this bound, a 10-ride ticket signed with `passenger_count=40` boarded
    forty people. The deduction in `redeem` clamps at zero, so the ticket
    recorded ten rides and thirty fares were simply never paid. Confirmed against
    the running stack before the fix, and it came back `accepted=True, flag=ok`
    — not even raised for review.

    Only checked for a **first sighting**, like the empty-ticket rule above and
    for the same reason: a duplicate must still be recorded so the fraud sweep
    has the evidence.

    `rides_remaining` is None for an unlimited pass, so this is not a truthiness
    check — `0` and `None` mean opposite things. An unlimited pass is bounded by
    `MAX_PASSENGERS` in `verify_qr` instead, since there is no ride count to
    measure a group against.
    """
    if ticket.rides_remaining is None:
        return
    if passenger_count > ticket.rides_remaining:
        raise RedemptionRejected(
            f"{passenger_count} passengers but "
            f"{ticket.rides_remaining} ride(s) left on this ticket"
        )


async def redeem(
    db: AsyncSession,
    *,
    code: str,
    helper_id,
    device_id: str,
    redeemed_at: datetime,
    trip_id=None,
    now: datetime | None = None,
) -> Redemption:
    """Validate a scanned code and record the boarding. Does not commit.

    Raises `RedemptionRejected` for anything the helper should be shown a red
    screen for. Returns the stored row otherwise — flagged `duplicate_suspect`
    if another device already reported this exact code.
    """
    now = now or datetime.now(UTC)

    # Parse before touching the database: a forged code should cost one
    # signature check, not a query.
    try:
        head, *_ = code.split(".")
    except AttributeError as exc:
        raise RedemptionRejected("malformed code") from exc

    ticket = await _ticket_for(db, head)

    try:
        payload = verify_qr(code, ticket.qr_public_key, now.timestamp())
    except InvalidQr as exc:
        raise RedemptionRejected(str(exc)) from exc

    _check_valid_window(ticket, now)

    # Has any device already reported this exact code?
    seen = (
        await db.execute(select(Redemption).where(Redemption.nonce == payload.nonce))
    ).scalars().first()

    if seen is not None and seen.helper_device_id == device_id:
        # This device's own re-sync. Idempotent: the ride was already counted,
        # so returning the existing row keeps a retried batch from double-
        # charging a passenger for one boarding.
        return seen

    flag = RedemptionFlag.ok if seen is None else RedemptionFlag.duplicate_suspect

    if flag is RedemptionFlag.ok:
        # Only a first sighting can be refused for an empty ticket. A duplicate
        # must still be written even when the ticket is exhausted — that is
        # precisely the case spec §7.5 describes, where two offline devices push
        # a ticket past zero, and rejecting it here would throw away the only
        # evidence the fraud sweep has to work with.
        #
        # `rides_remaining` is None for an unlimited pass, so this is not a
        # truthiness check: `0` and `None` mean opposite things.
        if ticket.rides_remaining is not None and ticket.rides_remaining <= 0:
            raise RedemptionRejected("no rides remaining")
        _check_rides_cover_group(ticket, payload.passenger_count)
    else:
        logger.warning(
            "nonce %s already redeemed on device %s, now presented by %s — flagging",
            payload.nonce,
            seen.helper_device_id,
            device_id,
        )

    redemption = Redemption(
        ticket_id=ticket.id,
        trip_id=trip_id,
        helper_id=helper_id,
        helper_device_id=device_id,
        nonce=payload.nonce,
        passenger_count=payload.passenger_count,
        redeemed_at=redeemed_at,
        synced_at=now,
        flag=flag,
    )
    db.add(redemption)

    # Only a first sighting deducts. A duplicate is someone else's problem to
    # investigate, not the passenger's to pay for twice.
    if flag is RedemptionFlag.ok and ticket.rides_remaining is not None:
        ticket.rides_remaining = max(ticket.rides_remaining - payload.passenger_count, 0)
        if ticket.rides_remaining == 0:
            ticket.status = TicketStatus.exhausted

    return redemption
