# API v1

The deki-smpc server exposes a JSON and safetensors HTTP API under `/v1`.
Interactive OpenAPI documentation is served at `/docs`.

## Authentication

All v1 requests use bearer authentication:

```http
Authorization: Bearer <credential>
```

Round creation and abort use the operator credential from `DEKI_ADMIN_TOKEN`.
Participant resources use a token enrolled in `DEKI_FEDERATIONS_JSON`. The
server derives federation and participant identity from that credential.

Every `POST` and `PUT` request carries:

```http
Idempotency-Key: <operation-specific-value>
```

The maximum key length is 128 characters. Repeating a key with identical
content returns the committed response and identifies a replay where the
response model provides that field.

## Create a round

```http
POST /v1/federations/{federation_id}/rounds
Content-Type: application/json
Authorization: Bearer <operator-token>
Idempotency-Key: <key>
```

```json
{
  "protocol_version": "1.0",
  "model_schema": {
    "aggregation_policy": "EQUAL_WEIGHTED",
    "entries": [
      {
        "dtype": "float32",
        "name": "linear.bias",
        "policy": "MEAN",
        "shape": [1]
      },
      {
        "dtype": "float32",
        "name": "linear.weight",
        "policy": "MEAN",
        "shape": [1, 2]
      }
    ],
    "precision_bits": 24
  },
  "model_schema_hash": "<sha256-of-canonical-model-schema>",
  "participants": ["site-a", "site-b", "site-c"],
  "deadline_seconds": 1800
}
```

The entries are in lexical name order. `model_schema_hash` is SHA-256 of the
canonical JSON encoding of `model_schema`. A round contains at least three
unique enrolled members of the selected federation. Deployments may set an
optional participant ceiling with `DEKI_MAX_PARTICIPANTS`.

The response contains `round_id`, the committed participant-manifest hash,
state, deadline, and idempotency replay status.

## Inspect round state

```http
GET /v1/rounds/{round_id}
GET /v1/rounds/{round_id}/participants/me
```

The first resource returns protocol version, federation, state, state version,
schema hash, manifest hash, participant count, aggregation policy, precision,
deadline, retry guidance, and terminal failure fields. The second returns the
caller's participant state and completion status.

## Key setup

| Method | Path | Purpose |
| --- | --- | --- |
| `PUT` | `/v1/rounds/{round_id}/artifacts/public_key` | Store a signed ephemeral X25519 public key |
| `GET` | `/v1/rounds/{round_id}/artifacts/public_keys` | Read the complete signed public-key set |
| `PUT` | `/v1/rounds/{round_id}/artifacts/key_bundle` | Store signed encrypted envelopes for every peer |
| `GET` | `/v1/rounds/{round_id}/artifacts/key_bundle` | Read encrypted envelopes addressed to the caller |
| `POST` | `/v1/rounds/{round_id}/key-setup/complete` | Commit the reconstructed key context |

Public-key artifacts contain a padded Base64 raw X25519 key and a padded Base64
Ed25519 signature. Key bundles map each peer identifier to an AES-GCM nonce and
ciphertext, with one signature over the complete message map.

A pending incoming bundle response uses HTTP 204 and `Retry-After: 1`.

## Upload an update

```http
PUT /v1/rounds/{round_id}/artifacts/update
Content-Type: application/vnd.safetensors
X-Model-Schema-Hash: <sha256>
X-Integrity-Tag: <32-lowercase-hex-characters>
Idempotency-Key: <key>
```

The body contains the masked signed int64 tensors selected by `MEAN` or `SUM`.
The integrity tag is a big-endian field element in `2**127 - 1`. The server
streams the body under `DEKI_MAX_ARTIFACT_BYTES` and validates it against the
committed schema.

## Download and acknowledge a result

```http
GET /v1/rounds/{round_id}/artifacts/result
```

The response body is an int64 safetensors artifact. Response headers contain
`X-Integrity-Tag`, `X-Content-SHA256`, and `Cache-Control: no-store`.

After local verification, the participant acknowledges the result:

```http
POST /v1/rounds/{round_id}/complete
Content-Type: application/json
Idempotency-Key: <key>

{}
```

The completion barrier moves the round to `COMPLETED` after every participant
acknowledges the result.

## Abort a round

```http
POST /v1/rounds/{round_id}/abort
Content-Type: application/json
Authorization: Bearer <operator-token>
Idempotency-Key: <key>

{"reason": "OPERATOR_ABORT"}
```

The reason has 1 to 128 characters and is stored with the terminal audit event.

## Error responses

Protocol errors have this shape:

```json
{
  "detail": {
    "code": "IDEMPOTENCY_CONFLICT",
    "message": "idempotency key was reused with different content",
    "round_id": "c15ff8be-10a2-4b78-aad8-3c292ac630df"
  }
}
```

Stable codes include authentication, authorization, protocol-version, round,
idempotency, schema, participant-limit, and artifact failures. Messages contain
sanitized diagnostic text. The exact serialization rules are specified in the
client repository's `docs/wire-format-v1.md`.
