"""Dev helper: create a bus and approve a helper so GPS ingest can be tested.

Usage:
    BUS_REG_NO=DHK-01 HELPER_EMAIL=bob@anything.com python -m scripts.dev_seed_fleet

- Creates the bus if it does not exist (prints its id — use as `bus_id` in POST /helper/gps).
- If HELPER_EMAIL is given, flips that helper to approved + activates the user
  (the real path is an admin approval endpoint; this is a dev shortcut).
"""

import asyncio
import os

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.fleet import Bus
from app.models.user import Helper, HelperStatus, User, UserRole, UserStatus


async def main() -> None:
    reg_no = os.environ.get("BUS_REG_NO", "DHK-01")
    helper_email = os.environ.get("HELPER_EMAIL")

    async with SessionLocal() as db:
        bus = (await db.execute(select(Bus).where(Bus.reg_no == reg_no))).scalar_one_or_none()
        if bus is None:
            bus = Bus(reg_no=reg_no, nickname=reg_no, capacity=40)
            db.add(bus)
            await db.flush()
            print(f"Created bus {reg_no} -> bus_id={bus.id}")
        else:
            print(f"Bus {reg_no} already exists -> bus_id={bus.id}")

        if helper_email:
            user = (
                await db.execute(select(User).where(User.email == helper_email.lower()))
            ).scalar_one_or_none()
            if user is None or user.role != UserRole.helper:
                print(f"No helper user with email {helper_email} — register one first.")
            else:
                helper = (
                    await db.execute(select(Helper).where(Helper.user_id == user.id))
                ).scalar_one_or_none()
                helper.status = HelperStatus.approved
                user.status = UserStatus.active
                print(f"Approved helper {helper_email} (login now works).")

        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
