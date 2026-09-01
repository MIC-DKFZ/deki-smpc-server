import threading
import time
import uuid
from pathlib import Path

import pytest

from app.domain.states import RoundState, transition_allowed
from app.persistence.database import Database


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RoundState.CREATED, RoundState.REGISTRATION_OPEN),
        (RoundState.REGISTRATION_OPEN, RoundState.KEY_SETUP),
        (RoundState.KEY_SETUP, RoundState.UPDATE_COLLECTION),
        (RoundState.UPDATE_COLLECTION, RoundState.AGGREGATING),
        (RoundState.AGGREGATING, RoundState.RESULT_READY),
        (RoundState.RESULT_READY, RoundState.COMPLETED),
        (RoundState.KEY_SETUP, RoundState.FAILED),
        (RoundState.UPDATE_COLLECTION, RoundState.EXPIRED),
    ],
)
def test_legal_state_transitions(source: RoundState, target: RoundState) -> None:
    assert transition_allowed(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RoundState.CREATED, RoundState.UPDATE_COLLECTION),
        (RoundState.KEY_SETUP, RoundState.RESULT_READY),
        (RoundState.COMPLETED, RoundState.REGISTRATION_OPEN),
        (RoundState.FAILED, RoundState.COMPLETED),
    ],
)
def test_illegal_state_transitions(source: RoundState, target: RoundState) -> None:
    assert not transition_allowed(source, target)


def _aggregating_round(database: Database, suffix: str) -> str:
    schema = {
        "aggregation_policy": "MEAN",
        "entries": [{"name": "weight", "shape": [1], "dtype": "float32", "policy": "MEAN"}],
        "precision_bits": 16,
    }
    response, _ = database.create_round(
        "federation",
        "1.0",
        schema,
        "0" * 64,
        ["a", "b", "c"],
        "1" * 64,
        60,
        3600,
        "operator",
        f"create-{suffix}",
        f"digest-{suffix}",
    )
    round_id = str(response["round_id"])
    with database.transaction() as connection:
        connection.execute("UPDATE rounds SET state='AGGREGATING' WHERE round_id=?", (round_id,))
    return round_id


def test_expired_worker_lease_is_reclaimed_with_new_ownership(tmp_path: Path) -> None:
    database = Database(tmp_path / "metadata.sqlite")
    database.initialize({})
    round_id = _aggregating_round(database, "reclaim")
    job_id = str(uuid.uuid4())
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,round_id,state,attempts,max_attempts,claimed_at,lease_until,claim_token) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (job_id, round_id, "CLAIMED", 1, 3, time.time() - 20, time.time() - 10, "dead-worker"),
        )

    reclaimed = database.claim_job(30)

    assert reclaimed is not None
    assert reclaimed["job_id"] == job_id
    assert reclaimed["attempts"] == 2
    assert reclaimed["claim_token"] != "dead-worker"
    with pytest.raises(ValueError, match="WORKER_CLAIM_LOST"):
        database.renew_job(job_id, "dead-worker", 30)
    database.renew_job(job_id, str(reclaimed["claim_token"]), 30)


def test_exhausted_worker_lease_fails_round(tmp_path: Path) -> None:
    database = Database(tmp_path / "metadata.sqlite")
    database.initialize({})
    round_id = _aggregating_round(database, "exhausted")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,round_id,state,attempts,max_attempts,claimed_at,lease_until,claim_token) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), round_id, "CLAIMED", 3, 3, time.time() - 20, time.time() - 10, "dead-worker"),
        )

    assert database.claim_job(30) is None
    assert database.get_round(round_id)["state"] == RoundState.FAILED.value


def test_deadline_reconciliation_expires_round_and_disables_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "metadata.sqlite")
    database.initialize({})
    round_id = _aggregating_round(database, "deadline")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE rounds SET deadline_at=? WHERE round_id=?",
            (time.time() - 1, round_id),
        )
        connection.execute(
            "INSERT INTO jobs(job_id,round_id,state) VALUES(?,?,?)",
            (str(uuid.uuid4()), round_id, "READY"),
        )

    assert database.claim_job(30) is None
    assert database.expire_rounds() == 1
    assert database.get_round(round_id)["state"] == RoundState.EXPIRED.value
    with database.connect() as connection:
        assert connection.execute("SELECT state FROM jobs WHERE round_id=?", (round_id,)).fetchone()[0] == "FAILED"


def test_retention_purge_removes_round_and_create_idempotency(tmp_path: Path) -> None:
    database = Database(tmp_path / "metadata.sqlite")
    database.initialize({})
    round_id = _aggregating_round(database, "retention")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE rounds SET state='ABORTED',retention_at=? WHERE round_id=?",
            (time.time() - 1, round_id),
        )

    assert database.retention_candidates() == [(round_id, [])]
    database.purge_round(round_id)

    assert database.get_round(round_id) is None
    replacement = _aggregating_round(database, "retention")
    assert replacement != round_id


def test_concurrent_database_initialization_is_serialized(tmp_path: Path) -> None:
    errors: list[Exception] = []

    def initialize() -> None:
        try:
            Database(tmp_path / "metadata.sqlite").initialize({})
        except Exception as exc:  # noqa: BLE001 - failures are asserted in the parent thread
            errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)


def test_divergent_key_context_durably_fails_the_round(tmp_path: Path) -> None:
    # Signalling the mismatch from inside the transaction used to roll the
    # FAILED transition back, leaving the round to time out under a generic
    # deadline code with no record of the divergence.
    database = Database(tmp_path / "metadata.sqlite")
    database.initialize({})
    round_id = _aggregating_round(database, "context")
    with database.transaction() as connection:
        connection.execute("UPDATE rounds SET state='KEY_SETUP' WHERE round_id=?", (round_id,))
    database.key_complete(round_id, "a", "a" * 64, "kc-a", "digest-a")
    database.key_complete(round_id, "b", "a" * 64, "kc-b", "digest-b")
    with pytest.raises(ValueError, match="KEY_CONTEXT_MISMATCH"):
        database.key_complete(round_id, "c", "c" * 64, "kc-c", "digest-c")

    row = database.get_round(round_id)
    assert row is not None
    assert row["state"] == RoundState.FAILED.value
    assert row["failure_code"] == "KEY_CONTEXT_MISMATCH"
    with database.connect() as connection:
        events = [
            event["event_type"]
            for event in connection.execute(
                "SELECT event_type FROM audit_events WHERE round_id=?", (round_id,)
            ).fetchall()
        ]
    assert "KEY_CONTEXT_MISMATCH" in events
