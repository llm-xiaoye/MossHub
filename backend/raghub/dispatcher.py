"""One durable publisher; UNKNOWN occupies the same unclaimed logical slot."""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import time
import traceback
from contextlib import closing

from celery import Celery
from kombu import Exchange, Queue

from .config import Settings
from .db import verify_schema, new_id, now, open_db, system_state, transaction
from .ingestion.store import event
from .process_locks import service_role
from .dispatch_gate import send_gate
from .worker_runtime import process_liveness

LOG = logging.getLogger(__name__)
POLL_SECONDS, IDLE_SECONDS = 2.0, 30.0
# Conservative starting implementation values, not measured optima.
HEARTBEAT_MAX_AGE, MAX_OBSERVATION_GAP = 6.0, 4.0
ROUTES = {"DOCUMENT": ("document-worker", "document", "raghub.prepare_document"),
          "OCR_SUGGESTION": ("ocr-worker", "ocr", "raghub.ocr_suggestion")}
running = True


def stop(_signum, _frame):
    global running
    running = False


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Dispatcher:
    def __init__(self, settings):
        self.settings = settings
        self.instance = None
        self.app = Celery("raghub.dispatcher", broker=settings.redis_url, backend=None)
        self.app.conf.update(
            task_publish_retry=False, task_serializer="json", accept_content=["json"],
            task_ignore_result=True, task_create_missing_queues=False,
            task_default_queue="document", task_default_exchange="raghub.document",
            task_default_routing_key="document",
            task_queues=(Queue("document", Exchange("raghub.document", type="direct"), routing_key="document"),
                         Queue("ocr", Exchange("raghub.ocr", type="direct"), routing_key="ocr")),
        )

    def initialize(self):
        with closing(open_db(self.settings.sqlite)) as db:
            verify_schema(db, self.settings.project / "backend/migrations")
            with transaction(db):
                state = system_state(db)
                self.instance = state["data_instance_id"]
                if state["verification_status"] != "VERIFIED":
                    raise RuntimeError("dispatcher data is not verified")
                for row in db.execute("SELECT * FROM outbox WHERE data_instance_id=? AND recovery_quarantined=0 "
                                      "AND state='PUBLISHING'", (self.instance,)).fetchall():
                    record = db.execute("SELECT * FROM publish_observations WHERE outbox_id=? AND publish_count=?",
                                        (row["outbox_id"], row["publish_count"])).fetchone()
                    if not record or record["state"] != "RESERVED":
                        raise RuntimeError("unconfirmed send has no durable reservation")
                    details = {"reservation": json.loads(record["details_json"]),
                               "producer_result": {"state": "UNKNOWN", "reason": "DISPATCHER_RESTART_UNCONFIRMED"}}
                    db.execute("UPDATE publish_observations SET state='UNKNOWN',details_json=? WHERE publish_id=?",
                               (encoded(details), record["publish_id"]))
                    db.execute("UPDATE outbox SET state='UNKNOWN' WHERE outbox_id=?", (row["outbox_id"],))
                # Restart cannot claim that unobserved downtime was idle time.
                db.execute("UPDATE outbox SET last_observed_worker_id=NULL,last_observed_state_epoch=NULL,"
                           "last_observed_at=NULL,idle_seconds=0 WHERE data_instance_id=?", (self.instance,))
        LOG.info("dispatch poll=2 idle=30; starting heartbeat freshness=6 observation gap=4 seconds")

    def eligible(self, db, kind, sent, limit):
        selector = "o.publish_count>0" if sent else "o.publish_count=0 AND o.state='PENDING'"
        return db.execute(
            "SELECT o.* FROM outbox o JOIN tasks t ON t.task_id=o.task_id JOIN attempts a ON a.attempt_id=o.attempt_id "
            "WHERE o.data_instance_id=? AND t.data_instance_id=o.data_instance_id AND a.data_instance_id=o.data_instance_id "
            "AND a.task_id=t.task_id AND t.kind=? AND t.current_attempt_id=a.attempt_id "
            "AND t.status='PENDING' AND a.state='PENDING' AND t.cancel_requested=0 "
            "AND o.recovery_quarantined=0 AND t.recovery_quarantined=0 AND a.recovery_quarantined=0 "
            "AND o.state IN ('PENDING','PUBLISHING','PUBLISHED','UNKNOWN') AND " + selector +
            " ORDER BY o.created_at,o.outbox_id LIMIT ?", (self.instance, kind, limit),
        ).fetchall()

    def worker(self, db, role, current_time):
        row = db.execute("SELECT w.* FROM worker_current c JOIN worker_sessions w ON w.worker_id=c.worker_id "
                         "WHERE c.role=?", (role,)).fetchone()
        if not row or row["data_instance_id"] != self.instance or row["state"] not in {"IDLE", "BUSY"}:
            return None
        if not 0 <= current_time - row["heartbeat_at"] <= HEARTBEAT_MAX_AGE:
            return None
        return row if process_liveness(row["pid"], row["process_created_at"]) == "LIVE" else None

    def recovery_basis(self, db, row, worker, current_time):
        if worker is None:
            db.execute("UPDATE outbox SET last_observed_state_epoch=NULL,last_observed_at=NULL WHERE outbox_id=?",
                       (row["outbox_id"],))
            return None
        if row["published_worker_id"] and row["published_worker_id"] != worker["worker_id"]:
            old = db.execute("SELECT * FROM worker_sessions WHERE worker_id=?", (row["published_worker_id"],)).fetchone()
            if old and old["role"] == worker["role"] and process_liveness(old["pid"], old["process_created_at"]) == "STOPPED":
                return "OLD_EXECUTOR_STOPPED_NEW_EXECUTOR_READY"
        # Preserve previously confirmed idle intervals while busy/unknown; only
        # instance changes, new sends and dispatcher restart reset the budget.
        total = row["idle_seconds"]
        if row["last_observed_worker_id"] not in {None, worker["worker_id"]}:
            total = 0.0
        elapsed = None
        if (worker["state"] == "IDLE" and row["last_observed_worker_id"] == worker["worker_id"]
                and row["last_observed_state_epoch"] == worker["state_epoch"] and row["last_observed_at"] is not None):
            elapsed = current_time - row["last_observed_at"]
        if elapsed is not None and 0 <= elapsed <= MAX_OBSERVATION_GAP:
            total += elapsed
        idle = worker["state"] == "IDLE"
        db.execute("UPDATE outbox SET last_observed_worker_id=?,last_observed_state_epoch=?,"
                   "last_observed_at=?,idle_seconds=? WHERE outbox_id=?",
                   (worker["worker_id"], worker["state_epoch"] if idle else None,
                    current_time if idle else None, total, row["outbox_id"]))
        return "CONFIRMED_IDLE_INTERVALS" if idle and total >= IDLE_SECONDS else None

    def exhaust_budget(self, db, row, basis):
        current = db.execute(
            "SELECT o.* FROM outbox o JOIN tasks t ON t.task_id=o.task_id "
            "JOIN attempts a ON a.attempt_id=o.attempt_id AND a.task_id=t.task_id "
            "JOIN system_state s ON s.singleton=1 AND s.data_instance_id=o.data_instance_id "
            "WHERE o.outbox_id=? AND o.data_instance_id=? AND o.publish_count=? AND o.state=? "
            "AND o.publish_count>=3 AND s.verification_status='VERIFIED' "
            "AND t.data_instance_id=o.data_instance_id AND a.data_instance_id=o.data_instance_id "
            "AND t.current_attempt_id=a.attempt_id AND t.status='PENDING' AND a.state='PENDING' "
            "AND t.cancel_requested=0 AND o.recovery_quarantined=0 "
            "AND t.recovery_quarantined=0 AND a.recovery_quarantined=0",
            (row["outbox_id"], self.instance, row["publish_count"], row["state"]),
        ).fetchone()
        if current is None:
            return False
        observation = db.execute(
            "SELECT state FROM publish_observations WHERE outbox_id=? AND publish_count=?",
            (current["outbox_id"], current["publish_count"]),
        ).fetchone()
        expected = {"PENDING": "NOT_SENT", "PUBLISHED": "CONFIRMED", "UNKNOWN": "UNKNOWN"}
        if observation is None or observation["state"] != expected.get(current["state"]):
            raise RuntimeError("exhausted publication has no matching producer outcome")
        producer_state = observation["state"]
        details = {"code": "PUBLISH_BUDGET_EXHAUSTED",
                   "message": "本次处理尝试的三次应用发布预约预算已耗尽。请查看最后一次发布结果，再手动发起恢复。",
                   "publish_count": current["publish_count"], "recovery_basis": basis,
                   "delivery_state": "UNCLAIMED", "last_publication_state": producer_state,
                   "automatic_execution_retry": False}
        task_update = db.execute("UPDATE tasks SET status='FAILED',last_error_json=?,updated_at=? "
                                 "WHERE task_id=? AND current_attempt_id=? AND status='PENDING'",
                                 (encoded(details), now(), current["task_id"], current["attempt_id"]))
        attempt_update = db.execute("UPDATE attempts SET state='FAILED',finished_at=? WHERE attempt_id=? AND state='PENDING'",
                                    (now(), current["attempt_id"]))
        if task_update.rowcount != 1 or attempt_update.rowcount != 1:
            raise RuntimeError("publication exhaustion terminal update differs")
        # NOT_SENT describes this reservation, not all prior sends. Keep the
        # outcome ledger/count; UNKNOWN still follows its existing idle gate.
        if current["state"] == "PENDING":
            outbox_update = db.execute("UPDATE outbox SET state='CANCELLED' WHERE outbox_id=? AND state='PENDING'",
                                       (current["outbox_id"],))
            if outbox_update.rowcount != 1:
                raise RuntimeError("unsent exhaustion outbox update differs")
        event(db, current["task_id"], current["attempt_id"], "PUBLISH_BUDGET_EXHAUSTED", details)
        return True

    def recover_unclaimed(self, db, row, worker, current_time):
        if row["state"] not in {"PUBLISHED", "UNKNOWN"}:
            return
        basis = self.recovery_basis(db, row, worker, current_time)
        if basis is None:
            return
        if row["publish_count"] >= 3:
            self.exhaust_budget(db, row, basis)
            return
        db.execute("UPDATE outbox SET state='PENDING',last_observed_worker_id=NULL,last_observed_state_epoch=NULL,"
                   "last_observed_at=NULL,idle_seconds=0 WHERE outbox_id=?", (row["outbox_id"],))
        event(db, row["task_id"], row["attempt_id"], "UNCLAIMED_REPUBLISH_ELIGIBLE", {"basis": basis})

    def mark_interrupted(self, db):
        rows = db.execute("SELECT t.task_id,a.attempt_id,a.worker_id,a.owner_epoch FROM tasks t "
                          "JOIN attempts a ON a.attempt_id=t.current_attempt_id WHERE t.data_instance_id=? "
                          "AND a.data_instance_id=t.data_instance_id AND t.status='RUNNING' AND a.state='RUNNING' "
                          "AND t.recovery_quarantined=0 AND a.recovery_quarantined=0", (self.instance,)).fetchall()
        for row in rows:
            worker = db.execute("SELECT * FROM worker_sessions WHERE worker_id=?", (row["worker_id"],)).fetchone()
            if not worker or process_liveness(worker["pid"], worker["process_created_at"]) != "STOPPED":
                continue
            details = {"code": "EXECUTOR_STOPPED_AFTER_CLAIM", "worker_id": row["worker_id"],
                       "pid": worker["pid"], "process_created_at": worker["process_created_at"],
                       "automatic_execution_retry": False}
            db.execute("UPDATE tasks SET status='INTERRUPTED',last_error_json=?,updated_at=? "
                       "WHERE task_id=? AND current_attempt_id=? AND owner_epoch_counter=? AND status='RUNNING'",
                       (encoded(details), now(), row["task_id"], row["attempt_id"], row["owner_epoch"]))
            db.execute("UPDATE attempts SET state='INTERRUPTED',finished_at=? WHERE attempt_id=? AND state='RUNNING'",
                       (now(), row["attempt_id"]))
            event(db, row["task_id"], row["attempt_id"], "EXECUTOR_STOPPED", details)

    def reserve(self, db, kind, row, worker):
        _role, queue, task_name = ROUTES[kind]
        if row["state"] == "PENDING" and row["publish_count"] >= 3:
            self.exhaust_budget(db, row, "RESERVATION_BUDGET_EXHAUSTED")
            return None
        if row["state"] != "PENDING" or not 0 <= row["publish_count"] < 3:
            raise RuntimeError("invalid application publication reservation")
        publish_id, count = new_id(), row["publish_count"] + 1
        db.execute("UPDATE outbox SET state='PUBLISHING',publish_count=?,last_publish_at=?,published_worker_id=?,"
                   "last_observed_worker_id=NULL,last_observed_state_epoch=NULL,last_observed_at=NULL,idle_seconds=0 "
                   "WHERE outbox_id=?", (count, now(), worker["worker_id"], row["outbox_id"]))
        job = {"outbox_id": row["outbox_id"], "publish_id": publish_id, "publish_count": count,
               "data_instance_id": self.instance, "task_id": row["task_id"], "attempt_id": row["attempt_id"],
               "worker_id": worker["worker_id"], "queue": queue, "task_name": task_name, "kind": kind, "retry": False}
        db.execute("INSERT INTO publish_observations(publish_id,outbox_id,publish_count,state,details_json,created_at) "
                   "VALUES (?,?,?,'RESERVED',?,?)", (publish_id, row["outbox_id"], count, encoded(job), now()))
        event(db, row["task_id"], row["attempt_id"], "PUBLISH_RESERVED", job)
        return job

    def tick(self):
        jobs, current_time = [], time.time()
        with closing(open_db(self.settings.sqlite)) as db, transaction(db):
            state = system_state(db)
            if state["data_instance_id"] != self.instance:
                raise RuntimeError("dispatcher belongs to a former instance")
            if state["verification_status"] != "VERIFIED":
                return jobs
            self.mark_interrupted(db)
            if state["mode"] != "IMPORT" or not state["dispatch_open"]:
                db.execute("UPDATE outbox SET last_observed_state_epoch=NULL,last_observed_at=NULL "
                           "WHERE data_instance_id=?", (self.instance,))
                return jobs
            for kind, (role, _queue, _task) in ROUTES.items():
                slots = self.eligible(db, kind, True, 2)
                if len(slots) > 1:
                    raise RuntimeError("queue has more than one sent unclaimed logical attempt")
                worker = self.worker(db, role, current_time)
                if slots:
                    self.recover_unclaimed(db, slots[0], worker, current_time)
                    slots = self.eligible(db, kind, True, 2)
                if slots:
                    candidate = slots[0]
                    if candidate["state"] != "PENDING":
                        continue
                else:
                    fresh = self.eligible(db, kind, False, 1)
                    if not fresh:
                        continue
                    candidate = fresh[0]
                if worker is not None:
                    job = self.reserve(db, kind, candidate, worker)
                    if job is not None:
                        jobs.append(job)
        return jobs

    def publish(self, job):
        with send_gate(self.settings):
            self._publish_locked(job)

    def _publish_locked(self, job):
        role, queue, task_name = ROUTES[job["kind"]]
        if queue != job["queue"] or task_name != job["task_name"] or job["retry"] is not False:
            raise RuntimeError("publication route differs from the reservation")
        with closing(open_db(self.settings.sqlite)) as db, transaction(db):
            rows = db.execute(
                "SELECT o.outbox_id FROM publish_observations po JOIN outbox o ON o.outbox_id=po.outbox_id "
                "JOIN tasks t ON t.task_id=o.task_id JOIN attempts a ON a.attempt_id=o.attempt_id AND a.task_id=t.task_id "
                "JOIN system_state s ON s.singleton=1 AND s.data_instance_id=o.data_instance_id "
                "WHERE po.publish_id=? AND po.publish_count=? AND po.state=\'RESERVED\' "
                "AND o.outbox_id=? AND o.publish_count=? AND o.state=\'PUBLISHING\' "
                "AND o.data_instance_id=? AND o.task_id=? AND o.attempt_id=? "
                "AND t.data_instance_id=o.data_instance_id AND a.data_instance_id=o.data_instance_id "
                "AND t.kind=? AND t.current_attempt_id=a.attempt_id AND t.status=\'PENDING\' AND a.state=\'PENDING\' "
                "AND o.recovery_quarantined=0 AND t.recovery_quarantined=0 AND a.recovery_quarantined=0 "
                "AND t.cancel_requested=0 AND s.verification_status=\'VERIFIED\' AND s.dispatch_open=1 AND s.mode=\'IMPORT\'",
                (job["publish_id"], job["publish_count"], job["outbox_id"], job["publish_count"],
                 job["data_instance_id"], job["task_id"], job["attempt_id"], job["kind"]),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError("publication guard cardinality differs")
            record = db.execute("SELECT details_json FROM publish_observations WHERE publish_id=?", (job["publish_id"],)).fetchone()
            if record is None or json.loads(record[0]) != job:
                raise RuntimeError("publication reservation bytes differ")
            eligible = len(rows) == 1
        if not eligible:
            outcome = {"state": "NOT_SENT", "send_started": False, "reason": "PRE_SEND_REVALIDATION_REJECTED"}
        else:
            try:
                result = self.app.signature(job["task_name"], args=[job["data_instance_id"], job["task_id"],
                                            job["attempt_id"]]).apply_async(queue=job["queue"], routing_key=job["queue"], retry=False)
                outcome = {"state": "CONFIRMED", "send_started": True, "celery_message_id": result.id,
                           "producer_call_returned": True, "worker_delivery_observed": False}
            except Exception as exc:
                outcome = {"state": "UNKNOWN", "send_started": True, "error_type": type(exc).__name__, "error": str(exc),
                           "traceback": traceback.format_exc(), "worker_delivery_observed": False}
        LOG.info("producer result %s", encoded({"job": job, "outcome": outcome}))
        with closing(open_db(self.settings.sqlite)) as db, transaction(db):
            observed = db.execute("SELECT * FROM publish_observations WHERE publish_id=?", (job["publish_id"],)).fetchone()
            if not observed or observed["state"] != "RESERVED" or observed["publish_count"] != job["publish_count"]:
                raise RuntimeError("producer result binding changed")
            db.execute("UPDATE publish_observations SET state=?,details_json=? WHERE publish_id=?",
                       (outcome["state"], encoded({"reservation": job, "producer_result": outcome}), job["publish_id"]))
            db.execute("UPDATE outbox SET state=? WHERE outbox_id=? AND data_instance_id=? AND publish_count=? "
                       "AND state='PUBLISHING' AND recovery_quarantined=0",
                       ("PUBLISHED" if outcome["state"] == "CONFIRMED" else "PENDING" if outcome["state"] == "NOT_SENT" else "UNKNOWN",
                        job["outbox_id"], job["data_instance_id"], job["publish_count"]))
            event(db, job["task_id"], job["attempt_id"], "PUBLISH_RESULT", outcome)
            if outcome["state"] == "NOT_SENT" and job["publish_count"] >= 3:
                row = db.execute("SELECT * FROM outbox WHERE outbox_id=? AND data_instance_id=?",
                                 (job["outbox_id"], job["data_instance_id"])).fetchone()
                if row is not None and row["state"] == "PENDING":
                    self.exhaust_budget(db, row, "PRE_SEND_REVALIDATION_REJECTED")

    def poll_once(self):
        # tick commits or rolls back all reservations before any producer call.
        try:
            jobs=self.tick()
        except Exception as exc:
            original=getattr(exc,"orig",exc)
            code=getattr(original,"sqlite_errorcode",None)
            if isinstance(original,sqlite3.OperationalError) and isinstance(code,int) and (code & 255) in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED}:
                LOG.warning("dispatcher reservation deferred: SQLite busy; no producer call")
                return False
            raise
        # Never retry here after a send or an unknown producer/finalization result.
        for job in jobs:
            self.publish(job)
        from .mode import maybe_finish_mode
        maybe_finish_mode(self.settings)
        return True

    def run(self):
        self.initialize()
        while running:
            self.poll_once()
            time.sleep(POLL_SECONDS)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    settings = Settings.load()
    with service_role(settings, "dispatcher"):
        dispatcher = Dispatcher(settings)
        try:
            dispatcher.run()
        finally:
            dispatcher.app.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
