# deki-smpc-server

`deki-smpc-server` is the server-side part of the [deki-smpc](https://github.com/MIC-DKFZ/deki-smpc) ecosystem.
This branch currently provides:

- a **Key Aggregation Server** (FastAPI)
- a **Redis** instance for coordination/state

The server exposes APIs for:
- client registration
- phased aggregation task orchestration
- artifact upload/download for secure aggregation
- final result handoff and reset

## Repository Layout

- `key-aggregation-server/app/main.py`: FastAPI app entrypoint
- `key-aggregation-server/app/key_aggregation/routes.py`: registration + phased aggregation APIs
- `key-aggregation-server/app/secure_fl/routes.py`: federated model upload/download APIs
- `key-aggregation-server/app/maintenance/routes.py`: debug and reset APIs
- `key-aggregation-server/app/utils.py`: task orchestration and artifact transfer helpers
- `docker-compose.yml`: local stack definition

## Architecture at a Glance

1. Clients register with `/key-aggregation/register`.
2. Server creates phase 1 and phase 2 task queues in Redis.
3. Clients poll for tasks, then upload/download intermediate artifacts.
4. Final sum is uploaded/downloaded via dedicated endpoints.
5. When all clients signal completion, server state is reset.

## Quickstart

1. Clone and enter the repository:

```bash
git clone <your-fork-or-origin-url>
cd deki-smpc-server
```

2. Start services:

```bash
docker compose up --build
```

3. Open API docs:

- Key Aggregation Server: `http://localhost:8080/docs`
- Redis: `localhost:6379`

4. Basic smoke check:

```bash
curl -s http://localhost:8080/maintenance/redis/keys
```

5. Stop services:

```bash
docker compose down
```

## Configuration

Configuration is primarily injected via `docker-compose.yml`.
Defaults used by the compose stack:

| Variable | Purpose | Default in compose |
| --- | --- | --- |
| `HOST` | Bind address | `0.0.0.0` |
| `PORT` | API port | `8080` |
| `NUM_CLIENTS` | Expected participant count | `4` |
| `PRESHARED_SECRET` | Shared registration secret | `my_secure_presHared_secret_123!` |
| `REDIS_HOST` | Redis hostname | `redis` |
| `REDIS_PORT` | Redis port | `6379` |

Note:
- Application code has internal fallbacks (for example, `NUM_CLIENTS=3`) but compose overrides these during normal local runs.

## API Overview

Base URL: `http://localhost:8080`

### Key Aggregation (`/key-aggregation`)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/register` | Register a client and trigger workflow when all clients are present |
| `GET` | `/tasks/participants` | List phase-1 and phase-2 participants |
| `GET` | `/aggregation/phase/{phase_id}/check_for_task` | Poll for next task (`phase_id` in `1,2`) |
| `PUT` | `/aggregation/upload` | Upload artifact for current task |
| `GET` | `/aggregation/download` | Download artifact for current task |
| `GET` | `/aggregation/phase/{phase_id}/active_tasks` | Inspect active/pending task state |
| `POST` | `/aggregation/final/upload` | Upload final aggregated sum |
| `GET` | `/aggregation/final/download` | Download final aggregated sum |
| `GET` | `/aggregation/final/recipient` | Get final recipient |
| `GET` | `/aggregation/phase/1/first_senders` | Get selected phase-1 first senders |
| `POST` | `/aggregation/finished` | Mark a client finished (can trigger reset) |

### Secure FL (`/secure-fl`)

| Method | Path | Purpose |
| --- | --- | --- |
| `PUT` | `/upload` | Upload model payload for secure FL aggregation |
| `GET` | `/download` | Download aggregated model payload |

### Maintenance (`/maintenance`)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/reset` | Reset task queues, state, and cached artifacts |
| `GET` | `/redis/keys` | Show phase-related Redis keys |
| `GET` | `/tasks` | Show queue entries used for aggregation |
| `GET` | `/redis/queues` | Show all aggregation queue contents |
| `GET` | `/redis/queues/{queue_name}` | Show one queue |
| `GET` | `/registered-participants` | Show registered clients |

## Request Conventions

Headers used by task-based endpoints:

- `X-Client-Name`: current client identifier
- `X-Phase`: current phase ID (`1` or `2`) for `/aggregation/upload` and `/aggregation/download`

`POST /key-aggregation/register` expects a JSON body:

```json
{
  "ip_address": "127.0.0.1",
  "client_name": "client-1",
  "preshared_secret": "my_secure_presHared_secret_123!"
}
```

`POST /key-aggregation/aggregation/final/upload` is multipart:

- `final_sum`: uploaded file
- `client_name`: form field

## Minimal End-to-End Flow (Manual)

1. Register all clients (`NUM_CLIENTS` times):

```bash
curl -X POST http://localhost:8080/key-aggregation/register \
  -H "Content-Type: application/json" \
  -d '{"ip_address":"127.0.0.1","client_name":"client-1","preshared_secret":"my_secure_presHared_secret_123!"}'
```

2. Poll task assignment for phase 1:

```bash
curl -X GET "http://localhost:8080/key-aggregation/aggregation/phase/1/check_for_task" \
  -H "Content-Type: application/json" \
  -d '{"client_name":"client-1"}'
```

3. Upload/download according to assigned action:
- upload: `PUT /key-aggregation/aggregation/upload` with headers `X-Client-Name` and `X-Phase`
- download: `GET /key-aggregation/aggregation/download` with same headers

4. After final sum upload/download, mark completion:

```bash
curl -X POST http://localhost:8080/key-aggregation/aggregation/finished \
  -H "Content-Type: application/json" \
  -d '{"client_name":"client-1"}'
```

## Operational Notes

- State is intentionally ephemeral:
  - task queues and coordination live in Redis
  - uploaded artifacts are held in in-memory server buffers
- `/maintenance/reset` and automatic reset-on-finished are designed for test/dev workflows.
- Replace the default `PRESHARED_SECRET` before non-local deployments.
