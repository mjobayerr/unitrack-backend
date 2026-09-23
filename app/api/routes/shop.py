"""Buying a ticket: catalogue, orders, payment settlement, wallet.

The payment flow, and why each step is where it is:

    POST /shop/orders          create the order, open an SSLCommerz session
    (student pays on the gateway's own page)
    POST /shop/payments/return the gateway sends the browser back here
    GET  /shop/tickets         the ticket is now in the wallet

**The return endpoint is unauthenticated, and that is not a hole.** SSLCommerz
redirects the student's browser with a form POST, which carries no Authorization
header and no cookie we control. Security does not come from authenticating that
request — anyone can forge it by typing a URL. It comes from what happens next:
the `val_id` in the request is checked server-to-server against SSLCommerz, and
the settled amount and currency are compared with the order's own. A forged
return fails validation and issues nothing.

That is also why the order, not the request, decides who gets the ticket: the
student is read from the order row we created earlier, never from the callback.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_student, require_authenticated
from app.core.config import API_V1_PREFIX, settings
from app.core.sslcommerz import GatewayError, SslCommerzClient
from app.db.session import get_db
from app.models.commerce import Order, OrderStatus, Ticket, TicketProduct
from app.models.user import Student
from app.repositories import commerce as commerce_repo
from app.repositories import users as user_repo
from app.schemas.commerce import CheckoutOut, OrderCreate, OrderOut, ProductOut, TicketOut
from app.services.settlement import AMOUNT_MISMATCH, PAID, apply_validation

logger = logging.getLogger("unitrack.shop")

router = APIRouter(prefix="/shop", tags=["shop"])

# One shared client, handed to handlers through a dependency rather than reached
# as a module global. The default is this instance; a test overrides `get_gateway`
# to stand in a fake, and nothing has to monkeypatch a module attribute to do it.
_gateway = SslCommerzClient()


def get_gateway() -> SslCommerzClient:
    return _gateway


def _return_urls() -> dict[str, str]:
    base = settings.public_base_url.rstrip("/")
    # One endpoint for all three outcomes: the gateway tells us which happened,
    # and three near-identical handlers would drift apart. Built with the version
    # prefix — the gateway must be handed the real served path, or the browser
    # comes back to a 404 and the ticket is never issued.
    return {
        "success_url": f"{base}{API_V1_PREFIX}/shop/payments/return",
        "fail_url": f"{base}{API_V1_PREFIX}/shop/payments/return",
        "cancel_url": f"{base}{API_V1_PREFIX}/shop/payments/return",
    }


def _ipn_url() -> str | None:
    """Where SSLCommerz should report the outcome independently of the browser.

    Only sent when the configured origin is publicly resolvable. Registering a
    localhost IPN would have the gateway retry a URL it can never reach, and
    every order would look unsettled to whoever reads those logs.
    """
    base = settings.public_base_url.rstrip("/")
    if "localhost" in base or "127.0.0.1" in base:
        return None
    return f"{base}{API_V1_PREFIX}/shop/payments/ipn"


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@router.get(
    "/products",
    response_model=list[ProductOut],
    dependencies=[Depends(require_authenticated)],
)
async def list_products(db: AsyncSession = Depends(get_db)) -> list[TicketProduct]:
    """What is for sale. Readable by any signed-in account so the helper app can
    show a student what they should have bought."""
    return await commerce_repo.list_products(db, active_only=True)


# ---------------------------------------------------------------------------
# Purchase
# ---------------------------------------------------------------------------


@router.post("/orders", response_model=CheckoutOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    student: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
    gateway: SslCommerzClient = Depends(get_gateway),
) -> CheckoutOut:
    """Start a purchase and return the gateway URL to send the student to.

    Idempotent on `idempotency_key`: a retried or double-tapped request returns
    the original order rather than charging twice. The uniqueness is enforced by
    the database, because a check-then-insert loses the race that makes this
    necessary in the first place.
    """
    existing = await commerce_repo.order_by_idempotency_key(db, payload.idempotency_key)
    if existing is not None:
        if existing.student_id != student.id:
            # Someone else's key. Refuse rather than reveal that it exists.
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency key already used")
        if existing.status is OrderStatus.paid:
            raise HTTPException(status.HTTP_409_CONFLICT, "Order already paid")
        return await _open_checkout(db, gateway, existing, student)

    product = await commerce_repo.get_product(db, payload.product_id)
    if product is None or not product.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not available")

    order = Order(
        student_id=student.id,
        product_id=product.id,
        # Copied, not referenced: a price change tomorrow must not rewrite what
        # this student agreed to pay today.
        amount_paisa=product.price_paisa,
        currency="BDT",
        status=OrderStatus.initiated,
        idempotency_key=payload.idempotency_key,
        tran_id=f"UT-{uuid.uuid4().hex[:20]}",
    )
    db.add(order)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race against a concurrent identical request; that request's
        # order is the real one.
        await db.rollback()
        winner = await commerce_repo.order_by_idempotency_key(db, payload.idempotency_key)
        return await _open_checkout(db, gateway, winner, student)

    await db.refresh(order)
    return await _open_checkout(db, gateway, order, student)


async def _open_checkout(
    db: AsyncSession, gateway: SslCommerzClient, order: Order, student: Student
) -> CheckoutOut:
    product = await commerce_repo.get_product(db, order.product_id)
    user = await user_repo.get(db, student.user_id)
    if product is None or user is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Order references missing records")

    try:
        checkout_url = await gateway.create_session(
            tran_id=order.tran_id,
            amount_paisa=order.amount_paisa,
            currency=order.currency,
            **_return_urls(),
            ipn_url=_ipn_url(),
            customer_name=user.name,
            customer_email=user.email,
            customer_phone=user.phone,
            product_name=product.name,
        )
    except GatewayError as exc:
        logger.error("checkout failed for order %s: %s", order.id, exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Payment gateway unavailable, try again"
        ) from exc

    order.status = OrderStatus.pending
    await db.commit()

    return CheckoutOut(
        order_id=order.id,
        tran_id=order.tran_id,
        amount_paisa=order.amount_paisa,
        currency=order.currency,
        status=order.status,
        checkout_url=checkout_url,
    )


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


class SettlementError(Exception):
    """Settlement could not be decided. Carries the HTTP shape to answer with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _settle(db: AsyncSession, gateway: SslCommerzClient, fields: dict) -> tuple[Order, str]:
    """Decide what happened to one payment, and issue a ticket if it succeeded.

    Shared by the browser return and the IPN, because those are two reports of
    the same event and must not reach different conclusions. Whichever arrives
    first settles the order; the other finds it already `paid` and stops.

    Nothing in `fields` is trusted beyond `tran_id`, which is only a lookup key.
    The outcome comes from validating `val_id` directly with SSLCommerz.
    """
    tran_id = str(fields.get("tran_id") or "")
    if not tran_id:
        raise SettlementError(status.HTTP_400_BAD_REQUEST, "Missing tran_id")

    order = await commerce_repo.order_by_tran_id(db, tran_id)
    if order is None:
        raise SettlementError(status.HTTP_404_NOT_FOUND, "Unknown transaction")

    # Already settled. A refreshed success page and a duplicate IPN both land
    # here, and neither may issue a second ticket.
    if order.status is OrderStatus.paid:
        return order, "paid"

    gateway_status = str(fields.get("status") or "").upper()
    if gateway_status in {"FAILED", "CANCELLED"}:
        order.status = (
            OrderStatus.cancelled if gateway_status == "CANCELLED" else OrderStatus.failed
        )
        await db.commit()
        return order, order.status.value

    val_id = str(fields.get("val_id") or "")
    if not val_id:
        raise SettlementError(status.HTTP_400_BAD_REQUEST, "Missing val_id")

    try:
        validation = await gateway.validate(val_id)
    except GatewayError as exc:
        # Leave the order pending rather than failing it: the money may well
        # have moved, and the reconciler needs to see it as unsettled.
        logger.error("validation unreachable for order %s: %s", order.id, exc)
        raise SettlementError(
            status.HTTP_502_BAD_GATEWAY, "Could not confirm payment, check your wallet later"
        ) from exc

    # The decision itself lives in app/services/settlement.py, shared with the
    # reconciler: three callers learn about a payment by different routes and
    # must not reach different conclusions about the same money.
    #
    # `setdefault` because a response that omits `val_id` would otherwise leave
    # `orders.gateway_val_id` null — and that column is the handle anyone
    # arguing with the gateway about this payment has to work from.
    validation.setdefault("val_id", val_id)
    outcome = await apply_validation(db, order, validation)

    if outcome == AMOUNT_MISMATCH:
        await db.commit()
        raise SettlementError(status.HTTP_400_BAD_REQUEST, "Payment amount mismatch")

    try:
        await db.commit()
    except IntegrityError:
        # The unique on tickets.order_id fired: the IPN and the browser return
        # raced and the other one already issued. That is success, not failure.
        await db.rollback()
        logger.info("ticket for order %s was already issued concurrently", order.id)
        return order, PAID

    return order, outcome


