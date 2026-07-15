"""Seed the initial admin account (spec §identity — admin is seeded, further admins by invite).

Usage:
    ADMIN_EMAIL=admin@ulab.edu.bd ADMIN_PASSWORD=... ADMIN_NAME="Ops Admin" \
        python scripts/seed_admin.py

Idempotent: if the email already exists, it is left untouched.
"""

import asyncio
import os

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.user import User, UserRole, UserStatus


async def main() -> None:
    email = os.environ.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD")
    name = os.environ.get("ADMIN_NAME", "UniTrack Admin")
    if not email or not password:
        raise SystemExit("Set ADMIN_EMAIL and ADMIN_PASSWORD env vars.")

    async with SessionLocal() as db:
        existing = await db.execute(select(User).where(User.email == email.lower()))
        if existing.scalar_one_or_none():
            print(f"Admin {email} already exists — nothing to do.")
            return
        db.add(
            User(
                email=email.lower(),
                password_hash=hash_password(password),
                role=UserRole.admin,
                name=name,
                status=UserStatus.active,
            )
        )
        await db.commit()
        print(f"Seeded admin {email}.")


if __name__ == "__main__":
    asyncio.run(main())
