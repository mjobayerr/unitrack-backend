import logging

from fastapi import FastAPI

from app.api.routes import api_router
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

    # One line, forever. New routers register in app/api/routes/__init__.py,
    # where the auth-coverage test can also see them.
    app.include_router(api_router)
    return app


app = create_app()
