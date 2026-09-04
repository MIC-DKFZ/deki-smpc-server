# Deployment

This guide describes the supplied single-host deployment for deki-smpc server
v1.

## Topology

A deployment consists of:

- one HTTPS ingress;
- one or more Uvicorn API processes on one host;
- one or more aggregation-worker processes on the same host;
- one local SQLite database on durable storage;
- one local artifact directory on durable storage.

All processes use the same environment configuration and shared data paths.
The filesystem must provide local POSIX locking, atomic rename, and fsync
semantics.

## Configuration

The application reads configuration from environment variables during startup.

| Variable | Description | Default |
| --- | --- | --- |
| `DEKI_ADMIN_TOKEN` | Operator bearer token with at least 32 characters | Required |
| `DEKI_FEDERATIONS_JSON` | Federation member, token, and signing-key map | `{}` |
| `DEKI_DATABASE_PATH` | SQLite metadata path | `/data/metadata.sqlite3` |
| `DEKI_ARTIFACT_PATH` | Immutable artifact directory | `/data/artifacts` |
| `DEKI_MAX_ARTIFACT_BYTES` | Maximum update or result size | `536870912` |
| `DEKI_MAX_PARTICIPANTS` | Optional maximum participants per round | No limit |
| `DEKI_ROUND_TTL_SECONDS` | Default round deadline | `1800` |
| `DEKI_RETENTION_SECONDS` | Terminal round retention interval | `86400` |
| `DEKI_WORKER_POLL_SECONDS` | Worker delay when the queue is empty | `0.25` |
| `DEKI_WORKER_LEASE_SECONDS` | Aggregation claim duration | `900` |
| `DEKI_MAINTENANCE_INTERVAL_SECONDS` | Deadline and retention sweep interval | `60` |

All numeric settings must be positive. When set, `DEKI_MAX_PARTICIPANTS` must
be at least 3. Round creation accepts an explicit deadline from 5 to 86,400
seconds.

## Federation enrollment

`DEKI_FEDERATIONS_JSON` maps federation IDs to participant records:

```json
{
  "hospital-network": {
    "site-a": {
      "token": "high-entropy-participant-token",
      "signing_public_key": "base64-encoded-raw-Ed25519-public-key",
      "role": "participant"
    },
    "site-b": {
      "token": "another-high-entropy-participant-token",
      "signing_public_key": "base64-encoded-raw-Ed25519-public-key",
      "role": "participant"
    }
  }
}
```

Participant IDs use letters, digits, `.`, `_`, and `-`, with a maximum length
of 128 characters. Tokens contain at least 32 characters. Signing keys are
padded Base64 encodings of raw 32-byte Ed25519 public keys.

The same complete public-key map is distributed to every participant through a
trusted administrative channel. Private keys and participant tokens are stored
in the corresponding site's secret-management system.

## Process commands

The API process runs:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 2
```

The worker process runs:

```bash
python -m app.worker.aggregation
```

Start at least one worker with every API deployment. The worker owns job
execution, deadline reconciliation, and retention cleanup.

## Container image

Build the server image from the repository root:

```bash
docker build -t deki-smpc-server:v1 key-aggregation-server
```

Mount a durable volume at `/data`, inject configuration through the container
runtime, and run the image's default API command. Start a second container from
the same image with `python -m app.worker.aggregation` as its command and the
same data volume.

The image runs as the unprivileged `deki` user. Its root filesystem supports a
read-only mount when `/data` and `/tmp` remain writable mounts.

## TLS and network policy

Terminate TLS at the ingress and forward traffic to the API over a private
service network. Use a certificate chain trusted by all participant sites.
Mutual TLS can provide an additional deployment-level identity layer.

Expose the HTTPS API to enrolled participant networks. Keep the worker and data
volume on the service host. Apply request-size limits at the ingress at or above
`DEKI_MAX_ARTIFACT_BYTES` so valid update uploads can stream to the application.

## Resource planning

An encoded model artifact contains eight bytes per uploaded element plus
safetensors metadata. During aggregation, the worker holds the accumulated
int64 tensors and one input artifact in memory. Set memory limits from the
largest committed model and validate them with representative end-to-end
rounds.

Disk capacity covers participant updates, one result, SQLite metadata, and
temporary streaming files for all concurrent rounds within the retention
window. Protocol `1.1` also retains one encrypted model-sized artifact per
group-ring and tree task plus one final-key artifact. For `n` participants and
`g = ceil(n/4)` groups (except the five-site case), budget roughly
`n + 2g` encrypted key artifacts (including final distribution) in addition to
updates and result. Set the
retention interval from storage capacity and audit requirements.

## Rolling deployment

Deploy server 1.0.1 first while operators explicitly create `1.0` rounds.
Active `1.0` rounds retain their direct setup-to-update transition across the
deployment. Upgrade every participant client next, validate both protocol
values, then omit `protocol_version` or request `1.1` to enable the new default.
Rollback must not remove the additive 1.1 SQLite tables while 1.1 rounds or
their retention records exist.

## Health checks

`GET /health/live` reports process liveness. `GET /health/ready` opens the
database and executes a query. Use readiness to control ingress traffic and
service startup ordering.

The worker is a foreground process and responds to `SIGTERM` and `SIGINT` after
its current `run_once()` call. Configure a shutdown grace period that covers
aggregation of the largest supported model.

## Deployment boundary

The supplied SQLite and filesystem persistence supports one durable host.
Multi-host API or worker placement uses implementations backed by a
transactional network database and shared object storage. Those implementations
preserve the database repository and artifact-store semantics described in
[ADR 0001](adr/0001-durable-rounds.md).
