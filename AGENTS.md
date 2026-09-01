# Agent Guide

## Scope

This file applies to the entire `deki-smpc-server` repository. It guides safe,
repo-consistent changes; `docs/protocol-v1.md`, `docs/api-v1.md`, and the client
protocol specification remain authoritative for deki-smpc v1 behavior.

Keep changes focused and preserve unrelated work. This service coordinates a
security-sensitive, durable state machine: avoid broad refactors that obscure
transaction boundaries, artifact ownership, authentication, or failure modes.

## Repository Map

- `key-aggregation-server/app/api/v1.py`: `/v1` FastAPI routes,
  authentication/authorization, request validation, and HTTP error mapping.
- `key-aggregation-server/app/domain/`: strict request models, stable errors,
  and legal round-state transitions.
- `key-aggregation-server/app/persistence/database.py`: SQLite schema,
  transactions, barriers, idempotency, job leases, audit events, and retention.
- `key-aggregation-server/app/storage/filesystem.py`: size-limited, immutable,
  fsync-backed artifact storage.
- `key-aggregation-server/app/worker/aggregation.py`: durable job claiming,
  artifact revalidation, int64/tag aggregation, publication, and maintenance.
- `key-aggregation-server/app/config.py` and
  `key-aggregation-server/app/main.py`: environment settings and ASGI
  application construction.
- `key-aggregation-server/tests/`: API, state, lease, retention, concurrency,
  participant-limit, canonical-fixture, and cross-repository end-to-end tests.
- `docker-compose.yml` and `docker-compose.tamper.yml`: local service, repeated
  round, and aggregate-modification scenarios.
- `docs/`: API, deployment, operations, protocol responsibilities, and durable
  orchestration ADR.

Treat `build/`, `dist/`, `*.egg-info/`, caches, local SQLite databases, and
artifact directories as generated state. Do not edit or commit them.

## Environment and Quality Gate

Python 3.12 or newer is required. The client and server repositories must be
siblings for the complete test suite:

```text
parent/
├── deki-smpc/
└── deki-smpc-server/
```

Install and validate from the server repository root:

```bash
python -m pip install -e ../deki-smpc
python -m pip install -e '.[test]'
pre-commit run --all-files
ruff check key-aggregation-server
mypy
python -m pytest -q
```

Run the narrowest relevant test first during development, for example:

```bash
python -m pytest -q key-aggregation-server/tests/test_v1_state.py
python -m pytest -q key-aggregation-server/tests/test_v1_e2e.py
python -m pytest -q key-aggregation-server/tests/test_participant_limits.py
```

Build the production image after Dockerfile, packaging, Python-version,
requirements, or runtime changes:

```bash
docker build --pull --tag deki-smpc-server:local key-aggregation-server
```

## Architecture and Coding Conventions

- Keep routes thin: authenticate, validate, authorize, invoke persistence or
  storage operations, and translate failures into sanitized stable errors.
- Keep state changes in explicit database transactions. A barrier transition,
  job creation/claim, idempotency record, and audit event must commit atomically
  when they describe one logical operation.
- Preserve the separation between metadata and artifact bytes. SQLite stores
  durable references and digests; the artifact store owns byte persistence.
- Keep worker execution restart-safe and idempotent. Claim tokens and leases
  define ownership; stale workers must not publish or fail another claim.
- Keep `create_app(settings)` usable in tests. Avoid import-time I/O beyond the
  intentional environment construction of the production `app` object.
