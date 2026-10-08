# Changelog

This file records released changes to `deki-smpc-server`.

## Unreleased

### Documentation

- Added a README citation section for the published IEEE JBHI article
  (doi:10.1109/JBHI.2026.3740976).

## [1.0.1] - 2026-09-04

### Protocol and API

- Added protocol `1.1` and made it the round-creation default while preserving
  explicit `1.0` rounds and their state path unchanged.
- Added `KEY_AGGREGATION`, canonical tree-plan retrieval, authorized next-action
  polling, immutable encrypted task artifacts and receipts, and final-key
  distribution and acknowledgement resources.
- Kept worker int64 and prime-field addition unchanged; its protocol-`1.1`
  result is explicitly still masked and cannot be cleared by the service.

### Durability and security

- Added repeatable SQLite tables for tree plans, tasks, dependencies, receipts,
  encrypted artifact references, and final-key receipts. Task completion and
  dependent activation commit atomically and survive API restarts.
- Enforced sender/receiver authorization, task readiness, immutable slots,
  stable idempotency, size and digest limits, signed AEAD contexts, deadlines,
  retention, and durable round failure for rejected key artifacts.

### Validation and documentation

- Added shared `1.1` fixture validation, restart/task-order tests, wrong-actor
  rejection, and complete 3/5/7/12-participant client/server rounds proving the
  server result differs from the clear aggregate.

### Fixed

- Failed a round durably when participants commit different key contexts.
  The transition was raised from inside its own transaction, so the FAILED
  state and its audit event were rolled back and the round instead timed
  out under a generic deadline code with no record of the divergence.

### Security

- Stopped shipping the `e2e` tamper fixture in the server image. The
  tampering worker is now bind-mounted only by the tamper compose overlay.
- Replaced the container health check, which only probed for PID 1 and so
  reported every running container healthy, with a real `/health/ready`
  probe. The worker runs no HTTP server and disables it explicitly.

### Changed

- Standardized product naming as `deki-smpc` throughout the repository.
- Reworked the README around a high-level introduction, visual overview, and streamlined getting-started flow.
- Raised the minimum supported Python version to 3.12.
- Made Black the authoritative formatter, with isort running before it through pre-commit.

## [1.0.0] - 2026-08-30

Initial deki-smpc server v1 release.

### API and protocol

- Added authenticated, round-scoped resources under `/v1`.
- Added strict model-schema, manifest, artifact, and idempotency validation.
- Added signed ephemeral key and encrypted share-bundle relay.
- Added safetensors update and result transport.
- Added additive int64 aggregation and prime-field integrity-tag aggregation.

### Persistence and operation

- Added transactional SQLite round state, barriers, jobs, audit events, and
  idempotency records.
- Added immutable fsync-backed filesystem artifacts.
- Added aggregation-worker claim tokens, leases, recovery attempts, and atomic
  result publication.
- Added deadline reconciliation and retention cleanup.
- Added liveness and readiness endpoints.

### Validation

- Added API, state-machine, concurrent startup, lease, and retention tests.
- Added three-participant cross-repository end-to-end tests.
- Added a real `PlainConvUNet` aggregation test.
- Added five-round container validation and an aggregate-modification scenario.
