import logging

import uvicorn
from app.config import HOST, PORT
from fastapi import FastAPI

# from maintenance.routes import router as maintenance_router
from secure_fl.routes import router as secure_fl_router

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":

    tags_metadata = [
        # {"name": "Maintenance", "description": "Endpoints for maintenance tasks"},
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

    app.include_router(
        secure_fl_router, prefix="/secure-fl", tags=["Federated Learning"]
    )
    # app.include_router(maintenance_router, prefix="/maintenance", tags=["Maintenance"])

    uvicorn.run(app, host=HOST, port=PORT)
