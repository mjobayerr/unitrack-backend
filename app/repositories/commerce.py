"""Data access for commerce: products, orders, tickets.

Reads only. Issuing a ticket and settling an order stage their writes in
app/services/settlement.py; the caller commits.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commerce import Order, Ticket, TicketProduct, TicketStatus
from app.models.user import Student, User


async def get_product(db: AsyncSession, product_id: uuid.UUID) -> TicketProduct | None:
    return await db.get(TicketProduct, product_id)


async def list_products(db: AsyncSession, active_only: bool) -> list[TicketProduct]:
    """The catalogue, cheapest first. Active-only for the student shop; the whole
    list, withdrawn products included, for the admin console."""
    stmt = select(TicketProduct)
    if active_only:
        stmt = stmt.where(TicketProduct.active.is_(True))
    return list((await db.execute(stmt.order_by(TicketProduct.price_paisa))).scalars())


async def get_ticket(db: AsyncSession, ticket_id: uuid.UUID) -> Ticket | None:
    return await db.get(Ticket, ticket_id)


async def order_by_idempotency_key(db: AsyncSession, key: str) -> Order | None:
    return await db.scalar(select(Order).where(Order.idempotency_key == key))


async def order_by_tran_id(db: AsyncSession, tran_id: str) -> Order | None:
    return await db.scalar(select(Order).where(Order.tran_id == tran_id))


async def orders_for_student(db: AsyncSession, student_id: uuid.UUID) -> list[Order]:
    stmt = select(Order).where(Order.student_id == student_id).order_by(Order.created_at.desc())
    return list((await db.execute(stmt)).scalars())


async def tickets_for_student(db: AsyncSession, student_id: uuid.UUID) -> list[Ticket]:
    stmt = select(Ticket).where(Ticket.student_id == student_id).order_by(Ticket.created_at.desc())
    return list((await db.execute(stmt)).scalars())


async def active_manifest(
    db: AsyncSession, now: datetime.datetime, limit: int
) -> Sequence[Row]:
    """The helper's offline ticket manifest — active, in-date tickets with the
    public key and just enough identity for the dead-phone fallback. Explicitly
    joined rather than walked through relationships: it runs once per trip start
    for every active ticket, and a lazy load per row would be thousands of them.
    """
    stmt = (
        select(
            Ticket.id,
            Ticket.qr_public_key,
            Ticket.rides_remaining,
            Ticket.valid_to,
            Ticket.status,
            User.name,
            Student.student_id_no,
        )
        .join(Student, Student.id == Ticket.student_id)
        .join(User, User.id == Student.user_id)
        .where(Ticket.status == TicketStatus.active, Ticket.valid_to >= now)
        .order_by(Ticket.valid_to)
        .limit(limit)
    )
    return (await db.execute(stmt)).all()
