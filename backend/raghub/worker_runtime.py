"""Actual solo Worker sessions; heartbeat is distinct from business progress."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from contextlib import closing

import psutil

from .config import Settings
from .process_identity import process_start_time
from .db import verify_schema, new_id, now, open_db, system_state, transaction

LOG = logging.getLogger(__name__)
TASK_BY_ROLE = {"document-worker": "raghub.prepare_document", "ocr-worker": "raghub.ocr_suggestion"}


def fail_closed(where: str, exc: BaseException) -> None:
    # Celery captures signal receiver exceptions; explicit exit prevents a
    # failed identity signal from silently allowing business execution.
    try:
        LOG.critical("worker identity failed at %s", where, exc_info=(type(exc), exc, exc.__traceback__))
    finally:
        os._exit(70)


def process_liveness(pid, created) -> str:
    try:
        if pid <= 0 or not math.isfinite(created):
            return "UNKNOWN"
        process = psutil.Process(pid)
        if process_start_time(process) != created or process.status() == psutil.STATUS_ZOMBIE:
            return "STOPPED"
        return "LIVE" if process.is_running() else "STOPPED"
    except psutil.NoSuchProcess:
        return "STOPPED"
    except (psutil.AccessDenied, OSError, ValueError):
        return "UNKNOWN"


class WorkerRuntime:
    def __init__(self, settings: Settings, role: str, hostname: str):
        if role not in TASK_BY_ROLE or hostname != role + "@raghub":
            raise ValueError("worker role/hostname mismatch")
        if os.environ.get("RAG_SERVICE_ROLE", role) != role:
            raise ValueError("declared worker role mismatch")
        self.settings, self.role = settings, role
        self.worker_id, self.pid = new_id(), os.getpid()
        self.process_created_at = process_start_time(psutil.Process(self.pid))
        self.launch_id = os.environ.get("RAG_SERVICE_LAUNCH_ID") or new_id()
        self.instance = None
        self.active_task_id = None
        self.delivery_id = None
        self.stop_event = threading.Event()
        self.guard = threading.RLock()
        self.thread = None

    def start(self):
        with closing(open_db(self.settings.sqlite)) as db:
            verify_schema(db, self.settings.project / "backend/migrations")
            with transaction(db):
                state = system_state(db)
                if state["verification_status"] != "VERIFIED":
                    raise RuntimeError("worker data instance is not verified")
                prior = db.execute(
                    "SELECT w.* FROM worker_current c JOIN worker_sessions w ON w.worker_id=c.worker_id WHERE c.role=?",
                    (self.role,),
                ).fetchone()
                if prior and process_liveness(prior["pid"], prior["process_created_at"]) != "STOPPED":
                    raise RuntimeError("previous worker is live or unconfirmed")
                if prior:
                    db.execute("UPDATE worker_sessions SET state='STOPPED',state_epoch=state_epoch+1 WHERE worker_id=?",
                               (prior["worker_id"],))
                self.instance = state["data_instance_id"]
                db.execute(
                    "INSERT INTO worker_sessions(worker_id,data_instance_id,role,pid,process_created_at,launch_id,"
                    "state,state_epoch,active_task_id,heartbeat_at,started_at) VALUES (?,?,?,?,?,?,'STARTING',0,NULL,?,?)",
                    (self.worker_id, self.instance, self.role, self.pid, self.process_created_at, self.launch_id,
                     time.time(), now()),
                )
                db.execute("INSERT INTO worker_current(role,worker_id) VALUES (?,?) "
                           "ON CONFLICT(role) DO UPDATE SET worker_id=excluded.worker_id", (self.role, self.worker_id))
        self.thread = threading.Thread(target=self.heartbeat, name=self.role + "-heartbeat", daemon=True)
        self.thread.start()

    def assert_owner(self, db):
        current = db.execute("SELECT worker_id FROM worker_current WHERE role=?", (self.role,)).fetchone()
        if system_state(db)["data_instance_id"] != self.instance or not current or current["worker_id"] != self.worker_id:
            raise RuntimeError("worker session is no longer current")

    def transition(self, allowed, target, active):
        with closing(open_db(self.settings.sqlite)) as db, transaction(db):
            self.assert_owner(db)
            row = db.execute("SELECT state,active_task_id FROM worker_sessions WHERE worker_id=?", (self.worker_id,)).fetchone()
            if not row or row["state"] not in allowed or row["active_task_id"] != self.active_task_id:
                raise RuntimeError("unexpected worker transition")
            db.execute("UPDATE worker_sessions SET state=?,active_task_id=?,state_epoch=state_epoch+1,heartbeat_at=? "
                       "WHERE worker_id=?", (target, active, time.time(), self.worker_id))

    def ready(self):
        with self.guard:
            self.transition(("STARTING",), "IDLE", None)

    def prerun(self, delivery, task_name, args, kwargs):
        with self.guard:
            if task_name != TASK_BY_ROLE[self.role] or self.delivery_id is not None:
                raise RuntimeError("incorrect task route or overlapping solo delivery")
            if not delivery or not isinstance(args, (list, tuple)) or len(args) != 3 or kwargs:
                raise RuntimeError("message must contain only instance/task/attempt IDs")
            if any(not isinstance(value, str) or not value for value in args):
                raise RuntimeError("invalid business message IDs")
            self.transition(("IDLE",), "BUSY", args[1])
            self.delivery_id, self.active_task_id = delivery, args[1]

    def postrun(self, delivery):
        with self.guard:
            if delivery != self.delivery_id:
                raise RuntimeError("delivery result identity mismatch")
            self.transition(("BUSY",), "IDLE", None)
            self.delivery_id, self.active_task_id = None, None

    def heartbeat(self):
        while not self.stop_event.wait(2):
            try:
                with closing(open_db(self.settings.sqlite)) as db, transaction(db):
                    self.assert_owner(db)
                    changed = db.execute("UPDATE worker_sessions SET heartbeat_at=? WHERE worker_id=? AND state!='STOPPED'",
                                         (time.time(), self.worker_id)).rowcount
                    if changed != 1:
                        raise RuntimeError("heartbeat session missing")
            except Exception as exc:
                fail_closed("heartbeat", exc)

    def shutdown(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        with self.guard:
            self.transition(("STARTING", "IDLE", "BUSY"), "STOPPED", self.active_task_id)
