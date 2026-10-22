"""Focused Stage 2 regressions; isolated SQLite and offline Chromium, no broker/model calls."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from lxml import html
from PIL import Image
from playwright.async_api import async_playwright

from raghub.config import Settings
from raghub.db import IdentityStore, migrate, new_id, now, open_db, system_state, transaction
from raghub.dispatcher import Dispatcher
from raghub.ingestion.archive import verify_snapshot_image, parse_captured_html
from raghub.ingestion.web import DOM_RESOURCES, dns_wait_observation, public_url


PROJECT = Path(__file__).resolve().parents[1]


class DispatchBudgetTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="raghub-stage2-budget-")
        home = Path(self.root.name).resolve()
        (home / "tmp").mkdir()
        self.settings = replace(Settings.load(), home=home, project=PROJECT, sqlite=home / "state.sqlite3")
        with closing(open_db(self.settings.sqlite, create=True)) as db:
            migrate(db, PROJECT / "backend/migrations")
            self.instance = system_state(db)["data_instance_id"]
        self.dispatcher = Dispatcher(self.settings)
        self.dispatcher.initialize()
        self.addCleanup(self.root.cleanup)
        self.addCleanup(self.dispatcher.app.close)

    def task(self, key):
        with closing(open_db(self.settings.sqlite)) as db:
            return IdentityStore(db).create_task(
                kind="DOCUMENT", request_key=key, request_sha256="a" * 64, budget_scope_id=key)

    def outcome_fixture(self, outbox, state, producer):
        with closing(open_db(self.settings.sqlite)) as db, transaction(db):
            db.execute("UPDATE outbox SET publish_count=3,state=? WHERE outbox_id=?", (state, outbox))
            db.execute("INSERT INTO publish_observations(publish_id,outbox_id,publish_count,state,details_json,created_at) "
                       "VALUES (?,?,3,?,'{}',?)", (new_id(), outbox, producer, now()))

    def test_third_not_sent_finishes_task_and_next_task_still_dispatches(self):
        task, attempt, outbox = self.task("mode-race")
        with patch.object(self.dispatcher.app, "signature") as signature:
            for _ordinal in range(3):
                with closing(open_db(self.settings.sqlite)) as db, transaction(db):
                    row = db.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox,)).fetchone()
                    job = self.dispatcher.reserve(db, "DOCUMENT", row, {"worker_id": "idle-worker"})
                    db.execute("UPDATE system_state SET mode='QUERY' WHERE singleton=1")
                self.dispatcher.publish(job)
                with closing(open_db(self.settings.sqlite)) as db, transaction(db):
                    db.execute("UPDATE system_state SET mode='IMPORT' WHERE singleton=1")
            signature.assert_not_called()
        with closing(open_db(self.settings.sqlite)) as db:
            task_row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task,)).fetchone()
            self.assertEqual(task_row["status"], "FAILED")
            error = json.loads(task_row["last_error_json"])
            self.assertEqual((error["code"], error["publish_count"], error["last_publication_state"]),
                             ("PUBLISH_BUDGET_EXHAUSTED", 3, "NOT_SENT"))
            self.assertTrue(error["message"])
            self.assertEqual(db.execute("SELECT state FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0], "FAILED")
            self.assertEqual(tuple(db.execute("SELECT state,publish_count FROM outbox WHERE outbox_id=?", (outbox,)).fetchone()),
                             ("CANCELLED", 3))
            self.assertEqual([row[0] for row in db.execute("SELECT state FROM publish_observations WHERE outbox_id=? "
                                                         "ORDER BY publish_count", (outbox,))], ["NOT_SENT"] * 3)
            self.assertIsNone(IdentityStore(db).claim_attempt(data_instance_id=self.instance, task_id=task,
                                                            attempt_id=attempt, worker_id="late-worker"))
        next_task, _attempt, _outbox = self.task("following-task")
        with patch.object(self.dispatcher, "worker", return_value={"worker_id": "idle-worker"}):
            jobs = self.dispatcher.tick()
        self.assertEqual([job["task_id"] for job in jobs], [next_task])

    def test_historical_not_sent_exhaustion_is_terminal_without_a_new_reservation(self):
        task, _attempt, outbox = self.task("historical-unsent")
        self.outcome_fixture(outbox, "PENDING", "NOT_SENT")
        with closing(open_db(self.settings.sqlite)) as db, transaction(db):
            row = db.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox,)).fetchone()
            self.assertIsNone(self.dispatcher.reserve(db, "DOCUMENT", row, {"worker_id": "idle-worker"}))
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "FAILED")
            self.assertEqual(db.execute("SELECT count(*) FROM publish_observations WHERE outbox_id=?", (outbox,)).fetchone()[0], 1)

    def test_unknown_still_waits_for_recovery_basis_and_retains_unknown(self):
        task, _attempt, outbox = self.task("unknown-budget")
        self.outcome_fixture(outbox, "UNKNOWN", "UNKNOWN")
        with closing(open_db(self.settings.sqlite)) as db, transaction(db):
            row = db.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox,)).fetchone()
            with patch.object(self.dispatcher, "recovery_basis", return_value=None):
                self.dispatcher.recover_unclaimed(db, row, None, time.time())
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "PENDING")
            with patch.object(self.dispatcher, "recovery_basis", return_value="CONFIRMED_IDLE_INTERVALS"):
                self.dispatcher.recover_unclaimed(db, row, None, time.time())
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "FAILED")
            self.assertEqual(db.execute("SELECT state FROM outbox WHERE outbox_id=?", (outbox,)).fetchone()[0], "UNKNOWN")
            self.assertEqual(db.execute("SELECT state FROM publish_observations WHERE outbox_id=?", (outbox,)).fetchone()[0], "UNKNOWN")

    def test_exhaustion_does_not_overwrite_a_claimed_attempt(self):
        task, attempt, outbox = self.task("claimed-before-terminal")
        self.outcome_fixture(outbox, "PENDING", "NOT_SENT")
        with closing(open_db(self.settings.sqlite)) as db:
            row = db.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox,)).fetchone()
            self.assertEqual(IdentityStore(db).claim_attempt(data_instance_id=self.instance, task_id=task,
                                                           attempt_id=attempt, worker_id="prior-delivery"), 1)
            with transaction(db):
                self.assertFalse(self.dispatcher.exhaust_budget(db, row, "STALE_PENDING_OBSERVATION"))
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "RUNNING")
            self.assertEqual(db.execute("SELECT state FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0], "RUNNING")


class WebCaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_evaluation_binds_changed_src_and_picture_selection(self):
        def bitmap(color):
            stream = BytesIO()
            Image.new("RGB", (2, 2), color).save(stream, format="PNG")
            return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
        first, second = bitmap("red"), bitmap("blue")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", lambda route: route.abort())
                await page.set_content('<!DOCTYPE html><html><head><base href="https://example.org/docs/"></head>'
                                       '<body><main><img id="changing" src="' + first + '">'
                                       '<picture><source srcset="' + second + ' 1x" media="(min-width: 1px)">'
                                       '<img src="' + first + '"></picture></main></body></html>')
                previous = await page.content()
                await page.evaluate("(value) => {document.getElementById('changing').src = value;}", second)
                await page.wait_for_function("Array.from(document.querySelectorAll('img')).every(image => image.complete && image.currentSrc)")
                snapshot = await page.evaluate(DOM_RESOURCES, "main")
                self.assertTrue(snapshot["captured_html"].lower().startswith("<!doctype html>"))
                self.assertNotEqual(previous, snapshot["captured_html"])
                captured = parse_captured_html(snapshot["captured_html"])
                self.assertEqual(len(snapshot["images"]), 2)
                for index, image in enumerate(snapshot["images"]):
                    binding = {key: image[key] for key in ("node_path", "attribute", "source_url", "selected_url",
                                                          "srcset", "current_src", "picture_sources")}
                    binding["local_path"] = "resources/image-" + str(index) + ".png"
                    nodes = captured.xpath(image["node_path"])
                    self.assertEqual(len(nodes), 1)
                    verify_snapshot_image(nodes[0], binding, snapshot["base_url"])
                    self.assertEqual(image["selected_url"], image["current_src"])
                self.assertEqual(snapshot["images"][0]["source_url"], second)
                self.assertEqual(snapshot["images"][1]["selected_url"], second)
                stale = parse_captured_html(previous).xpath(snapshot["images"][0]["node_path"])[0]
                binding = {key: snapshot["images"][0][key] for key in ("node_path", "attribute", "source_url", "selected_url",
                                                                     "srcset", "current_src", "picture_sources")}
                binding["local_path"] = "resources/image-0.png"
                with self.assertRaises(ValueError):
                    verify_snapshot_image(stale, binding, snapshot["base_url"])
            finally:
                await browser.close()

    async def test_dns_await_cancellation_is_observed_without_claiming_thread_exit(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def resolver(*_args):
            started.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("controlled DNS fixture was not released")
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
            finally:
                finished.set()
        phase = {"name": "NAVIGATION_RESOLUTION", "dns_wait_sequence": 0,
                 "active_dns_waits": {}, "cancelled_dns_waits": []}
        with patch("raghub.ingestion.web.socket.getaddrinfo", resolver):
            task = asyncio.create_task(public_url("https://example.org/doc", ["example.org"],
                                                  phase=phase, dns_scope="NAVIGATION_RESOLUTION"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                observed = dns_wait_observation(phase)
                self.assertIs(observed["dns_wait_cancelled"], True)
                self.assertEqual(observed["dns_thread_exit"], "NOT_OBSERVED")
                self.assertEqual(observed["cancelled_dns_waits"][0]["phase"], "URL_DNS_CHECK")
                self.assertEqual(observed["cancelled_dns_waits"][0]["scope"], "NAVIGATION_RESOLUTION")
                self.assertEqual(observed["active_dns_waits"], [])
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
