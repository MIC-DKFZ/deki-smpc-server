# Operations

## Service startup

Start the API and wait for readiness before starting federation rounds. Start
the aggregation worker with the same configuration and data volume.

```bash
curl --fail https://aggregation.example.org/health/live
curl --fail https://aggregation.example.org/health/ready
```

Application startup creates the SQLite schema and synchronizes configured
federation members. Concurrent API startup uses SQLite locking and bounded
initialization retries.

## Round lifecycle

The operator creates a round through `POST
/v1/federations/{federation_id}/rounds`. Participants can query their round and
participant state throughout execution.

Active states progress through registration, key setup, update collection,
aggregation, result publication, and completion. Terminal outcomes are:

| State | Meaning | Operator action |
| --- | --- | --- |
| `COMPLETED` | Every participant verified the result | Record the round ID with the training run |
| `FAILED` | Validation or aggregation failed | Inspect the failure code and start a fresh round after correction |
| `ABORTED` | Operator ended the round | Record the supplied abort reason |
| `EXPIRED` | The committed deadline elapsed | Confirm participant availability and create a fresh round |

Round identifiers and state changes form the operational correlation keys.
Participant clients expose stable exception types and the associated
`round_id`.

Protocol `1.1` adds the `KEY_AGGREGATION` active state. Its audit trail includes
`TREE_PLAN_COMMITTED`, `TREE_ARTIFACT_ACCEPTED`, `TREE_TASK_COMPLETED`,
`FINAL_KEY_PUBLISHED`, `FINAL_KEY_ACKNOWLEDGED`, and `FINAL_KEY_BARRIER`.
Restarted API processes resume from durable task and receipt rows; operators do
not manually advance tasks. A rejected or missing contribution requires a new
round because the protocol intentionally has no dropout recovery.

## Worker leases

The worker claims each aggregation job with a unique token and an expiry time.
It renews the lease while validating and adding artifacts. A process exit leaves
the claim available for recovery after the lease expires.

Recovered jobs consume another attempt. The third exhausted attempt records a
terminal job failure and moves the round to `FAILED`. Set
`DEKI_WORKER_LEASE_SECONDS` above the expected interval between lease renewals
for the largest model.

## Deadlines and retention

The worker maintenance loop performs two tasks:

1. It moves active rounds beyond their deadline to `EXPIRED` and closes related
   jobs.
2. It removes artifacts and database records after the terminal round's
   retention time.

`DEKI_MAINTENANCE_INTERVAL_SECONDS` controls reconciliation frequency.
`DEKI_RETENTION_SECONDS` controls the interval between terminal state and
purge.

## Backup

Treat the SQLite database and artifact directory as one data set.

For a simple consistent backup:

1. Stop the API and worker processes.
2. Copy the SQLite database, its WAL files when present, and the complete
   artifact directory.
3. Verify checksums and store the backup on protected durable storage.
4. Restart the API and worker and verify `/health/ready`.

For restoration, stop both process roles, restore the complete data set to the
configured paths, preserve ownership for the `deki` user, and start the API
followed by the worker.

Online backup procedures use the SQLite backup API and coordinate artifact
snapshots at the same consistency point.

## Capacity monitoring

Monitor:

- API liveness and readiness;
- API and worker process restarts;
- durable-volume free space and inode availability;
- SQLite database and WAL size;
- artifact-directory size;
- round terminal states and failure codes;
- worker CPU, memory, and aggregation duration;
- counts of lease recovery and exhausted jobs.

Storage alerts should allow enough time for active rounds to finish within the
configured artifact and retention limits.

## Credential and identity changes

Configured federation records are upserted at process startup. Coordinate token
or Ed25519 identity changes across the server secret configuration and the
trusted manifests at all participant sites. Create subsequent rounds after all
sites hold the same manifest.

Use a new operator token through the deployment secret mechanism and restart the
API processes together. Store all credentials in a secret manager and restrict
their visibility to the corresponding process or participant.

## Incident handling

For an aggregate integrity failure:

1. Preserve the round identifier and client exception details.
2. Stop further use of the round result.
3. Preserve server metadata and artifacts according to incident policy.
4. Review service-host integrity, worker image provenance, ingress behavior,
   and participant manifests.
5. Rotate affected credentials or images and create a fresh round.

For a storage-integrity event, stop both process roles and validate the SQLite
database and artifact checksums from a trusted backup before service recovery.
