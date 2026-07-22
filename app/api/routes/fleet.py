"""Fleet reference data — what the helper app and the maps need to render.

Read-only and available to any signed-in account: a helper picks a bus and a
route here before starting a trip, and the student map needs stop positions and
route shapes to draw anything.

Admin CRUD for this data is not built yet; seed it with
`scripts/dev_seed_routes.py`.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_authenticated
from app.db.session import get_db
from app.models.fleet import Bus, BusStatus, Route, RouteStop, Stop
from app.schemas.fleet import BusOut, RouteDetailOut, RouteOut, RouteStopOut, StopOut

router = APIRouter(
    prefix="/fleet",
    tags=["fleet"],
    dependencies=[Depends(require_authenticated)],
)


@router.get("/buses", response_model=list[BusOut])
async def list_buses(
    db: AsyncSession = Depends(get_db),
    only_active: bool = Query(default=True, description="Hide inactive/maintenance buses."),
) -> list[Bus]:
    """The bus picker in the helper app.

    Replaces typing a UUID by hand, which was the previous state of affairs and
    is exactly as error-prone as it sounds.
    """
    stmt = select(Bus)
    if only_active:
        stmt = stmt.where(Bus.status == BusStatus.active)
    return list((await db.execute(stmt.order_by(Bus.reg_no))).scalars())


@router.get("/routes", response_model=list[RouteOut])
async def list_routes(
    db: AsyncSession = Depends(get_db),
    only_active: bool = Query(default=True),
) -> list[Route]:
    stmt = select(Route)
    if only_active:
        stmt = stmt.where(Route.is_active.is_(True))
    return list((await db.execute(stmt.order_by(Route.name, Route.direction))).scalars())


@router.get("/routes/{route_id}", response_model=RouteDetailOut)
async def get_route(route_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> RouteDetailOut:
    """One route with its ordered stops — what the map draws.

    `selectinload` fetches route_stops and their stops in two extra queries
    rather than one per stop. The lazy default would issue an N+1 storm on a
    route with thirty stops, on an endpoint every map load hits.
    """
    stmt = (
        select(Route)
        .where(Route.id == route_id)
        .options(selectinload(Route.stops).selectinload(RouteStop.stop))
    )
    route = (await db.execute(stmt)).scalar_one_or_none()
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown route")

    return RouteDetailOut(
        id=route.id,
        name=route.name,
        direction=route.direction,
        is_active=route.is_active,
        polyline=route.polyline,
        stops=[
            RouteStopOut(
                seq=rs.seq,
                scheduled_offset_min=rs.scheduled_offset_min,
                stop=StopOut.model_validate(rs.stop),
            )
            for rs in route.stops
        ],
    )


@router.get("/stops", response_model=list[StopOut])
async def list_stops(db: AsyncSession = Depends(get_db)) -> list[Stop]:
    return list((await db.execute(select(Stop).order_by(Stop.name))).scalars())
