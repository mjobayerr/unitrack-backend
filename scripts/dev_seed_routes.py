"""Dev helper: seed stops and a two-direction route so trips can be started.

Usage:
    python -m scripts.dev_seed_routes

Idempotent — run it as often as you like. Real route management belongs in the
admin panel; this exists so the helper app has something to pick from today.

Coordinates trace a plausible Dhanmondi -> Uttara corridor. They are for local
development only, not survey data.
"""

import asyncio

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.fleet import Route, RouteDirection, RouteStop, Stop

# name, lat, lng — ordered south to north.
STOPS: list[tuple[str, float, float]] = [
    ("Dhanmondi 27", 23.7561, 90.3720),
    ("Kalabagan", 23.7480, 90.3830),
    ("Farmgate", 23.7583, 90.3897),
    ("Mohakhali", 23.7806, 90.4053),
    ("Banani", 23.7936, 90.4043),
    ("Airport", 23.8513, 90.4085),
    ("Uttara Sector 7", 23.8759, 90.3795),
]

ROUTE_NAME = "Campus Shuttle"
# Minutes from trip start, in normal Dhaka traffic.
OFFSETS = [0, 8, 18, 32, 40, 58, 70]


async def main() -> None:
    async with SessionLocal() as db:
        stops: list[Stop] = []
        for name, lat, lng in STOPS:
            stop = (
                await db.execute(select(Stop).where(Stop.name == name))
            ).scalar_one_or_none()
            if stop is None:
                stop = Stop(name=name, lat=lat, lng=lng)
                db.add(stop)
                await db.flush()
            stops.append(stop)
        print(f"Stops ready: {len(stops)}")

        for direction in (RouteDirection.outbound, RouteDirection.inbound):
            route = (
                await db.execute(
                    select(Route).where(
                        Route.name == ROUTE_NAME, Route.direction == direction
                    )
                )
            ).scalar_one_or_none()
            if route is not None:
                print(f"Route {ROUTE_NAME} {direction} already exists -> route_id={route.id}")
                continue

            route = Route(name=ROUTE_NAME, direction=direction, is_active=True)
            db.add(route)
            await db.flush()

            # Inbound is the same stops walked backwards, so the offsets reverse
            # with them rather than being re-measured.
            ordered = stops if direction is RouteDirection.outbound else list(reversed(stops))
            for seq, stop in enumerate(ordered, start=1):
                db.add(
                    RouteStop(
                        route_id=route.id,
                        stop_id=stop.id,
                        seq=seq,
                        scheduled_offset_min=OFFSETS[seq - 1],
                    )
                )
            print(f"Created route {ROUTE_NAME} {direction} -> route_id={route.id}")

        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
