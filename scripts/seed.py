"""
UniTrack development database seeder.

Seeds the full system with realistic test data — accounts, fleet, routes, a live
trip in progress, completed trips with seat reports, and open alerts — so every
API surface has something to hit immediately after setup.

Usage
-----
    python -m scripts.seed                  seed everything (asks before overwriting)
    python -m scripts.seed all              same
    python -m scripts.seed users buses      seed specific groups only
    python -m scripts.seed all --wipe       no prompt: wipe seed rows and redo them
    python -m scripts.seed all --keep       no prompt: leave existing rows alone

Groups (seed order)
-------------------
    users     admin + 2 approved helpers + 3 active students
    buses     4 buses (3 active, 1 inactive)
    stops     7 stops along the Dhanmondi -> Uttara corridor
    routes    Campus Shuttle outbound + inbound (with stop sequences)
    trips     2 live trips (one each way), 2 completed trips
    reports   5 seat reports spread across the completed trips
    alerts    2 open alerts (1 critical SOS, 1 warning breakdown)
    commerce  3 ticket products + a paid order and active ticket for student1

Accounts seeded
---------------
    Role      Email                        Password
    ------    ---------------------------  -----------
    admin     admin@ulab.edu.bd            Admin@1234
    helper    helper1@buscrew.com.bd       Helper@1234
    helper    helper2@buscrew.com.bd       Helper@1234
    student   student1@ulab.edu.bd         Student@1234
    student   student2@ulab.edu.bd         Student@1234
    student   student3@ulab.edu.bd         Student@1234
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.commerce import (
    Order,
    OrderStatus,
    ProductType,
    Redemption,
    Ticket,
    TicketProduct,
)
from app.models.fleet import (
    Bus,
    BusStatus,
    Route,
    RouteDirection,
    RouteStop,
    Stop,
    Trip,
    TripStatus,
)
from app.models.ops import (
    Alert,
    AlertSeverity,
    AlertSource,
    AlertStatus,
    AlertType,
    SeatReport,
)
from app.models.user import Helper, HelperStatus, Student, User, UserRole, UserStatus
from app.services.settlement import issue_ticket

# ---------------------------------------------------------------------------
# Static seed data
# ---------------------------------------------------------------------------

_ADMIN = {"email": "admin@ulab.edu.bd", "password": "Admin@1234", "name": "Dev Admin"}

# NOT a `.test` domain, however tempting. `LoginRequest.email` is an `EmailStr`,
# and email-validator rejects RFC 2606 reserved TLDs — so a seeded
# `@unitrack.test` account gets a 422 from POST /auth/login before the password
# is ever checked. The row exists and looks fine in psql; it simply cannot sign
# in. Seeding writes straight to Postgres, which is why nothing here catches it.
_HELPERS = [
    {"email": "helper1@buscrew.com.bd", "password": "Helper@1234",
     "name": "Dev Helper 1", "phone": "+8801711111111"},
    {"email": "helper2@buscrew.com.bd", "password": "Helper@1234",
     "name": "Dev Helper 2", "phone": "+8801722222222"},
]

_STUDENTS = [
    {"email": "student1@ulab.edu.bd", "password": "Student@1234", "name": "Alice Rahman",
     "student_id_no": "CSE-2021-001", "department": "CSE", "batch": "2021"},
    {"email": "student2@ulab.edu.bd", "password": "Student@1234", "name": "Bob Hossain",
     "student_id_no": "EEE-2022-002", "department": "EEE", "batch": "2022"},
    {"email": "student3@ulab.edu.bd", "password": "Student@1234", "name": "Carol Islam",
     "student_id_no": "BBA-2023-003", "department": "BBA", "batch": "2023"},
]

_ALL_EMAILS = (
    {_ADMIN["email"]}
    | {h["email"] for h in _HELPERS}
    | {s["email"] for s in _STUDENTS}
)

_BUSES = [
    {
        "reg_no": "UA-METRO-01",
        "nickname": "Campus Express 1",
        "capacity": 45,
        "status": BusStatus.active,
    },
    {
        "reg_no": "UA-METRO-02",
        "nickname": "Campus Express 2",
        "capacity": 45,
        "status": BusStatus.active,
    },
    {
        "reg_no": "UA-METRO-03",
        "nickname": "City Link",
        "capacity": 30,
        "status": BusStatus.active,
    },
    {
        "reg_no": "UA-METRO-04",
        "nickname": "Night Runner",
        "capacity": 30,
        "status": BusStatus.inactive,
    },
]

# (name, lat, lng) — south to north
_STOPS = [
    ("Dhanmondi 27",    23.7561, 90.3720),
    ("Kalabagan",       23.7480, 90.3830),
    ("Farmgate",        23.7583, 90.3897),
    ("Mohakhali",       23.7806, 90.4053),
    ("Banani",          23.7936, 90.4043),
    ("Airport",         23.8513, 90.4085),
    ("Uttara Sector 7", 23.8759, 90.3795),
]

_ROUTE_NAME = "Campus Shuttle"
_ROUTE_OFFSETS = [0, 8, 18, 32, 40, 58, 70]  # minutes from trip start

# Prices in paisa, which is how the whole system stores money — 1 BDT = 100
# paisa. Integers all the way to the gateway, because a fare is never a float.
_PRODUCTS = [
    {
        "name": "Single Ride",
        "type": ProductType.single,
        "price_paisa": 3000,      # 30 BDT
        "ride_count": 1,
        "validity_days": 7,
    },
    {
        "name": "10-Ride Pack",
        "type": ProductType.bulk,
        "price_paisa": 25000,     # 250 BDT — a ride cheaper than ten singles
        "ride_count": 10,
        "validity_days": 60,
    },
    {
        "name": "Monthly Pass",
        "type": ProductType.package,
        # ride_count None means unlimited: `Ticket.rides_remaining` stays NULL
        # and redemption stops checking a ride budget that does not exist.
        "price_paisa": 90000,     # 900 BDT
        "ride_count": None,
        "validity_days": 30,
    },
]

# The seeded student's paid order. `tran_id` is what the gateway echoes back, so
# the prefix makes a seeded order obvious in the orders table and impossible to
# confuse with a real settlement.
_SEED_TRAN_PREFIX = "SEED-"

# ---------------------------------------------------------------------------
# Seed order and wipe dependency map
# ---------------------------------------------------------------------------

# Groups must be seeded in this order (each depends on the ones before it).
SEED_ORDER = [
    "users", "buses", "stops", "routes", "trips", "reports", "alerts", "commerce",
]

# To safely wipe group G, these groups must be wiped first (FK RESTRICT chains).
# trips RESTRICT on buses, routes, helpers; helpers CASCADE from users;
# route_stops RESTRICT on stops (but CASCADE from routes); seat_reports RESTRICT
# on helpers (but CASCADE from trips). Wiping trips first untangles everything.
#
# commerce is in `users` because orders and tickets both reference the student
# row: deleting the seeded users first hits a RESTRICT on orders.student_id.
_WIPE_REQUIRES: dict[str, list[str]] = {
    "alerts":   [],
    "reports":  [],
    "trips":    [],
    "commerce": [],
    "routes":   ["trips"],
    "stops":    ["trips", "routes"],
    "buses":    ["trips"],
    "users":    ["trips", "commerce"],
}

# Wipe is always done in reverse seed order, filtered to the effective set.
_WIPE_ORDER = list(reversed(SEED_ORDER))


def _expand_wipe_set(groups: list[str]) -> set[str]:
    """Return groups + all groups that must be wiped first (transitive)."""
    needed: set[str] = set(groups)
    changed = True
    while changed:
        changed = False
        for g in list(needed):
            for dep in _WIPE_REQUIRES.get(g, []):
                if dep not in needed:
                    needed.add(dep)
                    changed = True
    return needed


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

async def _user(db: AsyncSession, email: str) -> User | None:
    return (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()


async def _bus(db: AsyncSession, reg_no: str) -> Bus | None:
    return (await db.execute(select(Bus).where(Bus.reg_no == reg_no))).scalar_one_or_none()


async def _stop(db: AsyncSession, name: str) -> Stop | None:
    return (await db.execute(select(Stop).where(Stop.name == name))).scalar_one_or_none()


async def _route(db: AsyncSession, direction: RouteDirection) -> Route | None:
    return (await db.execute(
        select(Route).where(Route.name == _ROUTE_NAME, Route.direction == direction)
    )).scalar_one_or_none()


async def _helper_ids(db: AsyncSession) -> list:
    emails = [h["email"] for h in _HELPERS]
    uids = (await db.execute(select(User.id).where(User.email.in_(emails)))).scalars().all()
    if not uids:
        return []
    return (await db.execute(select(Helper.id).where(Helper.user_id.in_(uids)))).scalars().all()


# ---------------------------------------------------------------------------
# Count (how many seed-owned rows exist?)
# ---------------------------------------------------------------------------

async def _count_users(db: AsyncSession) -> int:
    return (await db.execute(
        select(func.count()).select_from(User).where(User.email.in_(_ALL_EMAILS))
    )).scalar_one()


async def _count_buses(db: AsyncSession) -> int:
    return (await db.execute(
        select(func.count()).select_from(Bus)
        .where(Bus.reg_no.in_([b["reg_no"] for b in _BUSES]))
    )).scalar_one()


async def _count_stops(db: AsyncSession) -> int:
    return (await db.execute(
        select(func.count()).select_from(Stop)
        .where(Stop.name.in_([s[0] for s in _STOPS]))
    )).scalar_one()


async def _count_routes(db: AsyncSession) -> int:
    return (await db.execute(
        select(func.count()).select_from(Route).where(Route.name == _ROUTE_NAME)
    )).scalar_one()


async def _count_trips(db: AsyncSession) -> int:
    hids = await _helper_ids(db)
    if not hids:
        return 0
    return (await db.execute(
        select(func.count()).select_from(Trip).where(Trip.helper_id.in_(hids))
    )).scalar_one()


async def _count_reports(db: AsyncSession) -> int:
    hids = await _helper_ids(db)
    if not hids:
        return 0
    return (await db.execute(
        select(func.count()).select_from(SeatReport).where(SeatReport.helper_id.in_(hids))
    )).scalar_one()


async def _count_alerts(db: AsyncSession) -> int:
    uids = (await db.execute(
        select(User.id).where(User.email.in_(_ALL_EMAILS))
    )).scalars().all()
    if not uids:
        return 0
    return (await db.execute(
        select(func.count()).select_from(Alert).where(Alert.raised_by.in_(uids))
    )).scalar_one()


async def _count_commerce(db: AsyncSession) -> int:
    return (await db.execute(
        select(func.count()).select_from(TicketProduct)
        .where(TicketProduct.name.in_([p["name"] for p in _PRODUCTS]))
    )).scalar_one()


_COUNT_FNS = {
    "users": _count_users, "buses": _count_buses, "stops": _count_stops,
    "routes": _count_routes, "trips": _count_trips, "reports": _count_reports,
    "alerts": _count_alerts, "commerce": _count_commerce,
}

# ---------------------------------------------------------------------------
# Wipe
# ---------------------------------------------------------------------------

async def _wipe_alerts(db: AsyncSession) -> int:
    uids = (await db.execute(
        select(User.id).where(User.email.in_(_ALL_EMAILS))
    )).scalars().all()
    if not uids:
        return 0
    r = await db.execute(delete(Alert).where(Alert.raised_by.in_(uids)))
    return r.rowcount


async def _wipe_reports(db: AsyncSession) -> int:
    hids = await _helper_ids(db)
    if not hids:
        return 0
    r = await db.execute(delete(SeatReport).where(SeatReport.helper_id.in_(hids)))
    return r.rowcount


async def _wipe_trips(db: AsyncSession) -> int:
    hids = await _helper_ids(db)
    if not hids:
        return 0
    # Deleting a trip cascades its SeatReports automatically.
    r = await db.execute(delete(Trip).where(Trip.helper_id.in_(hids)))
    return r.rowcount


async def _wipe_routes(db: AsyncSession) -> int:
    # Deleting a route cascades its RouteStop rows.
    r = await db.execute(delete(Route).where(Route.name == _ROUTE_NAME))
    return r.rowcount


async def _wipe_stops(db: AsyncSession) -> int:
    r = await db.execute(delete(Stop).where(Stop.name.in_([s[0] for s in _STOPS])))
    return r.rowcount


async def _wipe_buses(db: AsyncSession) -> int:
    r = await db.execute(delete(Bus).where(Bus.reg_no.in_([b["reg_no"] for b in _BUSES])))
    return r.rowcount


async def _wipe_users(db: AsyncSession) -> int:
    # Deleting a user cascades its Helper and Student rows, and any helper this
    # admin approved has its `approved_by` set to NULL by the database
    # (migration e9c3a7b41f26). Before that migration this raised
    # ForeignKeyViolationError on every database where the seed admin had ever
    # approved anyone, so run `alembic upgrade head` if this fails that way.
    r = await db.execute(delete(User).where(User.email.in_(_ALL_EMAILS)))
    return r.rowcount


async def _wipe_commerce(db: AsyncSession) -> int:
    """Remove seeded products and everything hanging off them.

    Deleted innermost-first — redemptions, tickets, orders, then products —
    because every one of those foreign keys is RESTRICT. Scoped to the seeded
    product names so a real order placed against a real product survives a
    reseed; the sub-selects are what keeps that scope, rather than a bare
    `delete(Ticket)` that would empty the wallet of anyone using the stack.
    """
    product_ids = select(TicketProduct.id).where(
        TicketProduct.name.in_([p["name"] for p in _PRODUCTS])
    )
    ticket_ids = select(Ticket.id).where(Ticket.product_id.in_(product_ids))

    await db.execute(delete(Redemption).where(Redemption.ticket_id.in_(ticket_ids)))
    await db.execute(delete(Ticket).where(Ticket.product_id.in_(product_ids)))
    await db.execute(delete(Order).where(Order.product_id.in_(product_ids)))
    r = await db.execute(delete(TicketProduct).where(TicketProduct.id.in_(product_ids)))
    return r.rowcount


_WIPE_FNS = {
    "alerts": _wipe_alerts, "reports": _wipe_reports, "trips": _wipe_trips,
    "routes": _wipe_routes, "stops": _wipe_stops, "buses": _wipe_buses,
    "users": _wipe_users, "commerce": _wipe_commerce,
}

# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

async def _seed_users(db: AsyncSession) -> dict:
    admin = await _user(db, _ADMIN["email"])
    if admin is None:
        admin = User(
            email=_ADMIN["email"],
            password_hash=hash_password(_ADMIN["password"]),
            role=UserRole.admin, name=_ADMIN["name"], status=UserStatus.active,
        )
        db.add(admin)
        await db.flush()

    helper_rows: list[Helper] = []
    for h in _HELPERS:
        u = await _user(db, h["email"])
        if u is None:
            u = User(
                email=h["email"], password_hash=hash_password(h["password"]),
                role=UserRole.helper, name=h["name"], phone=h["phone"],
                status=UserStatus.active,
            )
            db.add(u)
            await db.flush()
        row = (await db.execute(select(Helper).where(Helper.user_id == u.id))).scalar_one_or_none()
        if row is None:
            row = Helper(user_id=u.id, status=HelperStatus.approved, approved_by=admin.id)
            db.add(row)
            await db.flush()
        else:
            row.status = HelperStatus.approved
        helper_rows.append(row)

    student_rows: list[User] = []
    for s in _STUDENTS:
        u = await _user(db, s["email"])
        if u is None:
            u = User(
                email=s["email"], password_hash=hash_password(s["password"]),
                role=UserRole.student, name=s["name"], status=UserStatus.active,
            )
            db.add(u)
            await db.flush()
            db.add(Student(
                user_id=u.id, student_id_no=s["student_id_no"],
                department=s["department"], batch=s["batch"],
            ))
            await db.flush()
        student_rows.append(u)

    await db.commit()
    return {"admin": admin, "helpers": helper_rows, "students": student_rows}


async def _seed_buses(db: AsyncSession) -> list[Bus]:
    result = []
    for b in _BUSES:
        bus = await _bus(db, b["reg_no"])
        if bus is None:
            bus = Bus(**b)
            db.add(bus)
            await db.flush()
        result.append(bus)
    await db.commit()
    return result


async def _seed_stops(db: AsyncSession) -> list[Stop]:
    result = []
    for name, lat, lng in _STOPS:
        s = await _stop(db, name)
        if s is None:
            s = Stop(name=name, lat=lat, lng=lng)
            db.add(s)
            await db.flush()
        result.append(s)
    await db.commit()
    return result


async def _seed_routes(db: AsyncSession) -> list[Route]:
    stops = []
    for name, _, _ in _STOPS:
        s = await _stop(db, name)
        if s is None:
            raise RuntimeError(f"Stop '{name}' not found — run: python -m scripts.seed stops")
        stops.append(s)

    result = []
    for direction in (RouteDirection.outbound, RouteDirection.inbound):
        r = await _route(db, direction)
        if r is None:
            r = Route(name=_ROUTE_NAME, direction=direction, is_active=True)
            db.add(r)
            await db.flush()
            ordered = stops if direction is RouteDirection.outbound else list(reversed(stops))
            for seq, s in enumerate(ordered, start=1):
                db.add(RouteStop(
                    route_id=r.id, stop_id=s.id,
                    seq=seq, scheduled_offset_min=_ROUTE_OFFSETS[seq - 1],
                ))
        result.append(r)
    await db.commit()
    return result


async def _seed_trips(db: AsyncSession) -> list[Trip]:
    helper_users: list[User] = []
    helper_rows: list[Helper] = []
    for h in _HELPERS:
        u = await _user(db, h["email"])
        if u is None:
            raise RuntimeError(f"User {h['email']} not found — run: python -m scripts.seed users")
        row = (await db.execute(select(Helper).where(Helper.user_id == u.id))).scalar_one_or_none()
        if row is None:
            raise RuntimeError(
                f"Helper row for {h['email']} missing — run: python -m scripts.seed users"
            )
        helper_users.append(u)
        helper_rows.append(row)

    bus1 = await _bus(db, "UA-METRO-01")
    bus2 = await _bus(db, "UA-METRO-02")
    bus3 = await _bus(db, "UA-METRO-03")
    if not bus1 or not bus2 or not bus3:
        raise RuntimeError("Buses not found — run: python -m scripts.seed buses")

    outbound = await _route(db, RouteDirection.outbound)
    inbound  = await _route(db, RouteDirection.inbound)
    if not outbound or not inbound:
        raise RuntimeError("Routes not found — run: python -m scripts.seed routes")

    now   = datetime.now(UTC)
    today = now.date()

    # Trip 1 — LIVE, started ~30 min ago
    t1 = Trip(
        route_id=outbound.id, bus_id=bus1.id, helper_id=helper_rows[0].id,
        service_date=today,
        scheduled_start=now - timedelta(minutes=45),
        actual_start=now - timedelta(minutes=30),
        status=TripStatus.live,
    )
    db.add(t1)
    await db.flush()

    # Trip 1b — LIVE the other way, started ~15 min ago.
    #
    # Two live trips rather than one, because a single bus proves nothing about
    # a *fleet*: the admin map's per-bus pins, and a student who wants the bus
    # heading their way rather than the one that just passed, both need the
    # inbound direction to exist. A second helper is required, not a stylistic
    # choice — `uq_trips_one_live_per_helper` is a partial unique index that
    # allows exactly one live trip per helper.
    t1b = Trip(
        route_id=inbound.id, bus_id=bus3.id, helper_id=helper_rows[1].id,
        service_date=today,
        scheduled_start=now - timedelta(minutes=20),
        actual_start=now - timedelta(minutes=15),
        status=TripStatus.live,
    )
    db.add(t1b)
    await db.flush()

    # Trip 2 — COMPLETED, yesterday (inbound, helper2)
    t2_start = now - timedelta(days=1, hours=2)
    t2 = Trip(
        route_id=inbound.id, bus_id=bus2.id, helper_id=helper_rows[1].id,
        service_date=today - timedelta(days=1),
        scheduled_start=t2_start,
        actual_start=t2_start + timedelta(minutes=5),
        actual_end=t2_start + timedelta(hours=1, minutes=20),
        status=TripStatus.completed,
    )
    db.add(t2)
    await db.flush()

    # Trip 3 — COMPLETED, two days ago (outbound, helper1)
    t3_start = now - timedelta(days=2, hours=1)
    t3 = Trip(
        route_id=outbound.id, bus_id=bus1.id, helper_id=helper_rows[0].id,
        service_date=today - timedelta(days=2),
        scheduled_start=t3_start,
        actual_start=t3_start + timedelta(minutes=2),
        actual_end=t3_start + timedelta(hours=1, minutes=15),
        status=TripStatus.completed,
    )
    db.add(t3)
    await db.flush()

    await db.commit()
    return [t1, t1b, t2, t3]


async def _seed_reports(db: AsyncSession) -> list[SeatReport]:
    helper_rows: list[Helper] = []
    for h in _HELPERS:
        u = await _user(db, h["email"])
        if u is None:
            raise RuntimeError(f"User {h['email']} not found — run: python -m scripts.seed users")
        row = (await db.execute(select(Helper).where(Helper.user_id == u.id))).scalar_one_or_none()
        if row is None:
            raise RuntimeError("Helper row missing — run: python -m scripts.seed users")
        helper_rows.append(row)

    completed = (await db.execute(
        select(Trip)
        .where(Trip.helper_id.in_([r.id for r in helper_rows]), Trip.status == TripStatus.completed)
        .order_by(Trip.actual_start)
    )).scalars().all()
    if not completed:
        raise RuntimeError("No completed trips found — run: python -m scripts.seed trips")

    reports: list[SeatReport] = []

    # Completed trip belonging to helper2 (trip 2, inbound)
    t2 = next((t for t in completed if t.helper_id == helper_rows[1].id), None)
    if t2 and t2.actual_start:
        for occupied, minutes in [(12, 10), (27, 35), (40, 65)]:
            r = SeatReport(
                trip_id=t2.id, helper_id=helper_rows[1].id,
                occupied=occupied, capacity_snapshot=45,
                reported_at=t2.actual_start + timedelta(minutes=minutes),
            )
            db.add(r)
            reports.append(r)

    # Completed trip belonging to helper1 (trip 3, outbound)
    t3 = next((t for t in completed if t.helper_id == helper_rows[0].id), None)
    if t3 and t3.actual_start:
        for occupied, minutes in [(18, 15), (32, 45)]:
            r = SeatReport(
                trip_id=t3.id, helper_id=helper_rows[0].id,
                occupied=occupied, capacity_snapshot=45,
                reported_at=t3.actual_start + timedelta(minutes=minutes),
            )
            db.add(r)
            reports.append(r)

    await db.commit()
    return reports


async def _seed_alerts(db: AsyncSession) -> list[Alert]:
    h1_user = await _user(db, _HELPERS[0]["email"])
    h2_user = await _user(db, _HELPERS[1]["email"])
    if not h1_user or not h2_user:
        raise RuntimeError("Helper users not found — run: python -m scripts.seed users")

    h1_row = (
        await db.execute(select(Helper).where(Helper.user_id == h1_user.id))
    ).scalar_one_or_none()
    live_trip = None
    if h1_row:
        live_trip = (await db.execute(
            select(Trip).where(Trip.helper_id == h1_row.id, Trip.status == TripStatus.live)
        )).scalar_one_or_none()

    alerts: list[Alert] = []

    # Critical SOS on the live trip
    db.add(a1 := Alert(
        source=AlertSource.helper, raised_by=h1_user.id,
        trip_id=live_trip.id if live_trip else None,
        bus_id=live_trip.bus_id if live_trip else None,
        type=AlertType.sos, severity=AlertSeverity.critical,
        message="Emergency — passenger needs medical assistance.",
        lat=23.7583, lng=90.3897,  # Farmgate
        status=AlertStatus.open,
    ))
    alerts.append(a1)

    # Warning breakdown (no live trip reference)
    db.add(a2 := Alert(
        source=AlertSource.helper, raised_by=h2_user.id,
        type=AlertType.breakdown, severity=AlertSeverity.warning,
        message="Bus making unusual noise, may need inspection.",
        lat=23.7806, lng=90.4053,  # Mohakhali
        status=AlertStatus.open,
    ))
    alerts.append(a2)

    await db.commit()
    return alerts


async def _seed_commerce(db: AsyncSession) -> list[TicketProduct]:
    """The shop's catalogue, plus one already-paid ticket in student1's wallet.

    The ticket matters more than it looks. Without it the wallet renders its
    empty state and the boarding QR — the one screen the helper app scans, and
    the hardest thing in the system to test — has nothing to draw. Seeding a
    ticket means a fresh stack can be scanned end to end.

    It is created through `issue_ticket`, the same function the payment callback
    calls, rather than by hand. A hand-built row would need its own Ed25519
    keypair and its own validity arithmetic, and would then drift from the real
    one silently — the seeded ticket would keep working after a change that
    broke every ticket a student actually bought.
    """
    products: list[TicketProduct] = []
    for spec in _PRODUCTS:
        existing = (await db.execute(
            select(TicketProduct).where(TicketProduct.name == spec["name"])
        )).scalar_one_or_none()
        if existing is None:
            existing = TicketProduct(**spec, active=True)
            db.add(existing)
            await db.flush()
        products.append(existing)
    await db.commit()

    # The catalogue above is real operational data and belongs anywhere. The
    # fixture ticket below is a dev convenience that needs the seeded student1,
    # so on a stack without the dev users (production seeded with `commerce`
    # alone) it is skipped rather than fatal — the products still land.
    user = await _user(db, _STUDENTS[0]["email"])
    student = (
        await db.execute(select(Student).where(Student.user_id == user.id))
    ).scalar_one_or_none() if user is not None else None
    if student is None:
        print("  commerce: no seed student — products seeded, skipping the fixture ticket")
        return products

    # The 10-ride pack: a ticket with rides left is the more interesting fixture,
    # since it exercises the decrement path that a single ride never reaches
    # twice and an unlimited pass never reaches at all.
    pack = products[1]
    tran_id = f"{_SEED_TRAN_PREFIX}{student.id.hex[:12]}"

    order = (await db.execute(
        select(Order).where(Order.tran_id == tran_id)
    )).scalar_one_or_none()
    if order is not None:
        return products

    now = datetime.now(UTC)
    order = Order(
        student_id=student.id,
        product_id=pack.id,
        amount_paisa=pack.price_paisa,
        currency="BDT",
        status=OrderStatus.paid,
        idempotency_key=tran_id,
        tran_id=tran_id,
        gateway="seed",
        paid_at=now - timedelta(days=1),
    )
    db.add(order)
    await db.flush()
    await issue_ticket(db, order)
    await db.commit()
    return products


_SEED_FNS = {
    "users": _seed_users, "buses": _seed_buses, "stops": _seed_stops,
    "routes": _seed_routes, "trips": _seed_trips, "reports": _seed_reports,
    "alerts": _seed_alerts, "commerce": _seed_commerce,
}

# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_summary(results: dict, groups: list[str]) -> None:
    sep = "-" * 60
    print()
    print(sep)
    print("  UniTrack dev seed complete")
    print(sep)

    if "users" in groups:
        print()
        print("  ACCOUNTS")
        print(f"  {'Role':<8}  {'Email':<30}  Password")
        print(f"  {'----':<8}  {'-----':<30}  --------")
        print(f"  {'admin':<8}  {_ADMIN['email']:<30}  {_ADMIN['password']}")
        for h in _HELPERS:
            print(f"  {'helper':<8}  {h['email']:<30}  {h['password']}")
        for s in _STUDENTS:
            print(f"  {'student':<8}  {s['email']:<30}  {s['password']}")

    if "buses" in groups and "buses" in results:
        print()
        print("  BUSES")
        for bus in results["buses"]:
            status = f"[{bus.status}]" if bus.status != BusStatus.active else ""
            print(f"  {bus.reg_no:<20}  {(bus.nickname or ''):<24}  id={bus.id}  {status}")

    if "stops" in groups and "stops" in results:
        print()
        print("  STOPS  (7 along Dhanmondi -> Uttara)")
        for stop in results["stops"]:
            print(f"  {stop.name:<22}  id={stop.id}")

    if "routes" in groups and "routes" in results:
        print()
        print("  ROUTES")
        for route in results["routes"]:
            print(f"  {route.name} {route.direction:<10}  id={route.id}")

    if "trips" in groups and "trips" in results:
        trips = results["trips"]
        labels = [
            "LIVE  outbound, started ~30 min ago",
            "LIVE  inbound, started ~15 min ago",
            "DONE  (yesterday)",
            "DONE  (2 days ago)",
        ]
        print()
        print("  TRIPS")
        # strict=True so adding a seeded trip without a label fails loudly here
        # rather than silently printing a short list.
        for trip, label in zip(trips, labels, strict=True):
            print(f"  {label:<30}  id={trip.id}")

    if "reports" in groups and "reports" in results:
        print()
        print(f"  SEAT REPORTS  ({len(results['reports'])} total, on the 2 completed trips)")

    if "alerts" in groups and "alerts" in results:
        print()
        print("  ALERTS  (2 open)")
        print("  - CRITICAL  SOS — passenger medical emergency  [Farmgate]")
        print("  - WARNING   Breakdown — unusual bus noise       [Mohakhali]")

    if "commerce" in groups and "commerce" in results:
        print()
        print("  TICKET PRODUCTS")
        for product in results["commerce"]:
            rides = "unlimited" if product.ride_count is None else f"{product.ride_count} ride(s)"
            print(
                f"  {product.name:<16}  {product.price_paisa / 100:>7.2f} BDT  "
                f"{rides:<12}  {product.validity_days}d"
            )
        print()
        print(f"  WALLET  {_STUDENTS[0]['email']} holds a paid 10-Ride Pack")

    print()
    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _confirm_wipe() -> str:
    """Ask before destroying data. Refuses rather than guessing when nobody can answer.

    `input()` raises EOFError with no stdin — which is every non-interactive
    caller: a compose `command:`, a CI step, a piped shell. The traceback that
    came out of that looked like a database failure and said nothing about the
    flag that fixes it.
    """
    try:
        return input("Wipe and reseed? [y/N] ").strip().lower()
    except EOFError:
        print("\nNo terminal to ask on. Pass --wipe to replace the data, or --keep to leave it.")
        return "n"


async def _run(groups: list[str], force_wipe: bool, keep: bool) -> None:
    async with SessionLocal() as db:
        # 1. Count existing seed data in targeted groups
        counts: dict[str, int] = {}
        for g in groups:
            counts[g] = await _COUNT_FNS[g](db)

        dirty = {g: c for g, c in counts.items() if c > 0}

        # 2. --keep: leave what is there and seed only the empty groups.
        #
        # Skipping the group entirely, not just the wipe. `_seed_trips`,
        # `_seed_reports` and `_seed_alerts` add rows unconditionally — running
        # them against a seeded database would file a second live trip per
        # helper and hit `uq_trips_one_live_per_helper`, which is the partial
        # unique index that exists precisely to stop that. This is the flag the
        # `init` container uses on every `docker compose up`, so "already seeded"
        # has to be a no-op rather than a crash loop.
        if keep:
            if dirty:
                print(f"Keeping existing: {', '.join(f'{g} ({c})' for g, c in dirty.items())}")
            groups = [g for g in groups if g not in dirty]
            if not groups:
                print("Nothing to seed — every group already has data.")
                return

        if dirty and not keep:
            if not force_wipe:
                print("Existing seed data found:")
                for g, c in dirty.items():
                    print(f"  {g}: {c} row(s)")
                answer = _confirm_wipe()
                if answer != "y":
                    print("Aborted — nothing changed.")
                    return

            # 3. Compute full wipe set (include required dependencies)
            wipe_set = _expand_wipe_set(list(dirty.keys()))
            extra = wipe_set - set(groups)
            if extra:
                print(f"Note: also wiping {', '.join(sorted(extra))} (required by FK constraints).")

            print("Wiping...")
            for g in _WIPE_ORDER:
                if g in wipe_set:
                    n = await _WIPE_FNS[g](db)
                    if n:
                        print(f"  {g}: removed {n} row(s)")
            await db.commit()

        # 4. Seed in order
        print("Seeding...")
        results: dict[str, object] = {}
        for g in groups:
            try:
                results[g] = await _SEED_FNS[g](db)
                n = len(results[g]) if isinstance(results[g], list) else (
                    sum(len(v) for v in results[g].values() if isinstance(v, list))
                    if isinstance(results[g], dict) else 1
                )
                print(f"  {g}: {n} row(s)")
            except RuntimeError as exc:
                print(f"  ERROR ({g}): {exc}")
                sys.exit(1)

    _print_summary(results, groups)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "groups",
        nargs="*",
        default=["all"],
        metavar="GROUP",
        help=f"Groups to seed: all {' '.join(SEED_ORDER)}",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--wipe",
        action="store_true",
        help="No prompt: remove the existing seed rows and create them again.",
    )
    mode.add_argument(
        "--keep",
        action="store_true",
        help="No prompt: seed only the groups that are empty. Safe to run repeatedly.",
    )
    args = parser.parse_args()

    # Resolve group list
    selected: list[str] = args.groups
    if not selected or selected == ["all"] or "all" in selected:
        groups = SEED_ORDER
    else:
        unknown = set(selected) - set(SEED_ORDER)
        if unknown:
            print(f"Unknown group(s): {', '.join(sorted(unknown))}")
            print(f"Available: all  {' '.join(SEED_ORDER)}")
            sys.exit(1)
        # Keep dependency order even for partial seeds
        groups = [g for g in SEED_ORDER if g in set(selected)]

    asyncio.run(_run(groups, force_wipe=args.wipe, keep=args.keep))


if __name__ == "__main__":
    main()
