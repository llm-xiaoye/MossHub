"""SQLAlchemy Core owns SQLite connections and transactions.

Legacy SQL files remain immutable inputs. Alembic is the single schema upgrade
entry; API and workers only verify an already upgraded database.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class Row:
    """The small sqlite3.Row interface used by the existing stores."""
    def __init__(self, keys, values):
        self._keys, self._values = tuple(keys), tuple(values)

    def keys(self):
        return list(self._keys)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        for i, name in enumerate(self._keys):
            if name.lower() == key.lower():
                return self._values[i]
        raise IndexError("No item with that key")

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class Result:
    def __init__(self, result):
        self.rowcount = result.rowcount
        self.lastrowid = result.lastrowid
        keys = tuple(result.keys()) if result.returns_rows else ()
        self._rows = [Row(keys, row) for row in result.fetchall()] if result.returns_rows else []
        self._offset = 0
        result.close()

    def fetchone(self):
        if self._offset == len(self._rows):
            return None
        row = self._rows[self._offset]
        self._offset += 1
        return row

    def fetchall(self):
        rows = self._rows[self._offset:]
        self._offset = len(self._rows)
        return rows

    def fetchmany(self, size=1):
        end = min(len(self._rows), self._offset + size)
        rows = self._rows[self._offset:end]
        self._offset = end
        return rows

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class Connection:
    """A per-thread Core connection; statements outside a block are short units."""
    def __init__(self, engine):
        self.engine = engine
        self.core = engine.connect()
        self._thread = threading.get_ident()
        self._explicit = False

    def _check(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError("SQLite connection cannot cross threads")
        if self.core.closed:
            raise RuntimeError("SQLite connection is closed")

    def execute(self, sql: str, parameters=()):
        self._check()
        first = sql.lstrip().split(None, 1)[0].upper()
        if first in {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
            raise RuntimeError("transaction control belongs to SQLAlchemy")
        params = parameters if isinstance(parameters, dict) else tuple(parameters)
        try:
            if self._explicit:
                return Result(self.core.exec_driver_sql(sql, params))
            # Only SELECT uses a deferred read unit. DML/DDL (including RETURNING)
            # runs in an immediate write unit and commits only after full fetch.
            with transaction(self, immediate=first != "SELECT"):
                return Result(self.core.exec_driver_sql(sql, params))
        except DBAPIError as exc:
            raise exc.orig from exc

    def executemany(self, sql, parameters):
        self._check()
        try:
            if self._explicit:
                return Result(self.core.exec_driver_sql(sql, [tuple(x) for x in parameters]))
            with transaction(self):
                return Result(self.core.exec_driver_sql(sql, [tuple(x) for x in parameters]))
        except DBAPIError as exc:
            raise exc.orig from exc

    @property
    def in_transaction(self):
        return self.core.in_transaction()

    def commit(self):
        self._check()
        if self._explicit:
            raise RuntimeError("commit belongs to the transaction context")
        self.core.commit()

    def rollback(self):
        self._check()
        if self._explicit:
            raise RuntimeError("rollback belongs to the transaction context")
        self.core.rollback()

    def close(self):
        self._check()
        self.core.close()
        self.engine.dispose()

    def executescript(self, _source):
        raise RuntimeError("scripts must use the Alembic maintenance entry")


def open_db(path: Path, *, create: bool = False) -> Connection:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("SQLite path must be absolute")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise RuntimeError("SQLite database is missing; maintenance upgrade required")
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(path)),
        poolclass=NullPool, connect_args={"timeout": 5, "check_same_thread": True},
    )

    @event.listens_for(engine, "connect")
    def configure(driver, _record):
        driver.isolation_level = None
        cursor = driver.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA journal_mode=WAL" if create else "PRAGMA journal_mode")
            journal = cursor.fetchone()[0]
            values = {}
            for name in ("busy_timeout", "foreign_keys", "synchronous"):
                cursor.execute("PRAGMA " + name)
                values[name] = cursor.fetchone()[0]
            if journal.lower() != "wal" or values != {
                "busy_timeout": 5000, "foreign_keys": 1, "synchronous": 2
            }:
                raise RuntimeError("SQLite connection configuration was not applied")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin(core):
        core.exec_driver_sql("BEGIN IMMEDIATE" if core.info.get("rag_immediate") else "BEGIN DEFERRED")

    try:
        return Connection(engine)
    except BaseException:
        engine.dispose()
        raise


@contextmanager
def transaction(db: Connection, *, immediate: bool = True) -> Iterator[None]:
    db._check()
    if db._explicit or db.core.in_transaction():
        raise RuntimeError("nested or unmanaged SQLite transaction")
    db.core.info["rag_immediate"] = immediate
    db._explicit = True
    try:
        with db.core.begin():
            yield
    finally:
        db._explicit = False
        db.core.info.pop("rag_immediate", None)


def migrate(db: Connection, migrations: Path) -> None:
    # Kept as the public maintenance/test entry. Services call verify_schema.
    from .schema import upgrade_schema
    upgrade_schema(db, migrations)


def verify_schema(db: Connection, migrations: Path) -> None:
    from .schema import verify_current_schema
    verify_current_schema(db, migrations)


def system_state(db: Connection) -> Row:
    row = db.execute("SELECT * FROM system_state WHERE singleton=1").fetchone()
    if row is None:
        raise RuntimeError("system state missing; migrations are incomplete")
    return row


class IdentityStore:
    def __init__(self, db: Connection):
        self.db = db

    def create_task(
        self, *, kind: str, request_key: str, request_sha256: str, budget_scope_id: str
    ) -> tuple[str, str, str]:
        if kind not in {"DOCUMENT", "OCR_SUGGESTION"}:
            raise ValueError("unsupported task kind")
        if len(request_sha256) != 64 or any(c not in "0123456789abcdef" for c in request_sha256):
            raise ValueError("invalid request digest")
        with transaction(self.db):
            state = system_state(self.db)
            if state["verification_status"] != "VERIFIED" or not state["dispatch_open"]:
                raise RuntimeError("business writes are closed during recovery")
            if state["mode"] != "IMPORT":
                raise RuntimeError("document and OCR tasks require IMPORT mode")
            old = self.db.execute(
                "SELECT task_id,current_attempt_id,request_sha256 FROM tasks "
                "WHERE data_instance_id=? AND request_key=?",
                (state["data_instance_id"], request_key),
            ).fetchone()
            if old:
                if old["request_sha256"] != request_sha256:
                    raise ValueError("idempotency key has different request content")
                outbox = self.db.execute(
                    "SELECT outbox_id FROM outbox WHERE attempt_id=?", (old["current_attempt_id"],)
                ).fetchone()
                return old["task_id"], old["current_attempt_id"], outbox["outbox_id"]
            task_id, attempt_id, outbox_id = new_id(), new_id(), new_id()
            timestamp = now()
            self.db.execute(
                "INSERT INTO tasks "
                "(task_id,data_instance_id,kind,request_key,request_sha256,status,"
                "current_attempt_id,stage_budget_scope_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'PENDING',NULL,?,?,?)",
                (task_id, state["data_instance_id"], kind, request_key, request_sha256,
                 budget_scope_id, timestamp, timestamp),
            )
            self.db.execute(
                "INSERT INTO attempts "
                "(attempt_id,task_id,data_instance_id,state,created_at) "
                "VALUES (?,?,?,'PENDING',?)",
                (attempt_id, task_id, state["data_instance_id"], timestamp),
            )
            self.db.execute(
                "UPDATE tasks SET current_attempt_id=? WHERE task_id=?", (attempt_id, task_id)
            )
            self.db.execute(
                "INSERT INTO outbox "
                "(outbox_id,task_id,attempt_id,data_instance_id,state,created_at) "
                "VALUES (?,?,?,?,'PENDING',?)",
                (outbox_id, task_id, attempt_id, state["data_instance_id"], timestamp),
            )
            return task_id, attempt_id, outbox_id

    def reserve_publish(self, outbox_id: str) -> tuple[str, str, str, int]:
        with transaction(self.db):
            state = system_state(self.db)
            row = self.db.execute(
                "SELECT o.*,t.status AS task_status,t.current_attempt_id,"
                "t.recovery_quarantined,t.kind FROM outbox o "
                "JOIN tasks t ON t.task_id=o.task_id WHERE o.outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None or row["data_instance_id"] != state["data_instance_id"]:
                raise RuntimeError("outbox belongs to another data instance")
            if (state["verification_status"] != "VERIFIED" or not state["dispatch_open"]
                    or state["mode"] != "IMPORT" or row["recovery_quarantined"]
                    or row["current_attempt_id"] != row["attempt_id"]
                    or row["task_status"] != "PENDING" or row["state"] != "PENDING"):
                raise RuntimeError("outbox is not eligible to publish")
            if row["publish_count"] >= 3:
                raise RuntimeError("application publish budget exhausted")
            count = row["publish_count"] + 1
            self.db.execute(
                "UPDATE outbox SET state='PUBLISHING',publish_count=?,last_publish_at=? "
                "WHERE outbox_id=?", (count, now(), outbox_id)
            )
            return row["data_instance_id"], row["task_id"], row["attempt_id"], count

    def publish_outcome(self, outbox_id: str, *, confirmed: bool) -> None:
        with transaction(self.db):
            row = self.db.execute("SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
            if row is None or row["state"] != "PUBLISHING":
                raise RuntimeError("outbox is not awaiting a publish outcome")
            self.db.execute(
                "UPDATE outbox SET state=? WHERE outbox_id=?",
                ("PUBLISHED" if confirmed else "UNKNOWN", outbox_id),
            )

    def claim_attempt(
        self, *, data_instance_id: str, task_id: str, attempt_id: str, worker_id: str
    ) -> int | None:
        with transaction(self.db):
            state = system_state(self.db)
            row = self.db.execute(
                "SELECT t.*,a.state AS attempt_state,a.recovery_quarantined AS attempt_quarantined "
                "FROM tasks t JOIN attempts a ON a.task_id=t.task_id "
                "WHERE t.task_id=? AND a.attempt_id=?",
                (task_id, attempt_id),
            ).fetchone()
            if (row is None or state["data_instance_id"] != data_instance_id
                    or row["data_instance_id"] != data_instance_id
                    or state["verification_status"] != "VERIFIED" or not state["dispatch_open"]
                    or state["mode"] != "IMPORT" or row["recovery_quarantined"]
                    or row["attempt_quarantined"] or row["current_attempt_id"] != attempt_id
                    or row["status"] != "PENDING" or row["attempt_state"] != "PENDING" or row["cancel_requested"]):
                return None
            epoch = row["owner_epoch_counter"] + 1
            self.db.execute(
                "UPDATE tasks SET owner_epoch_counter=?,status='RUNNING',updated_at=? WHERE task_id=?",
                (epoch, now(), task_id),
            )
            self.db.execute(
                "UPDATE attempts SET state='RUNNING',owner_epoch=?,worker_id=?,claimed_at=? "
                "WHERE attempt_id=?",
                (epoch, worker_id, now(), attempt_id),
            )
            return epoch

    def register_provider_attempt(
        self, *, task_id: str, attempt_id: str, owner_epoch: int,
        worker_id: str, purpose: str, budget_scope_id: str
    ) -> str:
        with transaction(self.db):
            row = self.db.execute(
                "SELECT t.current_attempt_id,t.data_instance_id,t.recovery_quarantined,"
                "t.status AS task_status,a.state,a.owner_epoch,a.worker_id,"
                "a.recovery_quarantined AS attempt_quarantined "
                "FROM tasks t JOIN attempts a ON a.task_id=t.task_id "
                "WHERE t.task_id=? AND a.attempt_id=?",
                (task_id, attempt_id),
            ).fetchone()
            state = system_state(self.db)
            if (row is None or row["data_instance_id"] != state["data_instance_id"]
                    or state["verification_status"] != "VERIFIED" or not state["dispatch_open"]
                    or row["current_attempt_id"] != attempt_id or row["recovery_quarantined"]
                    or row["attempt_quarantined"] or row["task_status"] != "RUNNING"
                    or row["state"] != "RUNNING" or row["owner_epoch"] != owner_epoch
                    or row["worker_id"] != worker_id):
                raise RuntimeError("provider call has no current execution owner")
            provider_id = new_id()
            self.db.execute(
                "INSERT INTO provider_attempts "
                "(provider_attempt_id,task_id,attempt_id,purpose,budget_scope_id,state,created_at) "
                "VALUES (?,?,?,?,?,'REGISTERED',?)",
                (provider_id, task_id, attempt_id, purpose, budget_scope_id, now()),
            )
            return provider_id

    def begin_recovery(self, *, recovery_id: str) -> str:
        """Install a new data-instance identity before any recovered work opens."""
        with transaction(self.db):
            new_instance = new_id()
            self.db.execute(
                "UPDATE system_state SET data_instance_id=?,recovery_id=?,"
                "verification_status='VERIFYING',dispatch_open=0,mode='IMPORT',updated_at=? "
                "WHERE singleton=1",
                (new_instance, recovery_id, now()),
            )
            self.db.execute(
                "UPDATE tasks SET recovery_quarantined=1 WHERE status IN ('PENDING','RUNNING')"
            )
            self.db.execute(
                "UPDATE attempts SET recovery_quarantined=1 "
                "WHERE state IN ('PENDING','RUNNING')"
            )
            self.db.execute(
                "UPDATE outbox SET recovery_quarantined=1 WHERE state IN (\'PENDING\',\'PUBLISHING\',\'PUBLISHED\',\'UNKNOWN\')"
            )
            return new_instance
