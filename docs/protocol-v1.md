# Protocol v1 server responsibilities

This document defines the legacy server path for wire value `1.0`, preserved in
package release 1.0.1. New rounds default to [protocol 1.1](protocol-v1.1.md).

## Round authority

The operator creates a round with a fixed federation, participant list, model
schema, aggregation policy, fixed-point precision, and deadline. The server
canonicalizes the participant manifest from enrolled federation members and
commits its SHA-256 hash.

All subsequent requests are scoped to the round identifier and authenticated
participant identity. The durable state machine is:

```text
CREATED
  -> REGISTRATION_OPEN
  -> KEY_SETUP
  -> UPDATE_COLLECTION
  -> AGGREGATING
  -> RESULT_READY
  -> COMPLETED
```

`FAILED`, `ABORTED`, and `EXPIRED` are terminal states. State transitions and
participant barriers are committed in SQLite transactions.

## Key-setup relay

The API verifies Ed25519 signatures on each participant's ephemeral X25519
public key and encrypted key bundle. It verifies that bundle recipients equal
the committed participant set. Public keys and encrypted recipient envelopes
are available to round participants during key setup.

Participants submit a commitment after decrypting their incoming shares. A
single shared commitment advances the round to update collection.

## Update collection

The API streams each update to the artifact store under a configured byte
limit. It verifies:

- participant membership and round phase;
- model schema hash;
- safetensors structure;
- exact tensor names and shapes;
- signed int64 encoded dtype;
- prime-field integrity-tag encoding;
- immutable artifact slot and idempotency record.

The final accepted update creates one durable aggregation job.

## Aggregation worker

The worker claims a job with an expiring lease and unique claim token. During
aggregation it renews the lease and verifies each artifact's SHA-256 digest,
safetensors structure, and committed schema.

For every uploaded tensor, the worker performs signed int64 addition. This is
the protocol's two's-complement ring addition. It sums the integrity tags
modulo `2**127 - 1`.

The result file is fsynced before a transaction records its artifact metadata,
aggregate tag, completed job, and `RESULT_READY` transition. Client
acknowledgements advance the round to `COMPLETED`.

## Recovery and retention

An expired worker lease returns a job to the ready state while retry attempts
remain. Exhausted attempts move the job and round to `FAILED`.

The worker periodically expires active rounds whose deadline elapsed. It also
removes artifacts and metadata after the terminal round's retention time.

## Information handled by the service

The server stores:

- federation and round metadata;
- participant public identity keys and bearer-token digests;
- signed ephemeral public keys;
- encrypted share envelopes;
- masked individual updates and padded integrity tags;
- aggregated encoded tensors and aggregate tag;
- state-transition audit records.

Client processes retain private identity keys, X25519 private keys, decrypted
shares, pairwise mask keys, the common integrity seed, and the aggregate field
pad.

The precise field encodings and endpoint contract are documented in
[API v1](api-v1.md) and in the client repository's v1 protocol specification.