async def _handle_return(db: AsyncSession, gateway: SslCommerzClient, fields: dict):
    """Where the gateway sends the student's browser, whatever the outcome.

    Unauthenticated by necessity and safe by construction — see the module
    docstring. This is the fast path; the IPN below is the reliable one.
    """
    try:
        order, outcome = await _settle(db, gateway, fields)
    except SettlementError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return _finish(order, outcome)


# Split by method rather than one `api_route` carrying both. FastAPI derives an
# operation id per method from the function name, so a single handler for two
# methods emits duplicates — which makes the generated TypeScript client in
# unitrack-web collide on one of them.
@router.post("/payments/return", operation_id="payment_return_post")
async def payment_return_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    gateway: SslCommerzClient = Depends(get_gateway),
):
    """The normal case: SSLCommerz returns the student with a form POST."""
    return await _handle_return(db, gateway, dict(await request.form()))


@router.get("/payments/return", operation_id="payment_return_get")
async def payment_return_get(
    request: Request,
    db: AsyncSession = Depends(get_db),
    gateway: SslCommerzClient = Depends(get_gateway),
):
    """Some gateway configurations redirect with a GET and query parameters."""
    return await _handle_return(db, gateway, dict(request.query_params))


@router.post("/payments/ipn")
async def payment_ipn(
    request: Request,
    db: AsyncSession = Depends(get_db),
    gateway: SslCommerzClient = Depends(get_gateway),
):
    """SSLCommerz reporting the outcome server to server.

    This exists because the browser return cannot be relied on: a student who
    closes the tab, loses signal, or is redirected through a flaky network
    never reaches it, and their money is gone with no ticket to show for it.
    The IPN arrives regardless, which makes it the authoritative path and the
    return merely the fast one.

    Answers 200 for outcomes SSLCommerz should stop retrying — including
    failures, which are settled facts. A 5xx is reserved for "ask again later",
    because that is what a retry can actually fix.
    """
    fields = dict(await request.form())
    try:
        _order, outcome = await _settle(db, gateway, fields)
    except SettlementError as exc:
        if exc.status_code >= 500:
            raise HTTPException(exc.status_code, exc.detail) from exc
        logger.warning("ipn rejected: %s", exc.detail)
        return {"received": True, "outcome": "rejected"}
    return {"received": True, "outcome": outcome}


def _finish(order: Order, outcome: str):
    """Send the browser onward, or answer plainly if there is nowhere to go."""
    if settings.checkout_return_url:
        target = (
            f"{settings.checkout_return_url.rstrip('/')}"
            f"?order={order.id}&status={outcome}"
        )
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    return {"order_id": str(order.id), "status": outcome}


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    student: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
) -> list[Order]:
    return await commerce_repo.orders_for_student(db, student.id)


@router.get("/tickets", response_model=list[TicketOut])
async def list_tickets(
    student: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
) -> list[Ticket]:
    """The student's wallet. Scoped to the caller — never takes an id from the
    request, so one student cannot read another's tickets."""
    return await commerce_repo.tickets_for_student(db, student.id)
