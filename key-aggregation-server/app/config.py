"""Side-effect-free application settings."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    artifact_path: Path
    federation_config: dict[str, dict[str, dict[str, str]]]
    admin_token: str
    max_artifact_bytes: int = 512 * 1024 * 1024
    round_ttl_seconds: int = 1800
    retention_seconds: int = 86400
    worker_poll_seconds: float = 0.25
    worker_lease_seconds: float = 900.0
    maintenance_interval_seconds: float = 60.0
    max_participants: int | None = None

    @classmethod
    def from_env(cls) -> Settings:
        admin_token = os.environ.get("DEKI_ADMIN_TOKEN", "")
        if len(admin_token) < 32:
            raise RuntimeError("DEKI_ADMIN_TOKEN must contain at least 32 characters")
        try:
            federations = json.loads(os.environ.get("DEKI_FEDERATIONS_JSON", "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("DEKI_FEDERATIONS_JSON is invalid") from exc
        if not isinstance(federations, dict):
            raise RuntimeError(  # noqa: TRY004 - environment configuration failure
                "DEKI_FEDERATIONS_JSON must be an object"
            )
        settings = cls(
            Path(os.environ.get("DEKI_DATABASE_PATH", "/data/metadata.sqlite3")),
            Path(os.environ.get("DEKI_ARTIFACT_PATH", "/data/artifacts")),
            federations,
            admin_token,
            int(os.environ.get("DEKI_MAX_ARTIFACT_BYTES", str(512 * 1024 * 1024))),
            int(os.environ.get("DEKI_ROUND_TTL_SECONDS", "1800")),
            int(os.environ.get("DEKI_RETENTION_SECONDS", "86400")),
            float(os.environ.get("DEKI_WORKER_POLL_SECONDS", "0.25")),
            float(os.environ.get("DEKI_WORKER_LEASE_SECONDS", "900")),
            float(os.environ.get("DEKI_MAINTENANCE_INTERVAL_SECONDS", "60")),
            int(value) if (value := os.environ.get("DEKI_MAX_PARTICIPANTS")) else None,
        )
        if (
            min(
                settings.max_artifact_bytes,
                settings.round_ttl_seconds,
                settings.retention_seconds,
                settings.worker_poll_seconds,
                settings.worker_lease_seconds,
                settings.maintenance_interval_seconds,
            )
            <= 0
        ):
            raise RuntimeError("numeric DEKI settings must be positive")
        if settings.max_participants is not None and settings.max_participants < 3:
            raise RuntimeError("DEKI_MAX_PARTICIPANTS must be at least 3")
        return settings
