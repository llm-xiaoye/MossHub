"""Central paths and non-secret service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    home: Path
    project: Path
    python: Path
    sqlite: Path
    compose: Path
    logs: Path
    run_state: Path
    model_embedding: Path
    model_reranker: Path
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    model_host: str = "127.0.0.1"
    model_port: int = 8001
    milvus_uri: str = "http://127.0.0.1:19530"
    redis_url: str = "redis://127.0.0.1:6379/0"
    frontend_origin: str = "http://127.0.0.1:5173"

    @classmethod
    def load(cls) -> "Settings":
        raw = os.environ.get("RAG_HOME", str(Path.home() / "MossHub"))
        home = Path(raw)
        if not home.is_absolute() or home.resolve() != home:
            raise ValueError("RAG_HOME must be a canonical absolute path")
        project = home / "project"
        return cls(
            home=home,
            project=project,
            python=home / "runtime/python/bin/python",
            sqlite=home / "data/sqlite/rag.sqlite3",
            compose=project / "configs/services/compose.yaml",
            logs=home / "logs",
            run_state=home / "tmp/services.json",
            model_embedding=home / "models/embedding/bge-m3",
            model_reranker=home / "models/reranker/bge-reranker-v2-m3",
        )

    def required_dirs(self) -> tuple[Path, ...]:
        return (
            self.home / "data/sqlite",
            self.home / "data/services/milvus",
            self.home / "data/services/etcd",
            self.home / "data/services/minio",
            self.home / "data/services/redis",
            self.logs,
            self.home / "tmp",
        )


    def check_business_start(self) -> None:
        """A copied VERIFIED database never unlocks an incomplete recovery."""
        import json
        import sqlite3
        from contextlib import closing

        marker = self.home / "recovery.json"
        record = None
        if os.path.lexists(marker):
            if marker.is_symlink() or not marker.is_file():
                raise RuntimeError("RECOVERY_NOT_VERIFIED: recovery record is not a regular file")
            try:
                record = json.loads(marker.read_bytes())
            except (OSError, ValueError) as exc:
                raise RuntimeError("RECOVERY_NOT_VERIFIED: recovery record cannot be read") from exc
            if (not isinstance(record, dict) or record.get("schema") != "raghub.recovery.v1"
                    or record.get("new_home") != str(self.home) or record.get("phase") != "VERIFIED"):
                raise RuntimeError("RECOVERY_NOT_VERIFIED: restore or verification is incomplete")
            if not self.sqlite.is_file():
                raise RuntimeError("RECOVERY_NOT_VERIFIED: verified database is missing")
        if self.sqlite.is_file():
            with closing(sqlite3.connect(self.sqlite.as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT data_instance_id,recovery_id,verification_status,dispatch_open "
                                 "FROM system_state WHERE singleton=1").fetchone()
            if row is None:
                raise RuntimeError("RECOVERY_NOT_VERIFIED: instance state is missing")
            if record is None:
                if row[1] is not None:
                    raise RuntimeError("RECOVERY_NOT_VERIFIED: restored instance has no recovery record")
            elif (not isinstance(record.get("data_instance_id"), str)
                  or not isinstance(record.get("recovery_id"), str)
                  or row[0] != record["data_instance_id"] or row[1] != record["recovery_id"]
                  or row[2] != "VERIFIED" or row[3] != 1):
                raise RuntimeError("RECOVERY_NOT_VERIFIED: recovery and database state differ")

    def check_layout(self) -> None:
        for path in (self.home, self.project, *self.required_dirs()):
            if not path.is_dir() or path.is_symlink():
                raise RuntimeError(f"required directory missing or aliased: {path}")
        if not self.compose.is_file() or not self.python.is_file():
            raise RuntimeError("locked environment or compose configuration is missing")
