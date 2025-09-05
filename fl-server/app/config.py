import logging
import os
from hashlib import sha256

from openfhe import (
    CCParamsCKKSRNS,
    GenCryptoContext,
    PKESchemeFeature,
    ScalingTechnique,
)

logging.basicConfig(level=logging.INFO)

# Environment variables
NUM_CLIENTS = int(os.environ.get("NUM_CLIENTS", 3))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))

PRESHARED_SECRET = os.environ.get("PRESHARED_SECRET", "my_secure_presHared_secret_123!")
PRESHARED_SECRET = sha256(PRESHARED_SECRET.encode()).hexdigest()

logging.info(f"Number of clients: {NUM_CLIENTS}")
logging.info(f"Host: {HOST}")
logging.info(f"Port: {PORT}")

batch_size = 8
parameters = CCParamsCKKSRNS()
parameters.SetMultiplicativeDepth(5)
parameters.SetScalingModSize(50)
parameters.SetScalingTechnique(ScalingTechnique.FLEXIBLEAUTO)
parameters.SetBatchSize(batch_size)

crypto_context = GenCryptoContext(parameters)

logging.info(
    f"CKKS scheme is using ring dimension {crypto_context.GetRingDimension()}\n"
)

crypto_context.Enable(PKESchemeFeature.PKE)
crypto_context.Enable(PKESchemeFeature.KEYSWITCH)
crypto_context.Enable(PKESchemeFeature.LEVELEDSHE)
