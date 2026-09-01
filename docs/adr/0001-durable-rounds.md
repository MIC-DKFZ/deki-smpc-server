# ADR 0001: Durable round orchestration

Status: Accepted for v1

## Context

A secure aggregation round spans several authenticated barriers and may run for
minutes or hours. API and worker processes require a common state after process
restart, concurrent requests, and worker lease expiry. Model artifacts can be
hundreds of megabytes and require streaming validation and bounded storage.

## Decision

The v1 server uses:

- a transactional round state machine as the authority for protocol progress;
- SQLite in write-ahead-log mode for metadata, barriers, jobs, idempotency
  records, and audit events;
- immutable filesystem objects for model updates and results;
- fsync before publication of artifact metadata;
- separate API and aggregation-worker processes;
- expiring worker leases with claim tokens and bounded attempts;
- one deadline and one retention time per round.

The SQLite database and artifact directory reside on the same durable host and
are shared by all v1 API and worker processes.

## Consequences

Round progress survives process restarts. Mutating requests are idempotent,
artifact slots are immutable, and one claimed job publishes one aggregate.

The supplied persistence implementation defines a single-host deployment
topology. A multi-host topology requires repository and artifact-store
implementations backed by a transactional network database and shared object
storage. The protocol service boundary permits those implementations while
preserving the v1 API and round model.

Backup and restore treat the SQLite database and artifact directory as one
consistent data set. Operational procedures are documented in
[Operations](../operations.md).
