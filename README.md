# deki-smpc-server

**deki-smpc-server** is the server-side component of the [deki-smpc](https://github.com/MIC-DKFZ/deki-smpc) ecosystem, enabling **Secure Multi-Party Computation (SMPC)** for federated learning.  
It includes the **Key Aggregation Server** and the **FL Aggregation Server**, all deployable effortlessly with **Docker Compose**.

## 🛠️ Components

- **Key Aggregation Server**  
  REST API (FastAPI) for efficient, concurrent multi-party key generation and secure key exchange.
- **FL Aggregation Server**  
  Secure federated model aggregation based on FastAPI and PyTorch. (Used to be Flower)
- **Redis**  
  In-memory store for temporary key material and synchronization between parties.

---

## 🚀 Quickstart

1. Clone the repository:

   ```bash
   git clone https://github.com/your-org/deki-smpc-server.git
   cd deki-smpc-server
   ```

2. Launch the servers with Docker Compose:

   ```bash
   docker-compose up --build
   ```
✅ This will start:

- **Key Aggregation Server** at `http://localhost:8080`
- **Redis Server** at `localhost:6379`

---

## ⚙️ Configuration

The following environment variables are used (with their default values):

| Variable           | Description                                | Default                               |
|:------------------:|:-------------------------------------------|:-------------------------------------:|
| `HOST`              | Host IP to bind to                        | `0.0.0.0`                             |
| `NUM_CLIENTS`       | Number of expected clients                | `10`                                  |
| `PORT`              | Port to serve on                          | `8080`                                |
| `PRESHARED_SECRET`  | Secret key used for secure communication  | `my_secure_presHared_secret_123!`     |
| `REDIS_HOST`        | Redis host                                | `redis`                               |
| `REDIS_PORT`        | Redis port                                | `6379`                                |

You can override any of these by editing the `docker-compose.yml` file.