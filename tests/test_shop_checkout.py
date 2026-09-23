"""HTTP-level coverage for the purchase flow — order, checkout, settlement.

The payment path is the highest-stakes business logic in the API and, until now,
lived entirely in the route handler with only the integration smoke test around
it. These exercise it with SQLite and a mocked gateway (patched at the client's
own methods, so they hold whether the gateway is a module global or an injected
dependency), pinning the observable outcome — order status and whether a ticket
is issued — across a refactor that moves the orchestration into a service.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_current_student, get_db
from app.core.sslcommerz import SslCommerzClient
from app.db.base import Base
from app.main import create_app
from app.models.commerce import Order, OrderStatus, ProductType, Ticket, TicketProduct
from app.models.user import Student, User, UserRole, UserStatus


async def _fake_create_session(self, **kwargs) -> str:
    return "https://sandbox.example/pay/abc"


def _valid_response(**over) -> dict:
    """A validation response for a settled 100.00 BDT payment."""
    base = {
        "status": "VALID",
        "currency_amount": "100.00",
        "currency_type": "BDT",
        "risk_level": "0",
        "val_id": "VAL-1",
        "bank_tran_id": "BANK-1",
        "card_type": "VISA",
    }
    base.update(over)
    return base


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def student_and_product(db) -> tuple[Student, TicketProduct]:
    user = User(
        email="s@ulab.edu.bd",
        password_hash="x",
        role=UserRole.student,
        name="Student One",
        status=UserStatus.active,
    )
    db.add(user)
    await db.flush()
    student = Student(user_id=user.id, student_id_no="0111", department="CSE", batch="2021")
    product = TicketProduct(
        type=ProductType.single,
        name="Single Ride",
        price_paisa=10000,
        ride_count=10,
        validity_days=30,
        active=True,
    )
    db.add_all([student, product])
    await db.commit()
    return student, product


@pytest.fixture
def client(db, student_and_product) -> AsyncClient:
    student, _ = student_and_product
    app = create_app()

    async def _get_db():
        yield db

    async def _get_student() -> Student:
        return student

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_student] = _get_student
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_create_order_opens_a_checkout(client, db, student_and_product, monkeypatch):
    _, product = student_and_product
    monkeypatch.setattr(SslCommerzClient, "create_session", _fake_create_session)

    async with client as c:
        resp = await c.post(
            "/api/v1/shop/orders",
            json={"product_id": str(product.id), "idempotency_key": "idem-0001"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["checkout_url"] == "https://sandbox.example/pay/abc"
    # Amount is copied from the product, in paisa.
    assert body["amount_paisa"] == 10000

    order = (await db.execute(select(Order))).scalar_one()
    assert order.status is OrderStatus.pending


async def test_a_validated_return_issues_the_ticket(client, db, student_and_product, monkeypatch):
    _, product = student_and_product
    monkeypatch.setattr(SslCommerzClient, "create_session", _fake_create_session)

    async def _fake_validate(self, val_id: str) -> dict:
        return _valid_response(val_id=val_id)

    monkeypatch.setattr(SslCommerzClient, "validate", _fake_validate)

    async with client as c:
        created = await c.post(
            "/api/v1/shop/orders",
            json={"product_id": str(product.id), "idempotency_key": "idem-0002"},
        )
        tran_id = created.json()["tran_id"]
        settled = await c.post(
            "/api/v1/shop/payments/return",
            data={"tran_id": tran_id, "val_id": "VAL-1", "status": "VALID"},
        )

    assert settled.status_code == 200
    assert settled.json()["status"] == "paid"

    order = (await db.execute(select(Order))).scalar_one()
    assert order.status is OrderStatus.paid
    ticket = (await db.execute(select(Ticket))).scalar_one()
    assert ticket.rides_remaining == 10


async def test_a_failed_return_issues_no_ticket(client, db, student_and_product, monkeypatch):
    _, product = student_and_product
    monkeypatch.setattr(SslCommerzClient, "create_session", _fake_create_session)

    async with client as c:
        created = await c.post(
            "/api/v1/shop/orders",
            json={"product_id": str(product.id), "idempotency_key": "idem-0003"},
        )
        tran_id = created.json()["tran_id"]
        settled = await c.post(
            "/api/v1/shop/payments/return",
            data={"tran_id": tran_id, "status": "FAILED"},
        )

    assert settled.status_code == 200
    assert settled.json()["status"] == "failed"

    order = (await db.execute(select(Order))).scalar_one()
    assert order.status is OrderStatus.failed
    assert (await db.execute(select(Ticket))).first() is None
