from __future__ import annotations

from fastapi import FastAPI

from app.api.webhooks import router as webhooks_router
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Missed Call Text-Back")
    app.include_router(webhooks_router)
    return app


app = create_app()
