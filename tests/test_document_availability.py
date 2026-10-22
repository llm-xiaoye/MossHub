"""Pending archive access and macOS clock changes, using isolated local state."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from raghub import manager, ui
from raghub.config import Settings
from raghub.db import IdentityStore, migrate, new_id, now, open_db, system_state
from raghub.dispatcher import Dispatcher
from raghub.ingestion import router as intake
from raghub.ingestion.store import BusinessError, IngestionStore
from raghub.process_identity import process_start_time
from raghub.worker_runtime import WorkerRuntime, process_liveness

PROJECT = Path(__file__).resolve().parents[1]


class IsolatedState(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rag-source-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "tmp").mkdir()
        (root / "logs").mkdir()
        self.settings = replace(Settings.load(), home=root, project=PROJECT,
                                sqlite=root / "state.sqlite3", logs=root / "logs",
                                run_state=root / "tmp/services.json")
        with closing(open_db(self.settings.sqlite, create=True)) as db:
            migrate(db, PROJECT / "backend/migrations")
            self.instance = system_state(db)["data_instance_id"]


class DocumentAvailabilityTest(IsolatedState):
    def setUp(self):
        super().setUp()
        for module in (ui, intake):
            setting = patch.object(module, "settings", self.settings)
            setting.start()
            self.addCleanup(setting.stop)
        with closing(open_db(self.settings.sqlite)) as db:
            self.kb = IngestionStore(db, self.settings).create_kb(self.instance, "pending sample")["kb_id"]
        app = FastAPI()
        app.include_router(intake.router)
        app.include_router(ui.router)

        @app.exception_handler(BusinessError)
        async def business_error(_request: Request, error: BusinessError):
            return JSONResponse({"code": error.code, "detail": str(error)}, status_code=error.status)

        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"X-RAG-Data-Instance": self.instance}
        content = b"# Pending original\nA real accepted upload.\n"
        batch = self.client.post("/api/import/batches", headers={**self.headers, "X-Request-ID": new_id()},
            json={"kb_id": self.kb, "items": [{"filename": "pending.md", "size_bytes": len(content),
                  "sha256": hashlib.sha256(content).hexdigest(), "provenance": {"nature": "ORIGINAL"}}]})
        self.assertEqual(batch.status_code, 200, batch.text)
        record = batch.json()
        uploaded = self.client.post("/api/import/batches/" + record["batch_id"] + "/items/" + record["items"][0]["item_id"] + "/file",
            files={"file": ("pending.md", content, "text/markdown")}, headers=self.headers)
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        with closing(open_db(self.settings.sqlite)) as db:
            self.document = db.execute("SELECT document_id FROM documents WHERE kb_id=?", (self.kb,)).fetchone()[0]

    def test_pending_original_and_parsing_options_are_not_ready(self):
        for endpoint in ("source", "parsing-options"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get("/api/ui/documents/" + self.document + "/" + endpoint, headers=self.headers)
                self.assertEqual((response.status_code, response.json()["code"]), (409, "SOURCE_NOT_READY"))
                self.assertIn("尚未归档", response.json()["detail"])
        with closing(open_db(self.settings.sqlite)) as db:
            row = db.execute("SELECT current_raw_version_id,active_prepare_task_id FROM documents WHERE document_id=?", (self.document,)).fetchone()
            self.assertIsNone(row["current_raw_version_id"])
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?", (row["active_prepare_task_id"],)).fetchone()[0], "PENDING")

    def test_explicit_unowned_raw_version_is_still_rejected(self):
        response = self.client.get("/api/ui/documents/" + self.document + "/source",
                                   params={"raw_version_id": new_id()}, headers=self.headers)
        self.assertEqual((response.status_code, response.json()["code"]), (404, "SOURCE_NOT_ACCESSIBLE"))
        self.assertIn("不属于此文档", response.json()["detail"])

    def test_removed_document_stays_inaccessible(self):
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE documents SET removed_at=? WHERE document_id=?", (now(), self.document))
        for endpoint in ("source", "parsing-options"):
            response = self.client.get("/api/ui/documents/" + self.document + "/" + endpoint, headers=self.headers)
            self.assertEqual((response.status_code, response.json()["code"]), (404, "SOURCE_NOT_ACCESSIBLE"))


@unittest.skipUnless(psutil.OSX, "Darwin-specific wall-clock adjustment regression")
class ProcessClockTest(IsolatedState):
    def clock_change(self):
        return patch.object(psutil._psplatform, "INIT_BOOT_TIME", psutil._psplatform.boot_time() - 120)

    def test_dispatcher_keeps_live_worker_eligible_after_clock_change(self):
        runtime = WorkerRuntime(self.settings, "document-worker", "document-worker@raghub")
        runtime.start()
        self.addCleanup(runtime.shutdown)
        runtime.ready()
        dispatcher = Dispatcher(self.settings)
        self.addCleanup(dispatcher.app.close)
        dispatcher.initialize()
        with closing(open_db(self.settings.sqlite)) as db:
            task, _attempt, _outbox = IdentityStore(db).create_task(
                kind="DOCUMENT", request_key=new_id(), request_sha256="a" * 64, budget_scope_id=new_id())
        with self.clock_change():
            self.assertNotEqual(psutil.Process(runtime.pid).create_time(), runtime.process_created_at)
            self.assertEqual(process_liveness(runtime.pid, runtime.process_created_at), "LIVE")
            jobs = dispatcher.tick()
        self.assertEqual([job["task_id"] for job in jobs], [task])
        self.assertEqual(jobs[0]["worker_id"], runtime.worker_id)
        self.assertEqual(jobs[0]["publish_count"], 1)

    def test_clock_change_does_not_accept_a_different_start_identity(self):
        created = process_start_time(psutil.Process())
        with self.clock_change():
            self.assertEqual(process_liveness(os.getpid(), created + 1), "STOPPED")
            self.assertFalse(manager.process_matches({"pid": os.getpid(), "created_at": created + 1}))

    def test_manager_and_maintenance_report_live_process_after_clock_change(self):
        record = {"pid": os.getpid(), "created_at": process_start_time(psutil.Process())}
        self.settings.run_state.write_text(json.dumps({"services": {"api": record}}))
        with patch.object(ui, "settings", self.settings), self.clock_change():
            self.assertTrue(manager.process_matches(record))
            self.assertTrue(ui.maintenance()["services"][0]["running"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

