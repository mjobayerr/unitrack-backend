"""Create an admin, or reset an existing admin's password — server-side only.

Admins have no "forgot password" on the web on purpose: an admin console is a
higher-value target than a student wallet, and an emailed reset link is one more
way in. Recovery is this command instead, which can only be run by someone with
shell access to the server.

Usage (on the VPS, from ~/unitrack-backend):

    docker compose -f docker-compose.cloudflared.yml --env-file .env.prod \\
        exec api python -m scripts.set_admin_password EMAIL PASSWORD ["Full Name"]

Creates the admin if the email is unknown; otherwise resets its password (and
reactivates it). Refuses to touch a non-admin account, so it can never be used
to hijack a student or helper by reusing their address.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.user import User, UserRole, UserStatus

MIN_PASSWORD_LEN = 8


async def set_admin_password(email: str, password: str, name: str | None) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LEN} characters.")

    email = email.strip().lower()
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

        if user is None:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(password),
                    role=UserRole.admin,
                    name=name or "UniTrack Admin",
                    status=UserStatus.active,
                )
            )
            await db.commit()
            print(f"Created admin {email}.")
            return

        if user.role is not UserRole.admin:
            raise SystemExit(
                f"{email} exists as a {user.role.value}, not an admin — refusing to change it."
            )

        user.password_hash = hash_password(password)
        user.status = UserStatus.active
        if name:
            user.name = name
        await db.commit()
        print(f"Reset password for admin {email}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or reset an admin account.")
    parser.add_argument("email")
    parser.add_argument("password")
    parser.add_argument("name", nargs="?", default=None)
    args = parser.parse_args()
    asyncio.run(set_admin_password(args.email, args.password, args.name))


if __name__ == "__main__":
    main()
