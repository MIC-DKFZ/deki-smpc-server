import asyncio
import logging
import os
from hashlib import sha256
from typing import Dict, Optional

import bcrypt
import redis
import torch

logging.basicConfig(level=logging.INFO)

# Environment variables
NUM_CLIENTS = int(os.environ.get("NUM_CLIENTS", 3))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
PRESHARED_SECRET = os.environ.get("PRESHARED_SECRET", "my_secure_presHared_secret_123!")
WEIGHT_TTL = int(os.environ.get("WEIGHT_TTL", 600))  # seconds

# Redis connection
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
R = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
R_BINARY = redis.Redis(
    host=REDIS_HOST, port=REDIS_PORT, db=0
)  # decode_responses=False is default
# Store Phase in Redis
R.set("phase", 1)

# Global variable to hold the aggregated model
aggregated_state_dict: Optional[Dict[str, torch.Tensor]] = None
aggregated_state_dict_lock = asyncio.Lock()

# Device
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# First hash the pre-shared secret with SHA-256, then bcrypt the result
sha256_hash = sha256(PRESHARED_SECRET.encode()).hexdigest().encode()
HASHED_PRESHARED_SECRET = bcrypt.hashpw(sha256_hash, bcrypt.gensalt())

# Delete the preshared secret from memory
del PRESHARED_SECRET
del sha256_hash

assert NUM_CLIENTS >= 3, "NUM_CLIENTS must be greater than 3"

logging.info(f"Number of clients: {NUM_CLIENTS}")
logging.info(f"Host: {HOST}")
logging.info(f"Port: {PORT}")
logging.info(f"Device: {DEVICE}")
