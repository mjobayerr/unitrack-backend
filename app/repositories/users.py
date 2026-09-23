"""Data access for identity: users, students, helpers.

Reads only build queries here; the caller stages writes and owns the commit.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Helper, HelperStatus, Student, User


async def get(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def by_email(db: AsyncSession, email: str) -> User | None:
    """Case-folded: addresses are stored and looked up in lower case, so a login
    is not defeated by the shift key."""
    return await db.scalar(select(User).where(User.email == email.lower()))


async def student_by_user_id(db: AsyncSession, user_id: uuid.UUID) -> Student | None:
    return await db.scalar(select(Student).where(Student.user_id == user_id))


async def student_id_taken(db: AsyncSession, student_id_no: str) -> bool:
    """Whether a student ID is already registered — selects the id alone, never
    the row, since the answer is a yes/no."""
    found = await db.scalar(select(Student.id).where(Student.student_id_no == student_id_no))
    return found is not None


async def get_helper(db: AsyncSession, helper_id: uuid.UUID) -> Helper | None:
    return await db.get(Helper, helper_id)


async def helper_by_user_id(db: AsyncSession, user_id: uuid.UUID) -> Helper | None:
    return await db.scalar(select(Helper).where(Helper.user_id == user_id))


async def helpers_with_users(
    db: AsyncSession, status: HelperStatus | None = None
) -> list[tuple[User, Helper]]:
    """Helper accounts with their user rows — the admin approval queue, oldest
    first. Filtered by approval state when one is given."""
    stmt = select(User, Helper).join(Helper, Helper.user_id == User.id)
    if status is not None:
        stmt = stmt.where(Helper.status == status)
    return list((await db.execute(stmt.order_by(User.created_at))).all())
