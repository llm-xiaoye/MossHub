"""Celery process identity and explicit queues.

Stage 2 adds document handlers. No unknown task is acknowledged as work done.
"""

from celery import Celery
from celery.signals import worker_init, worker_shutdown, worker_ready, task_prerun, task_postrun
from kombu import Exchange, Queue

from .config import Settings
from .process_locks import enter_service_role
from .worker_runtime import WorkerRuntime, fail_closed
from .ingestion.pipeline import prepare_document
from .parsing.ocr import prepare_suggestion,close_client
from .db import IdentityStore, open_db
from .ingestion.store import IngestionStore
from contextlib import closing

settings = Settings.load()
app = Celery("raghub", broker=settings.redis_url, backend=None)
app.conf.update(
    broker_connection_retry_on_startup=False,
    task_publish_retry=False,
    task_acks_late=False,
    task_reject_on_worker_lost=False,
    broker_transport_options={"visibility_timeout": 3600},
    result_backend_transport_options={"visibility_timeout": 3600},
    visibility_timeout=3600,
    worker_prefetch_multiplier=1,
    task_default_queue="document",
    task_default_exchange="raghub.document",
    task_default_routing_key="document",
    task_queues=(
        Queue("document", Exchange("raghub.document", type="direct"), routing_key="document"),
        Queue("ocr", Exchange("raghub.ocr", type="direct"), routing_key="ocr"),
    ),
    task_create_missing_queues=False,
    worker_send_task_events=False,
    task_track_started=True,
)

_role_lock = None
_runtime = None


@worker_init.connect(weak=False)
def lock_worker_role(sender, **_kwargs) -> None:
    global _role_lock, _runtime
    hostname = str(sender.hostname)
    role = hostname.split("@", 1)[0]
    if role not in {"document-worker", "ocr-worker"} or hostname != f"{role}@raghub":
        raise SystemExit("worker hostname must identify one configured RAG Hub role")
    try:
        _role_lock = enter_service_role(settings, role)
        _runtime = WorkerRuntime(settings, role, hostname)
        _runtime.start()
    except Exception as exc:
        # Celery logs and suppresses ordinary signal exceptions; SystemExit
        # rejects this worker before its consumer connects to the broker.
        raise SystemExit(f"worker role lock rejected: {exc}") from exc


@worker_shutdown.connect(weak=False)
def release_worker_role(**_kwargs) -> None:
    global _role_lock, _runtime
    if _runtime is not None:
        try:
            _runtime.shutdown()
        except Exception as exc:
            fail_closed("shutdown", exc)
        _runtime = None
    close_client()
    if _role_lock is not None:
        _role_lock.close()
        _role_lock = None

@worker_ready.connect(weak=False)
def ready_worker(**_kwargs):
    try:
        _runtime.ready()
    except Exception as exc:
        fail_closed("ready", exc)

@task_prerun.connect(weak=False)
def before_task(sender, task_id, args, kwargs, **_extra):
    try:
        _runtime.prerun(task_id, sender.name, args, kwargs)
    except Exception as exc:
        fail_closed("task_prerun", exc)

@task_postrun.connect(weak=False)
def after_task(task_id, **_kwargs):
    try:
        _runtime.postrun(task_id)
    except Exception as exc:
        fail_closed("task_postrun", exc)

@app.task(name="raghub.prepare_document", bind=True, ignore_result=True, max_retries=0)
def document_task(self, instance, task_id, attempt_id):
    return prepare_document(settings, _runtime, instance, task_id, attempt_id, self.request.id, dict(self.request.delivery_info or {}))

@app.task(name="raghub.ocr_suggestion", bind=True, ignore_result=True, max_retries=0)
def ocr_task(self, instance, task_id, attempt_id):
    return prepare_suggestion(settings,_runtime,instance,task_id,attempt_id,self.request.id,dict(self.request.delivery_info or {}))
