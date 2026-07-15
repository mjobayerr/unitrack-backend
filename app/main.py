import logging

from fastapi import FastAPI

from app.api.routes import auth, helper, tracking
from app.core.config import settings

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    app = FastAPI(
        title="UniTrack API",
        version="0.1.0",
        description="Hub API for the UniTrack bus ticketing & live-tracking platform.",
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.env}

    app.include_router(auth.router)
    app.include_router(helper.router)
    app.include_router(tracking.router)
    return app


app = create_app()
