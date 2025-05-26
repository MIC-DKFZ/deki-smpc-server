import gzip
import io
import json
import logging
import os
import secrets
import time
from hashlib import sha256
from io import BytesIO

import bcrypt
import redis
import torch
import uvicorn
from app.config import HOST, PORT

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from models import CheckForTaskRequest, KeyClientRegistration
from starlette.requests import Request
from utils import ActiveTasks, ActiveTasksPhase2

from key_aggregation.routes import router as key_aggregation_router

# from .secure_fl.routes import router as secure_fl_router

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":

    tags_metadata = [
        {
            "name": "Key Aggregation",
            "description": "Aggregate keys from multiple clients",
        },
        {
            "name": "Client Registration",
            "description": "Register clients for key aggregation",
        },
        {
            "name": "Task Management",
            "description": "Manage active tasks and their phases",
        },
        {
            "name": "Health Check",
            "description": "Check the health of the server",
        },
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
    # app.include_router(
    #     secure_fl_router, prefix="/secure-fl", tags=["Federated Learning"]
    # )

    uvicorn.run(app, host=HOST, port=PORT)
