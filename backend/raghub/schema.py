"""Alembic upgrade bridge, with exact legacy-ledger/schema verification."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event

from .db import new_id, now, transaction

LEGACY_FILES = ("001_initial.sql", "002_ingestion.sql", "003_parsing.sql")
LEDGER_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations "
    "(version INTEGER PRIMARY KEY, source_sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)"
)


def legacy_sources(migrations):
    return [(i, path, path.read_bytes()) for i, name in enumerate(LEGACY_FILES, 1)
            for path in [Path(migrations) / name]]


def statements(source):
    # SQLite's complete_statement understands quoted semicolons and triggers.
    pending = ""
    for char in source:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise RuntimeError("incomplete legacy SQL statement")


def config_for(core, migrations):
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic").replace("%", "%%"))
    cfg.attributes["connection"] = core
    cfg.attributes["legacy_migrations"] = Path(migrations)
    return cfg


def apply_legacy(core, migrations, version):
    source = legacy_sources(migrations)[version - 1][2]
    if version == 1:
        core.exec_driver_sql(LEDGER_SQL)
    for statement in statements(source.decode("utf-8")):
        core.exec_driver_sql(statement)
    core.exec_driver_sql(
        "INSERT INTO schema_migrations(version,source_sha256,applied_at) VALUES (?,?,?)",
        (version, hashlib.sha256(source).hexdigest(), now()),
    )


def schema_objects(core):
    return [tuple(row) for row in core.exec_driver_sql(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE ? AND name!=? ORDER BY type,name",
        ("sqlite_%", "alembic_version"),
    ).fetchall()]


def expected_objects(migrations, revision, *, legacy=False):
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def configure(driver, _record):
        driver.isolation_level = None

    @event.listens_for(engine, "begin")
    def begin(core):
        core.exec_driver_sql("BEGIN DEFERRED")

    try:
        with engine.begin() as core:
            if legacy:
                for version in range(1, int(revision.rsplit("_", 1)[1]) + 1):
                    apply_legacy(core, migrations, version)
            else:
                command.upgrade(config_for(core, migrations), revision)
            return schema_objects(core)
    finally:
        engine.dispose()


def check_ledger(core, migrations):
    rows = [tuple(row) for row in core.exec_driver_sql(
        "SELECT version,source_sha256 FROM schema_migrations ORDER BY version"
    ).fetchall()]
    sources = legacy_sources(migrations)
    if not rows or len(rows) > len(sources):
        raise RuntimeError("unsupported legacy migration ledger")
    expected = [(i, hashlib.sha256(source).hexdigest()) for i, _path, source in sources[:len(rows)]]
    if rows != expected:
        raise RuntimeError("legacy migration ledger/source mismatch")
    return len(rows)


def current_revision(core):
    rows = core.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall()
    if len(rows) != 1:
        raise RuntimeError("Alembic must have exactly one current revision")
    return rows[0][0]


def upgrade_schema(db, migrations):
    """Called only by the manager maintenance entry or isolated DB tests."""
    with transaction(db):
        objects = schema_objects(db.core)
        names = {row[1] for row in objects if row[0] == "table"}
        empty = not objects
        cfg = config_for(db.core, migrations)
        has_alembic = db.core.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", ("table", "alembic_version")
        ).fetchone() is not None
        if empty and not has_alembic:
            command.upgrade(cfg, "head")
        else:
            if "schema_migrations" not in names or "system_state" not in names:
                raise RuntimeError("existing database has no supported legacy baseline")
            if db.core.exec_driver_sql(
                "SELECT data_instance_id FROM system_state WHERE singleton=1"
            ).fetchone() is None:
                raise RuntimeError("existing database has no data-instance identity")
            version = check_ledger(db.core, migrations)
            revision = current_revision(db.core) if has_alembic else f"legacy_{version:03}"
            expected = expected_objects(migrations, revision, legacy=not has_alembic)
            if objects != expected:
                raise RuntimeError("actual schema differs from exact migration baseline")
            if not has_alembic:
                # Stamp only the precise, proven predecessor; never stamp head.
                command.stamp(cfg, revision)
            command.upgrade(cfg, "head")
        if empty:
            db.core.exec_driver_sql(
                "INSERT INTO system_state "
                "(singleton,data_instance_id,recovery_id,verification_status,dispatch_open,mode,updated_at) "
                "VALUES (1,?,NULL,'VERIFIED',1,'IMPORT',?)", (new_id(), now()),
            )
    verify_current_schema(db, migrations)


def verify_current_schema(db, migrations):
    """Read existing state; never creates, stamps, upgrades or repairs it."""
    with transaction(db, immediate=False):
        core = db.core
        cfg = config_for(core, migrations)
        head = ScriptDirectory.from_config(cfg).get_current_head()
        if current_revision(core) != head or check_ledger(core, migrations) != len(LEGACY_FILES):
            raise RuntimeError("database upgrade required before starting services")
        if schema_objects(core) != expected_objects(migrations, head):
            raise RuntimeError("current SQLite schema does not match Alembic head")
        if core.exec_driver_sql("SELECT 1 FROM system_state WHERE singleton=1").fetchone() is None:
            raise RuntimeError("database identity is missing")


def backup_database(db, directory):
    """Consistent native backup only; business transactions remain Core-owned."""
    import json
    import os
    db._check()
    if db.core.in_transaction():
        raise RuntimeError("backup requires no active business transaction")
    directory = Path(directory)
    name = "sqlite-before-upgrade-" + new_id()
    target, record = directory / (name + ".sqlite3"), directory / (name + ".json")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(str(target))) as destination:
            db.core.connection.driver_connection.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("backup integrity check failed")
            if destination.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("backup foreign-key check failed")
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        value = {
            "schema": "raghub.sqlite-backup.v1", "backup_path": str(target),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "size_bytes": target.stat().st_size,
            "data_instance_id": db.execute(
                "SELECT data_instance_id FROM system_state WHERE singleton=1"
            ).fetchone()[0],
        }
        with record.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return value
    except BaseException:
        # Preserve every failed backup byte for diagnosis.
        raise


def maintenance_upgrade(db, migrations, backup_directory):
    cfg = config_for(db.core, migrations)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    has_version = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", ("table", "alembic_version")
    ).fetchone() is not None
    current = db.execute("SELECT version_num FROM alembic_version").fetchall() if has_version else []
    has_identity = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", ("table", "system_state")
    ).fetchone() is not None
    backup = None
    if has_identity and (len(current) != 1 or current[0][0] != head):
        backup = backup_database(db, backup_directory)
    upgrade_schema(db, migrations)
    return {"revision": head, "backup": backup, "sqlite_version": sqlite3.sqlite_version}
