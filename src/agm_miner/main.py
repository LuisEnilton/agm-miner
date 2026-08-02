from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from agm_miner.api.routes import router
from agm_miner.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(title=settings.app_name)
    app.include_router(router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run("agm_miner.main:app", host="0.0.0.0", port=8000, reload=settings.environment == "development")