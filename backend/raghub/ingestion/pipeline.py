"""Real acquisition handler; successful acquisition does not run a parser."""
from __future__ import annotations
import asyncio
import json
import logging
import random
import time
import traceback
from contextlib import closing
from pathlib import Path

from ..db import IdentityStore, open_db, transaction
from .archive import assemble_archive, json_bytes, json_hash, local_members, make_directory, write_closed
from .store import BusinessError, IngestionStore, event
from .web import AcquisitionError, acquire_web

LOG = logging.getLogger(__name__)

def prepare_document(settings, runtime, instance, task_id, attempt_id, delivery, delivery_info):
    runtime_instance = runtime.instance
    if instance != runtime_instance or runtime.active_task_id != task_id:
        return {"adopted": False, "reason": "STALE_DELIVERY"}
    with closing(open_db(settings.sqlite)) as db:
        runtime.assert_owner(db)
        epoch = IdentityStore(db).claim_attempt(data_instance_id=instance, task_id=task_id, attempt_id=attempt_id, worker_id=runtime.worker_id)
        if epoch is None:
            with transaction(db):
                event(db, task_id, attempt_id, "DELIVERY_REJECTED", {"delivery_id": delivery, "redelivered": delivery_info.get("redelivered"), "delivery_info": delivery_info})
            return {"adopted": False, "reason": "ATTEMPT_NOT_CLAIMABLE"}
        identity = (instance, task_id, attempt_id, epoch, runtime.worker_id)
        with transaction(db):
            event(db, task_id, attempt_id, "BROKER_DELIVERY_OBSERVED", {"delivery_id": delivery, "delivery_info": delivery_info, "redelivered": delivery_info.get("redelivered"), "application_publish_count_is_not_physical_send_count": True})
        store = IngestionStore(db, settings)
        payload = {}
        try:
            payload = store.input_for(task_id)
            if payload.get("operation") == "BUILD_GENERATION":
                from ..indexing.worker import build_claimed
                return build_claimed(db, settings, identity, payload)
            if payload.get("operation") == "DOUYIN_NOTE":
                from ..douyin_worker import prepare_claimed
                return prepare_claimed(db, settings, identity, payload)
            input_digest = json_hash(payload)
            directory = settings.home / "data/processing" / payload["kb_id"] / payload["document_id"] / payload["processing_version_id"] / "attempts" / attempt_id
            with transaction(db):
                store.current_owner(*identity)
                db.execute("UPDATE attempts SET directory_path=? WHERE attempt_id=?", (str(directory), attempt_id))
            make_directory(directory, settings.home, exclusive=True)
            write_closed(directory / "input.json", json_bytes(payload))
            if payload.get("operation") == "PARSE_ARCHIVE":
                from ..parsing.worker import parse_claimed
                return parse_claimed(db, settings, identity, directory, payload)
            def cancelled():
                try:
                    store.current_owner(*identity)
                    return False
                except BusinessError:
                    return True
            reused = store.reusable_acquisition(task_id, input_digest)
            if reused:
                archive, details, artifact = reused
                store.progress(identity, "ACQUIRE_REUSE_VERIFIED", {"artifact_id": artifact})
                adopted = store.adopt_archive(identity, archive, details, input_digest, reuse_of=artifact)
            else:
                deadline = time.monotonic() + 180 if payload["kind"] == "URL" else None
                while True:
                    count = store.use_stage_budget(identity, "ACQUIRE", input_digest)
                    execution_dir = directory / ("acquire-" + str(count))
                    make_directory(execution_dir, settings.home, exclusive=True)
                    store.progress(identity, "ACQUIRING", {"execution": count, "directory": str(execution_dir)})
                    try:
                        if payload["kind"] == "URL":
                            members, details, derived = asyncio.run(acquire_web(payload, execution_dir, deadline, cancelled))
                        else:
                            members, details, derived = local_members(payload, execution_dir)
                        break
                    except AcquisitionError as exc:
                        retry = exc.retryable and count < 3 and deadline is not None
                        delay = (2 if count == 1 else 4) + random.uniform(0, 0.5)
                        if not retry or cancelled() or time.monotonic() + delay >= deadline:
                            raise
                        with transaction(db):
                            store.current_owner(*identity)
                            event(db, task_id, attempt_id, "KNOWN_TRANSIENT_RETRY", {"code": exc.code, "execution": count, "delay_seconds": delay})
                        time.sleep(delay)
                store.progress(identity, "ARCHIVE_CLOSING", {"member_count": len(members)})
                from ..db import new_id
                raw_path = settings.home / "data/archives" / payload["kb_id"] / payload["document_id"] / new_id()
                archive = assemble_archive(settings, raw_path, members, derived=derived)
                store.progress(identity, "ARCHIVE_VERIFIED", {"manifest_path": archive["manifest_path"], "manifest_sha256": archive["manifest_sha256"]})
                adopted = store.adopt_archive(identity, archive, details, input_digest)
            write_closed(directory / "result.json", json_bytes({"acquisition": adopted, "archive": archive, "parsing": "NOT_STARTED"}))
            store.finish(identity)
            return adopted
        except Exception as exc:
            error = {"code": getattr(exc, "code", "GENERATION_FAILED" if payload.get("operation") == "BUILD_GENERATION" else "PARSE_FAILED" if payload.get("operation") == "PARSE_ARCHIVE" else "ACQUISITION_FAILED"), "error_type": type(exc).__name__,
                     "message": str(exc), "traceback": traceback.format_exc(), "details": getattr(exc, "details", {})}
            LOG.exception("document preparation failed task=%s attempt=%s", task_id, attempt_id)
            store.finish(identity, error=error)
            return {"adopted": False, "error": error}
