"""Production ASGI entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request

from app.api.v1 import router
from app.config import Settings
from app.persistence.database import Database
from app.storage.filesystem import FilesystemArtifactStore


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.database_path)
        database.initialize(settings.federation_config)
        application.state.settings = settings
        application.state.database = database
        application.state.artifact_store = FilesystemArtifactStore(settings.artifact_path, settings.max_artifact_bytes)
        yield

    application = FastAPI(
        title="deki-smpc Server",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    application.include_router(router)

    @application.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "live"}

    @application.get("/health/ready", include_in_schema=False)
    def ready(request: Request) -> dict[str, str]:
        database = cast(Database, request.app.state.database)
        with database.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ready"}

    return application


app = create_app(Settings.from_env())
