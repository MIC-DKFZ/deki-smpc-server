"""SQLite transactional store for local and single-site deployments.

The service layer uses only this repository API, allowing a PostgreSQL backend
to be substituted without changing routes or protocol code.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from app.domain.states import RoundState, transition_allowed
from app.domain.topology import derive_tree_plan, tree_plan_hash
from app.storage.filesystem import StoredObject

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS members (
 federation_id TEXT NOT NULL, client_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 signing_public_key TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'participant', revoked INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(federation_id, client_id));
CREATE TABLE IF NOT EXISTS rounds (
 round_id TEXT PRIMARY KEY, federation_id TEXT NOT NULL, protocol_version TEXT NOT NULL,
 model_schema_hash TEXT NOT NULL, model_schema_json TEXT NOT NULL, participant_manifest_hash TEXT NOT NULL,
 participants_json TEXT NOT NULL, expected_count INTEGER NOT NULL, aggregation_policy TEXT NOT NULL,
 precision_bits INTEGER NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL,
 created_at REAL NOT NULL, deadline_at REAL NOT NULL, completed_at REAL, retention_at REAL NOT NULL,
 failure_code TEXT, failure_detail TEXT);
CREATE TABLE IF NOT EXISTS participants (
 round_id TEXT NOT NULL, client_id TEXT NOT NULL, state TEXT NOT NULL,
 public_key_json TEXT, key_bundle_json TEXT, context_commitment TEXT,
 update_artifact_id TEXT, update_digest TEXT, integrity_tag TEXT,
 completed INTEGER NOT NULL DEFAULT 0, last_seen REAL NOT NULL,
 PRIMARY KEY(round_id, client_id), FOREIGN KEY(round_id) REFERENCES rounds(round_id));
CREATE TABLE IF NOT EXISTS artifacts (
 artifact_id TEXT PRIMARY KEY, round_id TEXT NOT NULL, client_id TEXT, artifact_type TEXT NOT NULL,
 path TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL,
 UNIQUE(round_id, client_id, artifact_type), FOREIGN KEY(round_id) REFERENCES rounds(round_id));
CREATE TABLE IF NOT EXISTS idempotency (
 client_id TEXT NOT NULL, round_id TEXT NOT NULL, operation TEXT NOT NULL, idem_key TEXT NOT NULL,
 content_digest TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(client_id, round_id, operation, idem_key));
CREATE TABLE IF NOT EXISTS jobs (
 job_id TEXT PRIMARY KEY, round_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3, claimed_at REAL, lease_until REAL, claim_token TEXT,
 result_integrity_tag TEXT, last_error TEXT,
 FOREIGN KEY(round_id) REFERENCES rounds(round_id));
CREATE TABLE IF NOT EXISTS audit_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT, actor TEXT NOT NULL,
 event_type TEXT NOT NULL, state_version INTEGER, created_at REAL NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS tree_plans (
 round_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, plan_json TEXT NOT NULL, manifest_json TEXT NOT NULL,
 created_at REAL NOT NULL, FOREIGN KEY(round_id) REFERENCES rounds(round_id));
CREATE TABLE IF NOT EXISTS key_tasks (
 round_id TEXT NOT NULL, task_id TEXT NOT NULL, action TEXT NOT NULL, stage TEXT NOT NULL, level INTEGER NOT NULL,
 sender TEXT NOT NULL, receiver TEXT NOT NULL, dependencies_json TEXT NOT NULL, state TEXT NOT NULL,
 artifact_id TEXT, ciphertext_digest TEXT, nonce TEXT, signature TEXT, completed_at REAL,
 PRIMARY KEY(round_id,task_id), FOREIGN KEY(round_id) REFERENCES rounds(round_id),
 FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id));
CREATE TABLE IF NOT EXISTS task_dependencies (
 round_id TEXT NOT NULL, task_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
 PRIMARY KEY(round_id,task_id,dependency_id),
 FOREIGN KEY(round_id,task_id) REFERENCES key_tasks(round_id,task_id),
 FOREIGN KEY(round_id,dependency_id) REFERENCES key_tasks(round_id,task_id));
CREATE TABLE IF NOT EXISTS task_receipts (
 round_id TEXT NOT NULL, task_id TEXT NOT NULL, actor TEXT NOT NULL, artifact_digest TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(round_id,task_id),
 FOREIGN KEY(round_id,task_id) REFERENCES key_tasks(round_id,task_id));
CREATE TABLE IF NOT EXISTS final_keys (
 round_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, ciphertext_digest TEXT NOT NULL,
 nonce TEXT NOT NULL, signature TEXT NOT NULL, sender TEXT NOT NULL, created_at REAL NOT NULL,
 FOREIGN KEY(round_id) REFERENCES rounds(round_id), FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id));
CREATE TABLE IF NOT EXISTS final_key_receipts (
 round_id TEXT NOT NULL, client_id TEXT NOT NULL, artifact_digest TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(round_id,client_id), FOREIGN KEY(round_id) REFERENCES rounds(round_id));
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_artifacts_round ON artifacts(round_id);
CREATE INDEX IF NOT EXISTS idx_key_tasks_ready ON key_tasks(round_id,state,sender);
"""


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self, federations: dict[str, dict[str, dict[str, str]]]) -> None:
        for attempt in range(20):
            try:
                self._initialize_once(federations)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
                time.sleep(min(0.05 * 2**attempt, 1.0))

    def _initialize_once(self, federations: dict[str, dict[str, dict[str, str]]]) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            # CREATE TABLE does not add columns to databases created by an
            # earlier v1 build. Keep this small migration safe to repeat.
            job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            for name, declaration in {
                "lease_until": "REAL",
                "claim_token": "TEXT",
                "result_integrity_tag": "TEXT",
            }.items():
                if name not in job_columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            for federation_id, members in federations.items():
                for client_id, config in members.items():
                    token = config.get("token", "")
                    signing_key = config.get("signing_public_key", "")
                    try:
                        signing_key_bytes = base64.b64decode(signing_key, validate=True)
                    except Exception as exc:
                        raise RuntimeError("member signing_public_key is invalid") from exc
                    if len(token) < 32 or len(signing_key_bytes) != 32:
                        raise RuntimeError(
                            "every configured member needs a high-entropy token and raw Ed25519 public key"
                        )
                    connection.execute(
                        "INSERT INTO members VALUES(?,?,?,?,?,0) ON CONFLICT(federation_id,client_id) DO UPDATE SET token_hash=excluded.token_hash,signing_public_key=excluded.signing_public_key,role=excluded.role",
                        (
                            federation_id,
                            client_id,
                            hashlib.sha256(token.encode()).hexdigest(),
                            signing_key,
                            config.get("role", "participant"),
                        ),
                    )

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def member_for_token(self, token: str) -> dict[str, object] | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM members WHERE token_hash=? AND revoked=0", (digest,)).fetchone()
            return dict(row) if row else None

    def member(self, federation_id: str, client_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM members WHERE federation_id=? AND client_id=? AND revoked=0", (federation_id, client_id)
            ).fetchone()
            return dict(row) if row else None

    def create_round(
        self,
        federation_id: str,
        protocol_version: str,
        schema: dict[str, object],
        schema_hash: str,
        participants: list[str],
        manifest_hash: str,
        ttl: int,
        retention: int,
        actor: str,
        idem_key: str,
        request_digest: str,
    ) -> tuple[dict[str, object], bool]:
        now = time.time()
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT response_json,content_digest FROM idempotency WHERE client_id=? AND round_id=? AND operation='create' AND idem_key=?",
                (actor, federation_id, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != request_digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return json.loads(prior["response_json"]), True
            round_id = str(uuid.uuid4())
            response = {
                "round_id": round_id,
                "federation_id": federation_id,
                "state": RoundState.REGISTRATION_OPEN.value,
                "state_version": 1,
                "protocol_version": protocol_version,
                "model_schema_hash": schema_hash,
                "participant_manifest_hash": manifest_hash,
                "expected_participant_count": len(participants),
                "aggregation_policy": schema["aggregation_policy"],
                "precision_bits": schema["precision_bits"],
                "deadline_at": now + ttl,
            }
            connection.execute(
                "INSERT INTO rounds VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    round_id,
                    federation_id,
                    protocol_version,
                    schema_hash,
                    canonical_json(schema),
                    manifest_hash,
                    canonical_json(participants),
                    len(participants),
                    schema["aggregation_policy"],
                    schema["precision_bits"],
                    RoundState.REGISTRATION_OPEN.value,
                    1,
                    now,
                    now + ttl,
                    None,
                    now + retention,
                    None,
                    None,
                ),
            )
            connection.executemany(
                "INSERT INTO participants(round_id,client_id,state,last_seen) VALUES(?,?,?,?)",
                [(round_id, client, "AWAITING_PUBLIC_KEY", now) for client in participants],
            )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,state_version,created_at) VALUES(?,?,?,?,?)",
                (round_id, actor, "ROUND_CREATED", 1, now),
            )
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (actor, federation_id, "create", idem_key, request_digest, canonical_json(response), now),
            )
            return response, False

    def get_round(self, round_id: str, expire: bool = True) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
        state = RoundState(data["state"])
        if (
            not expire
            or state in {RoundState.COMPLETED, RoundState.FAILED, RoundState.ABORTED, RoundState.EXPIRED}
            or time.time() <= data["deadline_at"]
        ):
            return data
        with self.transaction() as connection:
            current = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not current:
                return None
            current_state = RoundState(current["state"])
            if (
                current_state not in {RoundState.COMPLETED, RoundState.FAILED, RoundState.ABORTED, RoundState.EXPIRED}
                and time.time() > current["deadline_at"]
            ):
                self._transition(
                    connection,
                    round_id,
                    current_state,
                    RoundState.EXPIRED,
                    "reconciler",
                    "ROUND_DEADLINE_EXCEEDED",
                )
            return dict(connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone())

    @staticmethod
    def _transition(
        connection: sqlite3.Connection,
        round_id: str,
        source: RoundState,
        target: RoundState,
        actor: str,
        event: str,
        failure_detail: str | None = None,
    ) -> None:
        if not transition_allowed(source, target):
            raise ValueError(f"ILLEGAL_TRANSITION:{source}:{target}")
        now = time.time()
        result = connection.execute(
            "UPDATE rounds SET state=?,state_version=state_version+1,"
            "completed_at=CASE WHEN ? IN ('COMPLETED','FAILED','ABORTED','EXPIRED') THEN ? ELSE completed_at END,"
            "retention_at=CASE WHEN ? IN ('COMPLETED','FAILED','ABORTED','EXPIRED') "
            "THEN ?+(retention_at-created_at) ELSE retention_at END,"
            "failure_code=CASE WHEN ? IN ('FAILED','ABORTED','EXPIRED') THEN ? ELSE failure_code END,"
            "failure_detail=CASE WHEN ? IN ('FAILED','ABORTED','EXPIRED') THEN ? ELSE failure_detail END "
            "WHERE round_id=? AND state=?",
            (
                target.value,
                target.value,
                now,
                target.value,
                now,
                target.value,
                event,
                target.value,
                failure_detail,
                round_id,
                source.value,
            ),
        )
        if result.rowcount != 1:
            raise ValueError("ROUND_CONFLICT")
        version = connection.execute("SELECT state_version FROM rounds WHERE round_id=?", (round_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO audit_events(round_id,actor,event_type,state_version,created_at,detail) VALUES(?,?,?,?,?,?)",
            (round_id, actor, event, version, now, failure_detail),
        )

    def participant(self, round_id: str, client_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM participants WHERE round_id=? AND client_id=?", (round_id, client_id)
            ).fetchone()
            return dict(row) if row else None

    def record_json_slot(
        self, round_id: str, client_id: str, slot: str, body: dict[str, object], idem_key: str, content_digest: str
    ) -> tuple[dict[str, object], bool]:
        column = {"public_key": "public_key_json", "key_bundle": "key_bundle_json"}[slot]
        now = time.time()
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? AND operation=? AND idem_key=?",
                (client_id, round_id, slot, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != content_digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return json.loads(prior["response_json"]), True
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not round_row:
                raise ValueError("ROUND_NOT_FOUND")
            required = RoundState.REGISTRATION_OPEN if slot == "public_key" else RoundState.KEY_SETUP
            if round_row["state"] not in {
                required.value,
                RoundState.KEY_SETUP.value if slot == "public_key" else required.value,
            }:
                raise ValueError("ROUND_CONFLICT")
            participant = connection.execute(
                "SELECT * FROM participants WHERE round_id=? AND client_id=?", (round_id, client_id)
            ).fetchone()
            encoded = canonical_json(body)
            if participant[column] and participant[column] != encoded:
                raise ValueError("ROUND_CONFLICT")
            connection.execute(
                f"UPDATE participants SET {column}=?,state=?,last_seen=? WHERE round_id=? AND client_id=?",
                (
                    encoded,
                    "PUBLIC_KEY_SUBMITTED" if slot == "public_key" else "KEY_BUNDLE_SUBMITTED",
                    now,
                    round_id,
                    client_id,
                ),
            )
            response = {"accepted": True, "digest": content_digest}
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (client_id, round_id, slot, idem_key, content_digest, canonical_json(response), now),
            )
            count = connection.execute(
                f"SELECT count(*) FROM participants WHERE round_id=? AND {column} IS NOT NULL", (round_id,)
            ).fetchone()[0]
            if (
                slot == "public_key"
                and count == round_row["expected_count"]
                and round_row["state"] == RoundState.REGISTRATION_OPEN.value
            ):
                self._transition(
                    connection,
                    round_id,
                    RoundState.REGISTRATION_OPEN,
                    RoundState.KEY_SETUP,
                    client_id,
                    "PUBLIC_KEY_BARRIER",
                )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,created_at) VALUES(?,?,?,?)",
                (round_id, client_id, f"{slot.upper()}_ACCEPTED", now),
            )
            return response, False

    def public_keys(self, round_id: str) -> dict[str, object]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT client_id,public_key_json FROM participants WHERE round_id=?", (round_id,)
            ).fetchall()
            if not rows or any(row["public_key_json"] is None for row in rows):
                raise ValueError("NOT_READY")
            return {row["client_id"]: json.loads(row["public_key_json"]) for row in rows}

    def incoming_bundles(self, round_id: str, recipient: str) -> dict[str, object] | None:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT client_id,key_bundle_json FROM participants WHERE round_id=?", (round_id,)
            ).fetchall()
            if not rows or any(row["key_bundle_json"] is None for row in rows):
                return None
            result = {}
            for row in rows:
                if row["client_id"] == recipient:
                    continue
                bundle = json.loads(row["key_bundle_json"])
                if recipient not in bundle["messages"]:
                    raise ValueError("KEY_BUNDLE_INVALID")
                result[row["client_id"]] = bundle
            return result

    def key_complete(
        self, round_id: str, client_id: str, commitment: str, idem_key: str, digest: str
    ) -> dict[str, object]:
        now = time.time()
        mismatch = False
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? AND operation='key_complete' AND idem_key=?",
                (client_id, round_id, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], json.loads(prior["response_json"]))
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not round_row or round_row["state"] not in {
                RoundState.KEY_SETUP.value,
                RoundState.KEY_AGGREGATION.value,
                RoundState.UPDATE_COLLECTION.value,
            }:
                raise ValueError("ROUND_CONFLICT")
            existing = connection.execute(
                "SELECT context_commitment FROM participants WHERE round_id=? AND client_id=?", (round_id, client_id)
            ).fetchone()
            if existing[0] and existing[0] != commitment:
                raise ValueError("ROUND_CONFLICT")
            connection.execute(
                "UPDATE participants SET context_commitment=?,state='READY_FOR_UPDATE',last_seen=? WHERE round_id=? AND client_id=?",
                (commitment, now, round_id, client_id),
            )
            response: dict[str, object] = {"accepted": True}
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (client_id, round_id, "key_complete", idem_key, digest, canonical_json(response), now),
            )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,created_at) VALUES(?,?,?,?)",
                (round_id, client_id, "KEY_CONTEXT_ACCEPTED", now),
            )
            rows = connection.execute(
                "SELECT context_commitment FROM participants WHERE round_id=?", (round_id,)
            ).fetchall()
            if all(row[0] for row in rows):
                if len({row[0] for row in rows}) != 1:
                    self._transition(
                        connection, round_id, RoundState.KEY_SETUP, RoundState.FAILED, client_id, "KEY_CONTEXT_MISMATCH"
                    )
                    mismatch = True
                elif round_row["state"] == RoundState.KEY_SETUP.value:
                    if round_row["protocol_version"] == "1.1":
                        self._create_tree_plan(connection, dict(round_row), now)
                        self._transition(
                            connection,
                            round_id,
                            RoundState.KEY_SETUP,
                            RoundState.KEY_AGGREGATION,
                            client_id,
                            "KEY_SETUP_BARRIER",
                        )
                    else:
                        self._transition(
                            connection,
                            round_id,
                            RoundState.KEY_SETUP,
                            RoundState.UPDATE_COLLECTION,
                            client_id,
                            "KEY_SETUP_BARRIER",
                        )
        # Raised after the transaction commits: signalling the mismatch from
        # inside it rolled the FAILED transition and its audit event back, and
        # left the round to time out under a generic deadline code instead.
        if mismatch:
            raise ValueError("KEY_CONTEXT_MISMATCH")
        return response

    @staticmethod
    def _create_tree_plan(connection: sqlite3.Connection, round_row: dict[str, object], now: float) -> None:
        records = connection.execute(
            "SELECT client_id,public_key_json FROM participants WHERE round_id=? ORDER BY client_id",
            (round_row["round_id"],),
        ).fetchall()
        if not records or any(record["public_key_json"] is None for record in records):
            raise ValueError("NOT_READY")
        manifest = []
        for record in records:
            public_key = json.loads(record["public_key_json"])
            manifest.append(
                {
                    "client_id": record["client_id"],
                    "public_key": public_key["public_key"],
                    "signature": public_key["signature"],
                }
            )
        plan = derive_tree_plan(round_row, manifest)
        digest = tree_plan_hash(plan)
        connection.execute(
            "INSERT INTO tree_plans(round_id,plan_hash,plan_json,manifest_json,created_at) VALUES(?,?,?,?,?)",
            (round_row["round_id"], digest, canonical_json(plan), canonical_json(manifest), now),
        )
        for task in plan["tasks"]:
            dependencies = task["dependencies"]
            connection.execute(
                "INSERT INTO key_tasks(round_id,task_id,action,stage,level,sender,receiver,dependencies_json,state) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    round_row["round_id"],
                    task["task_id"],
                    task["action"],
                    task["stage"],
                    task["level"],
                    task["sender"],
                    task["receiver"],
                    canonical_json(dependencies),
                    "READY" if not dependencies else "WAITING",
                ),
            )
        for task in plan["tasks"]:
            connection.executemany(
                "INSERT INTO task_dependencies(round_id,task_id,dependency_id) VALUES(?,?,?)",
                [(round_row["round_id"], task["task_id"], dependency) for dependency in task["dependencies"]],
            )
        connection.execute(
            "INSERT INTO audit_events(round_id,actor,event_type,created_at,detail) VALUES(?,?,?,?,?)",
            (round_row["round_id"], "coordinator", "TREE_PLAN_COMMITTED", now, digest),
        )

    def tree_plan(self, round_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tree_plans WHERE round_id=?", (round_id,)).fetchone()
            if not row:
                return None
            return {
                "plan_hash": row["plan_hash"],
                "plan": json.loads(row["plan_json"]),
                "ephemeral_manifest": json.loads(row["manifest_json"]),
            }

    @staticmethod
    def _task_metadata(row: sqlite3.Row) -> dict[str, object]:
        return {
            "task_id": row["task_id"],
            "action": row["action"],
            "stage": row["stage"],
            "level": row["level"],
            "sender": row["sender"],
            "receiver": row["receiver"],
            "dependencies": json.loads(row["dependencies_json"]),
        }

    def next_key_action(self, round_id: str, client_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not round_row or round_row["protocol_version"] != "1.1":
                raise ValueError("ROUND_CONFLICT")
            if round_row["state"] == RoundState.UPDATE_COLLECTION.value:
                return {"action": "KEY_AGGREGATION_COMPLETE"}
            if round_row["state"] != RoundState.KEY_AGGREGATION.value:
                raise ValueError("ROUND_CONFLICT")
            task = connection.execute(
                "SELECT * FROM key_tasks WHERE round_id=? AND sender=? AND state='READY' "
                "ORDER BY CASE stage WHEN 'GROUP' THEN 0 ELSE 1 END,level,task_id LIMIT 1",
                (round_id, client_id),
            ).fetchone()
            plan = connection.execute("SELECT * FROM tree_plans WHERE round_id=?", (round_id,)).fetchone()
            if not plan:
                raise ValueError("NOT_READY")
            if task:
                return {**self._task_metadata(task), "plan_hash": plan["plan_hash"]}
            incomplete = connection.execute(
                "SELECT count(*) FROM key_tasks WHERE round_id=? AND state!='COMPLETED'", (round_id,)
            ).fetchone()[0]
            final = connection.execute("SELECT * FROM final_keys WHERE round_id=?", (round_id,)).fetchone()
            plan_json = json.loads(plan["plan_json"])
            if not incomplete and not final and client_id == plan_json["root_client_id"]:
                return {
                    "action": "PUBLISH_FINAL",
                    "task_id": "final-key",
                    "stage": "FINAL_DISTRIBUTION",
                    "level": -1,
                    "sender": client_id,
                    "receiver": "ALL_PARTICIPANTS",
                    "dependencies": [plan_json["root_task_id"]],
                    "plan_hash": plan["plan_hash"],
                }
            receipt = connection.execute(
                "SELECT 1 FROM final_key_receipts WHERE round_id=? AND client_id=?", (round_id, client_id)
            ).fetchone()
            if final and not receipt:
                return {
                    "action": "ACK_FINAL",
                    "task_id": "final-key",
                    "stage": "FINAL_DISTRIBUTION",
                    "level": -1,
                    "sender": final["sender"],
                    "receiver": client_id,
                    "dependencies": [],
                    "plan_hash": plan["plan_hash"],
                }
            return None

    def record_task_artifact(
        self,
        round_id: str,
        client_id: str,
        task_id: str,
        stored: StoredObject,
        ciphertext_digest: str,
        nonce: str,
        signature: str,
        idem_key: str,
        request_digest: str,
    ) -> tuple[dict[str, object], bool]:
        now = time.time()
        operation = f"key_task_artifact:{task_id}"
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? "
                "AND operation=? AND idem_key=?",
                (client_id, round_id, operation, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != request_digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], json.loads(prior["response_json"])), True
            round_row = connection.execute("SELECT state FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            task = connection.execute(
                "SELECT * FROM key_tasks WHERE round_id=? AND task_id=?", (round_id, task_id)
            ).fetchone()
            if (
                not round_row
                or round_row["state"] != RoundState.KEY_AGGREGATION.value
                or not task
                or task["sender"] != client_id
                or task["state"] != "READY"
            ):
                raise ValueError("ROUND_CONFLICT")
            connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                (
                    stored.object_id,
                    round_id,
                    client_id,
                    f"key_task:{task_id}",
                    stored.path,
                    stored.digest,
                    stored.size,
                    now,
                ),
            )
            connection.execute(
                "UPDATE key_tasks SET state='ARTIFACT_UPLOADED',artifact_id=?,ciphertext_digest=?,nonce=?,signature=? "
                "WHERE round_id=? AND task_id=? AND state='READY'",
                (stored.object_id, ciphertext_digest, nonce, signature, round_id, task_id),
            )
            response: dict[str, object] = {
                "accepted": True,
                "artifact_id": stored.object_id,
                "digest": ciphertext_digest,
                "size": stored.size,
            }
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (client_id, round_id, operation, idem_key, request_digest, canonical_json(response), now),
            )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,created_at,detail) VALUES(?,?,?,?,?)",
                (round_id, client_id, "TREE_ARTIFACT_ACCEPTED", now, task_id),
            )
            return response, False

    def task_artifact(self, round_id: str, task_id: str, client_id: str) -> tuple[dict[str, object], dict[str, object]]:
        with self.connect() as connection:
            task = connection.execute(
                "SELECT * FROM key_tasks WHERE round_id=? AND task_id=?", (round_id, task_id)
            ).fetchone()
            if not task or task["receiver"] != client_id or task["state"] != "COMPLETED":
                raise ValueError("ROUND_CONFLICT")
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (task["artifact_id"],)
            ).fetchone()
            if not artifact:
                raise ValueError("NOT_READY")
            metadata = {
                **self._task_metadata(task),
                "model_schema_hash": connection.execute(
                    "SELECT model_schema_hash FROM rounds WHERE round_id=?", (round_id,)
                ).fetchone()[0],
                "nonce": task["nonce"],
                "ciphertext_digest": task["ciphertext_digest"],
                "ciphertext_size": artifact["size"],
                "signature": task["signature"],
            }
            plan_hash = connection.execute("SELECT plan_hash FROM tree_plans WHERE round_id=?", (round_id,)).fetchone()[
                0
            ]
            metadata["plan_hash"] = plan_hash
            return dict(artifact), metadata

    def acknowledge_task(
        self, round_id: str, client_id: str, task_id: str, artifact_digest: str, idem_key: str, digest: str
    ) -> dict[str, object]:
        now = time.time()
        operation = f"key_task_ack:{task_id}"
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? "
                "AND operation=? AND idem_key=?",
                (client_id, round_id, operation, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], json.loads(prior["response_json"]))
            task = connection.execute(
                "SELECT * FROM key_tasks WHERE round_id=? AND task_id=?", (round_id, task_id)
            ).fetchone()
            round_row = connection.execute("SELECT state FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if (
                not task
                or not round_row
                or round_row["state"] != RoundState.KEY_AGGREGATION.value
                or task["sender"] != client_id
                or task["state"] != "ARTIFACT_UPLOADED"
                or task["ciphertext_digest"] != artifact_digest
            ):
                raise ValueError("ROUND_CONFLICT")
            connection.execute(
                "UPDATE key_tasks SET state='COMPLETED',completed_at=? WHERE round_id=? AND task_id=?",
                (now, round_id, task_id),
            )
            connection.execute(
                "INSERT INTO task_receipts(round_id,task_id,actor,artifact_digest,created_at) VALUES(?,?,?,?,?)",
                (round_id, task_id, client_id, artifact_digest, now),
            )
            connection.execute(
                "UPDATE key_tasks SET state='READY' WHERE round_id=? AND state='WAITING' AND NOT EXISTS ("
                "SELECT 1 FROM task_dependencies d JOIN key_tasks source "
                "ON source.round_id=d.round_id AND source.task_id=d.dependency_id "
                "WHERE d.round_id=key_tasks.round_id AND d.task_id=key_tasks.task_id AND source.state!='COMPLETED')",
                (round_id,),
            )
            response: dict[str, object] = {"accepted": True}
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (client_id, round_id, operation, idem_key, digest, canonical_json(response), now),
            )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,created_at,detail) VALUES(?,?,?,?,?)",
                (round_id, client_id, "TREE_TASK_COMPLETED", now, task_id),
            )
            return response

    def record_final_key(
        self,
        round_id: str,
        client_id: str,
        stored: StoredObject,
        ciphertext_digest: str,
        nonce: str,
        signature: str,
        idem_key: str,
        request_digest: str,
    ) -> tuple[dict[str, object], bool]:
        now = time.time()
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? "
                "AND operation='final_key' AND idem_key=?",
                (client_id, round_id, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != request_digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], json.loads(prior["response_json"])), True
            plan_row = connection.execute("SELECT * FROM tree_plans WHERE round_id=?", (round_id,)).fetchone()
            round_row = connection.execute("SELECT state FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            incomplete = connection.execute(
                "SELECT count(*) FROM key_tasks WHERE round_id=? AND state!='COMPLETED'", (round_id,)
            ).fetchone()[0]
            if (
                not plan_row
                or not round_row
                or round_row["state"] != RoundState.KEY_AGGREGATION.value
                or json.loads(plan_row["plan_json"])["root_client_id"] != client_id
                or incomplete
            ):
                raise ValueError("ROUND_CONFLICT")
            connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                (stored.object_id, round_id, client_id, "final_key", stored.path, stored.digest, stored.size, now),
            )
            connection.execute(
                "INSERT INTO final_keys VALUES(?,?,?,?,?,?,?)",
                (round_id, stored.object_id, ciphertext_digest, nonce, signature, client_id, now),
            )
            response: dict[str, object] = {
                "accepted": True,
                "artifact_id": stored.object_id,
                "digest": ciphertext_digest,
                "size": stored.size,
            }
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (client_id, round_id, "final_key", idem_key, request_digest, canonical_json(response), now),
            )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,created_at) VALUES(?,?,?,?)",
                (round_id, client_id, "FINAL_KEY_PUBLISHED", now),
            )
            return response, False

    def final_key(self, round_id: str) -> tuple[dict[str, object], dict[str, object]] | None:
        with self.connect() as connection:
            final = connection.execute("SELECT * FROM final_keys WHERE round_id=?", (round_id,)).fetchone()
            if not final:
                return None
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (final["artifact_id"],)
            ).fetchone()
            plan = connection.execute("SELECT * FROM tree_plans WHERE round_id=?", (round_id,)).fetchone()
            if not artifact or not plan:
                return None
            metadata = {
                "nonce": final["nonce"],
                "ciphertext_digest": final["ciphertext_digest"],
                "ciphertext_size": artifact["size"],
                "signature": final["signature"],
                "sender": final["sender"],
                "plan_hash": plan["plan_hash"],
            }
            return dict(artifact), metadata

    def acknowledge_final_key(
        self, round_id: str, client_id: str, artifact_digest: str, idem_key: str, digest: str
    ) -> dict[str, object]:
        now = time.time()
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? "
                "AND operation='final_key_ack' AND idem_key=?",
                (client_id, round_id, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], json.loads(prior["response_json"]))
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            final = connection.execute("SELECT * FROM final_keys WHERE round_id=?", (round_id,)).fetchone()
            if (
                not round_row
                or round_row["state"] != RoundState.KEY_AGGREGATION.value
                or not final
                or final["ciphertext_digest"] != artifact_digest
            ):
                raise ValueError("ROUND_CONFLICT")
            connection.execute(
                "INSERT INTO final_key_receipts VALUES(?,?,?,?)", (round_id, client_id, artifact_digest, now)
            )
            response: dict[str, object] = {"accepted": True}
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (client_id, round_id, "final_key_ack", idem_key, digest, canonical_json(response), now),
            )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,created_at) VALUES(?,?,?,?)",
                (round_id, client_id, "FINAL_KEY_ACKNOWLEDGED", now),
            )
            count = connection.execute(
                "SELECT count(*) FROM final_key_receipts WHERE round_id=?", (round_id,)
            ).fetchone()[0]
            if count == round_row["expected_count"]:
                self._transition(
                    connection,
                    round_id,
                    RoundState.KEY_AGGREGATION,
                    RoundState.UPDATE_COLLECTION,
                    client_id,
                    "FINAL_KEY_BARRIER",
                )
            return response

    def fail_round(self, round_id: str, actor: str, code: str, detail: str) -> None:
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not row:
                return
            state = RoundState(row["state"])
            if state in {RoundState.COMPLETED, RoundState.FAILED, RoundState.ABORTED, RoundState.EXPIRED}:
                return
            self._transition(connection, round_id, state, RoundState.FAILED, actor, code, detail[:256])

    def record_update(
        self, round_id: str, client_id: str, stored: StoredObject, tag: str, idem_key: str, request_digest: str
    ) -> tuple[dict[str, object], bool]:
        now = time.time()
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? AND operation='update' AND idem_key=?",
                (client_id, round_id, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != request_digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], json.loads(prior["response_json"])), True
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not round_row or round_row["state"] not in {
                RoundState.UPDATE_COLLECTION.value,
                RoundState.AGGREGATING.value,
            }:
                raise ValueError("ROUND_CONFLICT")
            participant = connection.execute(
                "SELECT update_digest,integrity_tag FROM participants WHERE round_id=? AND client_id=?",
                (round_id, client_id),
            ).fetchone()
            if participant[0]:
                if participant[0] != stored.digest or participant[1] != tag:
                    raise ValueError("ROUND_CONFLICT")
                artifact = connection.execute(
                    "SELECT artifact_id,digest,size FROM artifacts WHERE round_id=? AND client_id=? AND artifact_type='update'",
                    (round_id, client_id),
                ).fetchone()
                response: dict[str, object] = {
                    "accepted": True,
                    "artifact_id": artifact["artifact_id"],
                    "digest": artifact["digest"],
                    "size": artifact["size"],
                }
                connection.execute(
                    "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                    (client_id, round_id, "update", idem_key, request_digest, canonical_json(response), now),
                )
                return response, True
            connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                (stored.object_id, round_id, client_id, "update", stored.path, stored.digest, stored.size, now),
            )
            connection.execute(
                "UPDATE participants SET update_artifact_id=?,update_digest=?,integrity_tag=?,state='UPDATE_SUBMITTED',last_seen=? WHERE round_id=? AND client_id=?",
                (stored.object_id, stored.digest, tag, now, round_id, client_id),
            )
            response = {"accepted": True, "artifact_id": stored.object_id, "digest": stored.digest, "size": stored.size}
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (client_id, round_id, "update", idem_key, request_digest, canonical_json(response), now),
            )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,created_at) VALUES(?,?,?,?)",
                (round_id, client_id, "UPDATE_ACCEPTED", now),
            )
            count = connection.execute(
                "SELECT count(*) FROM participants WHERE round_id=? AND update_artifact_id IS NOT NULL", (round_id,)
            ).fetchone()[0]
            if count == round_row["expected_count"] and round_row["state"] == RoundState.UPDATE_COLLECTION.value:
                self._transition(
                    connection,
                    round_id,
                    RoundState.UPDATE_COLLECTION,
                    RoundState.AGGREGATING,
                    client_id,
                    "UPDATE_BARRIER",
                )
                connection.execute(
                    "INSERT INTO jobs(job_id,round_id,state) VALUES(?,?,?)", (str(uuid.uuid4()), round_id, "READY")
                )
            return response, False

    def claim_job(self, lease_seconds: float = 900.0) -> dict[str, object] | None:
        if lease_seconds <= 0:
            raise ValueError("worker lease must be positive")
        now = time.time()
        with self.transaction() as connection:
            stale = connection.execute(
                "SELECT * FROM jobs WHERE state='CLAIMED' AND "
                "((lease_until IS NOT NULL AND lease_until<=?) OR "
                "(lease_until IS NULL AND claimed_at IS NOT NULL AND claimed_at<=?))",
                (now, now - lease_seconds),
            ).fetchall()
            for expired in stale:
                if expired["attempts"] < expired["max_attempts"]:
                    connection.execute(
                        "UPDATE jobs SET state='READY',claimed_at=NULL,lease_until=NULL,claim_token=NULL,"
                        "last_error='worker lease expired' WHERE job_id=? AND state='CLAIMED'",
                        (expired["job_id"],),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(round_id,actor,event_type,created_at,detail) VALUES(?,?,?,?,?)",
                        (
                            expired["round_id"],
                            "worker",
                            "AGGREGATION_LEASE_EXPIRED",
                            now,
                            "job returned to ready queue",
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE jobs SET state='FAILED',lease_until=NULL,claim_token=NULL,"
                        "last_error='worker lease expired after maximum attempts' WHERE job_id=? AND state='CLAIMED'",
                        (expired["job_id"],),
                    )
                    round_state = RoundState(
                        connection.execute(
                            "SELECT state FROM rounds WHERE round_id=?", (expired["round_id"],)
                        ).fetchone()[0]
                    )
                    if round_state == RoundState.AGGREGATING:
                        self._transition(
                            connection,
                            str(expired["round_id"]),
                            round_state,
                            RoundState.FAILED,
                            "worker",
                            "AGGREGATION_LEASE_EXHAUSTED",
                            "worker lease expired after maximum attempts",
                        )
            row = connection.execute(
                "SELECT j.* FROM jobs j JOIN rounds r ON r.round_id=j.round_id "
                "WHERE j.state='READY' AND j.attempts<j.max_attempts "
                "AND r.state='AGGREGATING' AND r.deadline_at>? ORDER BY j.rowid LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            claim_token = str(uuid.uuid4())
            result = connection.execute(
                "UPDATE jobs SET state='CLAIMED',attempts=attempts+1,claimed_at=?,lease_until=?,claim_token=? "
                "WHERE job_id=? AND state='READY'",
                (now, now + lease_seconds, claim_token, row["job_id"]),
            )
            if result.rowcount != 1:
                return None
            return dict(connection.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone())

    def renew_job(self, job_id: str, claim_token: str, lease_seconds: float) -> None:
        if lease_seconds <= 0:
            raise ValueError("worker lease must be positive")
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE jobs SET lease_until=? WHERE job_id=? AND state='CLAIMED' AND claim_token=?",
                (time.time() + lease_seconds, job_id, claim_token),
            )
            if result.rowcount != 1:
                raise ValueError("WORKER_CLAIM_LOST")

    def expire_rounds(self, now: float | None = None) -> int:
        """Expire overdue active rounds and make their queued work unclaimable."""
        timestamp = time.time() if now is None else now
        terminal = {
            RoundState.COMPLETED.value,
            RoundState.FAILED.value,
            RoundState.ABORTED.value,
            RoundState.EXPIRED.value,
        }
        expired_count = 0
        with self.transaction() as connection:
            rows = connection.execute("SELECT round_id,state FROM rounds WHERE deadline_at<?", (timestamp,)).fetchall()
            for row in rows:
                if row["state"] in terminal:
                    continue
                state = RoundState(row["state"])
                self._transition(
                    connection,
                    str(row["round_id"]),
                    state,
                    RoundState.EXPIRED,
                    "reconciler",
                    "ROUND_DEADLINE_EXCEEDED",
                )
                connection.execute(
                    "UPDATE jobs SET state='FAILED',lease_until=NULL,claim_token=NULL,"
                    "last_error='round deadline exceeded' WHERE round_id=? AND state IN ('READY','CLAIMED')",
                    (row["round_id"],),
                )
                expired_count += 1
        return expired_count

    def retention_candidates(self, now: float | None = None) -> list[tuple[str, list[str]]]:
        timestamp = time.time() if now is None else now
        with self.connect() as connection:
            rounds = connection.execute(
                "SELECT round_id FROM rounds WHERE retention_at<=? "
                "AND state IN ('COMPLETED','FAILED','ABORTED','EXPIRED')",
                (timestamp,),
            ).fetchall()
            return [
                (
                    str(row["round_id"]),
                    [
                        str(artifact["path"])
                        for artifact in connection.execute(
                            "SELECT path FROM artifacts WHERE round_id=?",
                            (row["round_id"],),
                        ).fetchall()
                    ],
                )
                for row in rounds
            ]

    def purge_round(self, round_id: str) -> None:
        """Delete terminal metadata after its artifact files have been removed."""
        with self.transaction() as connection:
            row = connection.execute("SELECT state,retention_at FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not row:
                return
            if (
                row["state"]
                not in {
                    RoundState.COMPLETED.value,
                    RoundState.FAILED.value,
                    RoundState.ABORTED.value,
                    RoundState.EXPIRED.value,
                }
                or row["retention_at"] > time.time()
            ):
                raise ValueError("ROUND_CONFLICT")
            for table in (
                "jobs",
                "final_key_receipts",
                "final_keys",
                "task_receipts",
                "task_dependencies",
                "key_tasks",
                "tree_plans",
                "artifacts",
                "participants",
                "audit_events",
            ):
                connection.execute(f"DELETE FROM {table} WHERE round_id=?", (round_id,))
            connection.execute("DELETE FROM idempotency WHERE round_id=?", (round_id,))
            create_records = connection.execute(
                "SELECT rowid,response_json FROM idempotency WHERE operation='create'"
            ).fetchall()
            for record in create_records:
                response = json.loads(record["response_json"])
                if response.get("round_id") == round_id:
                    connection.execute("DELETE FROM idempotency WHERE rowid=?", (record["rowid"],))
            connection.execute("DELETE FROM rounds WHERE round_id=?", (round_id,))

    def job_inputs(self, round_id: str) -> tuple[dict[str, object], list[dict[str, object]]]:
        with self.connect() as connection:
            round_row = dict(connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone())
            rows = connection.execute(
                "SELECT a.*,p.integrity_tag FROM artifacts a JOIN participants p ON a.round_id=p.round_id AND a.client_id=p.client_id WHERE a.round_id=? AND a.artifact_type='update' ORDER BY a.client_id",
                (round_id,),
            ).fetchall()
            return round_row, [dict(row) for row in rows]

    def complete_job(self, job_id: str, round_id: str, claim_token: str, stored: StoredObject, tag: str) -> None:
        now = time.time()
        with self.transaction() as connection:
            owned = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id=? AND round_id=? AND state='CLAIMED' AND claim_token=?",
                (job_id, round_id, claim_token),
            ).fetchone()
            if not owned:
                raise ValueError("WORKER_CLAIM_LOST")
            existing = connection.execute(
                "SELECT * FROM artifacts WHERE round_id=? AND artifact_type='result'", (round_id,)
            ).fetchone()
            if existing:
                if existing["digest"] != stored.digest:
                    raise ValueError("WORKER_RESULT_CONFLICT")
            else:
                connection.execute(
                    "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                    (stored.object_id, round_id, None, "result", stored.path, stored.digest, stored.size, now),
                )
            state = RoundState(
                connection.execute("SELECT state FROM rounds WHERE round_id=?", (round_id,)).fetchone()[0]
            )
            if state != RoundState.AGGREGATING:
                raise ValueError("WORKER_CLAIM_LOST")
            self._transition(
                connection,
                round_id,
                state,
                RoundState.RESULT_READY,
                "worker",
                "AGGREGATION_COMPLETED",
            )
            connection.execute(
                "UPDATE jobs SET state='COMPLETED',lease_until=NULL,claim_token=NULL,result_integrity_tag=?,last_error=NULL "
                "WHERE job_id=? AND claim_token=?",
                (tag, job_id, claim_token),
            )

    def fail_job(self, job_id: str, round_id: str, claim_token: str, detail: str, retryable: bool) -> bool:
        with self.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_id=? AND round_id=? AND state='CLAIMED' AND claim_token=?",
                (job_id, round_id, claim_token),
            ).fetchone()
            if not job:
                return False
            if retryable and job["attempts"] < job["max_attempts"]:
                connection.execute(
                    "UPDATE jobs SET state='READY',claimed_at=NULL,lease_until=NULL,claim_token=NULL,last_error=? "
                    "WHERE job_id=? AND claim_token=?",
                    (detail[:256], job_id, claim_token),
                )
            else:
                connection.execute(
                    "UPDATE jobs SET state='FAILED',lease_until=NULL,claim_token=NULL,last_error=? "
                    "WHERE job_id=? AND claim_token=?",
                    (detail[:256], job_id, claim_token),
                )
                state = RoundState(
                    connection.execute("SELECT state FROM rounds WHERE round_id=?", (round_id,)).fetchone()[0]
                )
                if state == RoundState.AGGREGATING:
                    self._transition(
                        connection, round_id, state, RoundState.FAILED, "worker", "AGGREGATION_FAILED", detail[:256]
                    )
            return True

    def result(self, round_id: str) -> tuple[dict[str, object], str] | None:
        with self.connect() as connection:
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE round_id=? AND artifact_type='result'", (round_id,)
            ).fetchone()
            job = connection.execute(
                "SELECT result_integrity_tag FROM jobs WHERE round_id=? AND state='COMPLETED'", (round_id,)
            ).fetchone()
            return (dict(artifact), str(job[0])) if artifact and job else None

    def complete_participant(self, round_id: str, client_id: str, idem_key: str, digest: str) -> dict[str, object]:
        now = time.time()
        with self.transaction() as connection:
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not round_row or round_row["state"] not in {RoundState.RESULT_READY.value, RoundState.COMPLETED.value}:
                raise ValueError("ROUND_CONFLICT")
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? AND operation='complete' AND idem_key=?",
                (client_id, round_id, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], json.loads(prior["response_json"]))
            connection.execute(
                "UPDATE participants SET completed=1,state='COMPLETED',last_seen=? WHERE round_id=? AND client_id=?",
                (now, round_id, client_id),
            )
            response: dict[str, object] = {"accepted": True}
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (client_id, round_id, "complete", idem_key, digest, canonical_json(response), now),
            )
            connection.execute(
                "INSERT INTO audit_events(round_id,actor,event_type,created_at) VALUES(?,?,?,?)",
                (round_id, client_id, "PARTICIPANT_COMPLETED", now),
            )
            count = connection.execute(
                "SELECT count(*) FROM participants WHERE round_id=? AND completed=1", (round_id,)
            ).fetchone()[0]
            if count == round_row["expected_count"] and round_row["state"] == RoundState.RESULT_READY.value:
                self._transition(
                    connection, round_id, RoundState.RESULT_READY, RoundState.COMPLETED, client_id, "COMPLETION_BARRIER"
                )
            return response

    def abort(
        self, round_id: str, actor: str, reason: str, idem_key: str, digest: str
    ) -> tuple[dict[str, object], bool]:
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT content_digest,response_json FROM idempotency WHERE client_id=? AND round_id=? AND operation='abort' AND idem_key=?",
                (actor, round_id, idem_key),
            ).fetchone()
            if prior:
                if prior["content_digest"] != digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], json.loads(prior["response_json"])), True
            row = connection.execute("SELECT state FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not row:
                raise ValueError("ROUND_NOT_FOUND")
            state = RoundState(row[0])
            if state in {RoundState.COMPLETED, RoundState.FAILED, RoundState.ABORTED, RoundState.EXPIRED}:
                raise ValueError("ROUND_CONFLICT")
            self._transition(connection, round_id, state, RoundState.ABORTED, actor, "OPERATOR_ABORT", reason)
            response: dict[str, object] = {"accepted": True}
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                (actor, round_id, "abort", idem_key, digest, canonical_json(response), time.time()),
            )
            return response, False
