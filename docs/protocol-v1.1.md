# Protocol 1.1 server responsibilities

Package 1.0.1 supports legacy wire value `1.0` and defaults new rounds to wire
value `1.1`. The latter adds `KEY_AGGREGATION` after key setup. The server
commits the canonical signed-ephemeral-key manifest, derives and persists the
balanced group/binary task graph, and returns only authoritative task metadata.

Group starts are ready in parallel. A task output is an opaque AES-256-GCM
ciphertext stored in an immutable slot. The API verifies sender, state, plan and
schema context, digest, size, and Ed25519 signature without learning the tensor
key. A receipt completes the task and activates all newly satisfied dependents
in one SQLite transaction. Odd tree nodes use explicit carry tasks.

After every tree task completes, only the committed root may publish the single
group-encrypted final key. All participants must acknowledge it before the
round enters `UPDATE_COLLECTION`. The worker then performs the same int64-ring
and prime-field additions used for `1.0`; under `1.1` its result remains masked.

All plans, dependencies, task/final artifacts, receipts, and audit events are
durable. Reopening the API at any stage resumes from SQLite. Invalid actors,
early actions, changed replays, malformed or modified artifacts, and context or
signature mismatches durably fail the round with sanitized public errors.

The complete construction, topology formula, cryptographic domains, and threat
model are specified in the sibling client's `docs/protocol-v1.1.md`.
