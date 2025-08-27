import logging

import uvicorn
from app.config import HOST, PORT
from fastapi import FastAPI
from key_aggregation.routes import router as key_aggregation_router
from maintenance.routes import router as maintenance_router
from secure_fl.routes import router as secure_fl_router

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":

    tags_metadata = [
        {
            "name": "Key Aggregation",
            "description": "Aggregate keys from multiple clients",
        },
        {"name": "Maintenance", "description": "Endpoints for maintenance tasks"},
        {
            "name": "Federated Learning",
            "description": "Endpoints related to federated learning tasks",
        },
    ]
    app = FastAPI(
        root_path="/",
        title="Key Aggregation Server",
        description="A server for aggregating keys from multiple clients and performing federated learning.",
        version="1.0.0",
        openapi_tags=tags_metadata,
        docs_url="/docs",
    )

    # Include routers for different functionalities
    app.include_router(
        key_aggregation_router, prefix="/key-aggregation", tags=["Key Aggregation"]
    )
    app.include_router(
        secure_fl_router, prefix="/secure-fl", tags=["Federated Learning"]
    )
    app.include_router(maintenance_router, prefix="/maintenance", tags=["Maintenance"])

    uvicorn.run(app, host=HOST, port=PORT)
