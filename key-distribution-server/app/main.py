import logging

import uvicorn
from app.config import HOST, PORT
from fastapi import FastAPI
from key_distribution.routes import router as key_aggregation_router

logging.basicConfig(level=logging.INFO)

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
    )

    # Include routers for different functionalities
    app.include_router(
        key_aggregation_router, prefix="/key-distribution", tags=["Key Distribution"]
    )

    uvicorn.run(app, host=HOST, port=PORT)