- Follow strict mypy settings, Black formatting, Ruff linting, four-space
  indentation, and a 120-character line limit. Use `snake_case` for functions
  and variables,
  `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.
- Pydantic request models remain strict (`extra="forbid"`) and validate bounds
  before persistence or allocation. Keep response/error shapes stable.
- Tests should use `tmp_path`, in-process ASGI clients, deterministic fixtures,
  and explicit settings. Do not depend on real credentials, external services,
  GPUs, machine-specific paths, or shared mutable test state.

## Durability and Protocol Invariants

Preserve these invariants unless an explicit, documented architecture or
versioned protocol change replaces them:

- Legal active-state progression is
  `CREATED -> REGISTRATION_OPEN -> KEY_SETUP -> UPDATE_COLLECTION ->`
  `AGGREGATING -> RESULT_READY -> COMPLETED`. `FAILED`, `ABORTED`, and
  `EXPIRED` are terminal; transitions are transactional and audited.
- Every round commits protocol version, ordered participants, schema and
  manifest hashes, precision, policies, deadline, and retention context.
- Complete-participation barriers advance only after all committed members
  satisfy the phase. The protocol minimum is three participants; an optional
  deployment limit must not become a hidden protocol maximum.
- Every mutation requires a scoped idempotency key. Replaying identical content
  returns the committed response; reusing a key with different content fails
  with `IDEMPOTENCY_CONFLICT`.
- Artifact slots are immutable. Stream uploads enforce the byte limit, hash
  while writing, fsync file contents, atomically rename, fsync the directory,
  and remove partial files on failure.
- The worker revalidates stored digest, tensor names, shapes, int64 dtype, and
  integrity-tag range before aggregation. Tensor addition uses int64 ring
  semantics and tags sum modulo `2**127 - 1`.
- Result publication is atomic with durable job/round state. If database
  publication fails, remove the unreferenced result object.
- Job leases are renewable and reclaimable; exhausted retries durably fail the
  job and round. Deadline reconciliation disables work for expired rounds.
- Retention removes artifact bytes before purging their metadata. Cleanup and
  migrations remain safe to retry after interruption.
- The SQLite/filesystem implementation is a single-durable-host design. Do not
  imply multi-host safety without transactional network storage that preserves
  the repository and artifact-store contracts in ADR 0001.

## Security Boundaries

- Authenticate bearer tokens and authorize federation/round membership before
  returning round data or accepting artifacts. Operator credentials remain
  independent from participant credentials.
- Never log, persist in plaintext beyond required configuration, or return
  bearer tokens, private keys, decrypted shares, or sensitive exception text.
  Token persistence remains one-way hashed.
- Verify Ed25519 signatures against the committed round context and purpose.
  Do not loosen canonicalization, participant matching, schema hashes, digest
  checks, safetensors validation, or artifact size limits.
- The server computes an additive aggregate but does not claim to establish its
  correctness; clients must retain final prime-field integrity verification.
- Use safetensors for model artifacts. Never deserialize untrusted pickle data.
- Production assumes TLS termination and a durable writable volume. Insecure
  development credentials in Compose must stay obviously non-production.
- Keep public errors stable, minimal, and sanitized. Add detailed diagnostics
  only to appropriate internal audit/operation channels without secrets.

Changes to auth, signatures, uploads, state transitions, leases, cleanup, or
aggregation require failure-path, replay, authorization, boundary, and
concurrency tests as applicable.

## Cross-Repository and Protocol Changes

The sibling `../deki-smpc` repository owns the client implementation and the
normative canonical fixture at `../deki-smpc/tests/fixtures/protocol-v1.json`.
A protocol/API change is incomplete until these remain aligned:

- server request/response models and client transport/models;
- server API/protocol docs and client protocol/wire/security docs;
- canonical fixture and protocol tests in both repositories;
- stable server error codes and typed client exception mapping;
- `CHANGELOG.md` in each affected repository.

Do not change the meaning of protocol wire value `1.0` incompatibly. Use an
explicit new protocol version for breaking wire changes. Run both repositories'
quality gates for shared-contract changes.

For high-risk protocol, worker, persistence, or container changes, run the
named Compose end-to-end project and inspect verifier logs:

```bash
docker compose --profile e2e -p deki_v1_e2e up --build -d
test "$(docker wait deki_v1_e2e-verify-1)" = "0"
docker compose --profile e2e -p deki_v1_e2e logs verify
docker compose --profile e2e -p deki_v1_e2e down --volumes --remove-orphans
```

Also run the tamper overlay when aggregate integrity, artifact handling, or
worker behavior changes. Use only the explicitly named test project when
cleaning containers or volumes; do not remove unrelated Docker state.

## Documentation and Release Hygiene

- Update `docs/api-v1.md` for route, payload, header, status, or error changes.
- Update deployment/operations docs for settings, health, persistence,
  resource, recovery, or lifecycle changes. Record architectural changes in an
  ADR rather than silently invalidating ADR 0001.
- Add user-visible changes under `Unreleased` in `CHANGELOG.md`.
- Keep `pyproject.toml` ranges and `key-aggregation-server/requirements.txt`
  production pins compatible. Keep Python support synchronized across package
  metadata, Ruff, mypy, CI, Docker, README, and docs.
- Never commit real tokens, signing keys, federation manifests, databases,
  model artifacts, logs, or generated build output.
