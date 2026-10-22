"""Mode transitions share the producer-call lock; no query generation is invented."""
from contextlib import closing
import httpx

from .config import Settings
from .db import now, open_db, system_state, transaction
from .dispatch_gate import send_gate
from .ingestion.store import BusinessError, event, require_instance, require_kb
from .worker_runtime import process_liveness

def maybe_finish_mode(settings):
    with send_gate(settings), closing(open_db(settings.sqlite)) as db, transaction(db):
        state = system_state(db)
        if state["mode"] != "DRAINING" or state["verification_status"] != "VERIFIED":
            return dict(state)
        if db.execute("SELECT 1 FROM tasks WHERE data_instance_id=? AND status=\'RUNNING\' AND recovery_quarantined=0 LIMIT 1", (state["data_instance_id"],)).fetchone():
            return dict(state)
        workers = db.execute("SELECT w.* FROM worker_current c JOIN worker_sessions w ON w.worker_id=c.worker_id WHERE w.data_instance_id=?", (state["data_instance_id"],)).fetchall()
        for worker in workers:
            liveness = process_liveness(worker["pid"], worker["process_created_at"])
            if liveness == "UNKNOWN" or (liveness == "LIVE" and (worker["state"] != "IDLE" or worker["active_task_id"] is not None)):
                return dict(state)
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                response = client.get(f"http://{settings.model_host}:{settings.model_port}/health/live")
            if response.status_code != 200 or len(response.content) > 32768:
                return dict(state)
            observed = response.json()
            if (observed.get("ready") is not True or observed.get("compute_inflight") is not False
                    or observed.get("completion_unconfirmed") is not False
                    or observed.get("active_operation_id") is not None or observed.get("waiting_count") != 0):
                return dict(state)
        except (httpx.HTTPError, ValueError, AttributeError):
            return dict(state)
        db.execute("UPDATE system_state SET mode=\'QUERY\',updated_at=? WHERE singleton=1", (now(),))
        return dict(system_state(db))

def request_mode(settings: Settings, instance, request):
    with send_gate(settings), closing(open_db(settings.sqlite)) as db, transaction(db):
        state = require_instance(db, instance, importing=False)
        if request.mode == "QUERY":
            if request.kb_id is None:
                raise BusinessError("QUERY_KNOWLEDGE_BASE_REQUIRED", "请选择可问答知识库。", 422)
            kb = require_kb(db, str(request.kb_id))
            if kb["current_generation_id"] is None:
                raise BusinessError("QUERY_NOT_READY", "该知识库尚无已发布索引；归档完成不能代替可问答版本。")
            from .indexing.cleanup import require_query_generation
            require_query_generation(db, str(request.kb_id), kb["current_generation_id"])
            db.execute("UPDATE system_state SET mode=\'DRAINING\',updated_at=? WHERE singleton=1", (now(),))
            pending = db.execute("SELECT task_id,current_attempt_id FROM tasks WHERE data_instance_id=? AND kind=\'OCR_SUGGESTION\' AND status=\'PENDING\' AND recovery_quarantined=0", (instance,)).fetchall()
            for task in pending:
                db.execute("UPDATE tasks SET status=\'CANCELLED\',cancel_requested=1,updated_at=? WHERE task_id=?", (now(), task["task_id"]))
                db.execute("UPDATE attempts SET state=\'CANCELLED\',finished_at=? WHERE attempt_id=? AND state=\'PENDING\'", (now(), task["current_attempt_id"]))
                db.execute("UPDATE outbox SET state=\'CANCELLED\' WHERE attempt_id=?", (task["current_attempt_id"],))
                event(db, task["task_id"], task["current_attempt_id"], "MODE_CANCELLED_UNSENT_OCR", {"cloud_cancelled": False, "automatic_revival": False})
        else:
            active = db.execute("SELECT 1 FROM retrieval_requests WHERE data_instance_id=? "
                                "AND state IN ('RUNNING','CANCELLING') LIMIT 1", (instance,)).fetchone()
            reader = db.execute("SELECT 1 FROM generation_readers WHERE data_instance_id=? "
                                "AND state='OPEN' LIMIT 1", (instance,)).fetchone()
            if active or reader:
                raise BusinessError("QUERY_STILL_ACTIVE", "请先结束问答及索引读取，并确认本地计算停止后切换导入。")
            db.execute("UPDATE system_state SET mode=\'IMPORT\',updated_at=? WHERE singleton=1", (now(),))
        result = dict(system_state(db))
    return maybe_finish_mode(settings) if result["mode"] == "DRAINING" else result
