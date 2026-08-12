"""One unusable code must cost one result, not the whole batch.

`POST /helper/redemptions` promises an answer per item, and its docstring says
why: "One forged code in a batch of forty must not cost the other thirty-nine."

`RedemptionIn.code` used to carry `min_length=8, max_length=512`. Pydantic
validates a request body as a whole, so one code outside those bounds made the
response a 422 with no results in it — and the device only drops a queued row
when it receives a per-item answer. So it never dropped the bad row. It re-sent
the same batch every tick, forever, and every genuine boarding queued behind it
never synced. A single corrupt row in the outbox silently ended a helper's day
of takings.

The bounds moved into the handler, per code. These tests pin both halves: the
schema must accept anything, and the handler must have somewhere to reject it.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.api.routes.boarding import CODE_MAX_LEN, CODE_MIN_LEN
from app.schemas.commerce import RedemptionBatchIn, RedemptionIn


def _item(code: str) -> dict:
    return {
        "code": code,
        "device_id": "device-1",
        "redeemed_at": datetime.now(UTC).isoformat(),
    }


def test_a_truncated_code_still_parses() -> None:
    """The realistic corruption: a row cut short in the device's SQLite outbox."""
    assert RedemptionIn.model_validate(_item("abc")).code == "abc"


def test_an_over_long_code_still_parses() -> None:
    """The other realistic case: a helper scanned an unrelated QR.

    A poster or a payment code can be arbitrarily long, and it must not be able
    to take a batch of real boardings down with it.
    """
    assert len(RedemptionIn.model_validate(_item("x" * 4000)).code) == 4000


def test_a_batch_mixing_a_corrupt_code_with_a_valid_one_is_accepted() -> None:
    """The exact shape that used to 422. Verified against the live API, which
    answered 422 and discarded the valid boarding with the bad one."""
    batch = RedemptionBatchIn.model_validate(
        {"redemptions": [_item("bad"), _item("a" * 200)]}
    )
    assert len(batch.redemptions) == 2


def test_an_empty_batch_is_still_refused() -> None:
    """Relaxing the code bound must not relax the batch bound.

    An empty list is a client bug, not a boarding, and there is no per-item
    answer to give for it.
    """
    with pytest.raises(ValidationError):
        RedemptionBatchIn.model_validate({"redemptions": []})


def test_a_batch_beyond_the_cap_is_still_refused() -> None:
    """The 100-item cap is what keeps one sync from monopolising a worker, and it
    is now the only thing bounding the body size, so it has to stay."""
    with pytest.raises(ValidationError):
        RedemptionBatchIn.model_validate({"redemptions": [_item("a" * 200)] * 101})


def test_the_handler_bounds_are_wide_enough_for_a_real_code() -> None:
    """A genuine code is a uuid, two integers, a nonce and an Ed25519 signature —
    around 150 characters. The ceiling has to clear that with room to spare."""
    assert CODE_MIN_LEN < 50 < 200 < CODE_MAX_LEN
