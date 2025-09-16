import logging

import uvicorn
from app.config import HOST, PORT
from fastapi import FastAPI
from key_distribution.routes import router as key_aggregation_router, generate_keys
import asyncio
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)


async def init_stuff():
    # Generate keys at startup so they are available for download immediately.
    # Call the route handler directly (it is an async function that returns a dict).
    await generate_keys()
    await asyncio.sleep(0.2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_stuff()  # runs once at startup
    yield  # app serves requests after this line


if __name__ == "__main__":

    tags_metadata = [
        {
            "name": "Key Distribution",
            "description": "Endpoints related to key distribution tasks",
        },
    ]
    app = FastAPI(
        root_path="/",
        title="Key Distribution Server",
        description="A server for distributing keys to multiple clients and performing federated learning.",
        version="1.0.0",
        openapi_tags=tags_metadata,
        docs_url="/docs",
        lifespan=lifespan,
    )

    # Include routers for different functionalities
    app.include_router(
        key_aggregation_router, prefix="/key-distribution", tags=["Key Distribution"]
    )

    uvicorn.run(app, host=HOST, port=PORT)
