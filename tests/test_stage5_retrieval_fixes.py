"""Direct stage-5 checks: text intervals and one SQLite finalization failure."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx

from raghub.config import Settings
from raghub.db import open_db, system_state, transaction
from raghub.indexing.storage import decode
from raghub.retrieval.evidence import body_ranges
from raghub.retrieval.model_client import AsyncModelClient
from raghub.retrieval.service import RetrievalService
from raghub.retrieval.store import AsyncStore, RetrievalStore


class Stage5DirectFixChecks(unittest.TestCase):
    def test_mixed_text_keeps_protected_tokens_and_source_intervals(self):
        first = "版本 v2.5.1，阈值 3.14，示例 e.g. 采用 https://docs.example.com/v2.5.1?x=3.14 并联系 qa@example.com 后继续。"
        second = "Next step uses e.g. the same parser."
        third = " 末尾仅有缩写 e.g."
        text = first + second + third
        ranges = body_ranges({"text": text, "content_kind": "paragraph"}, {"parts": []}, 0, 0, len(text))
        self.assertEqual(ranges, [(0, len(first), "sentence"),
                                  (len(first), len(first + second), "sentence"),
                                  (len(first + second), len(text), "text_span")])
        self.assertEqual("".join(text[left:right] for left, right, _kind in ranges), text)
        print(json.dumps({"case": "mixed_text", "ranges": ranges, "source": text}, ensure_ascii=False))

    def test_first_complete_record_resumes_and_other_rows_continue(self):
        live = Settings.load()
        with tempfile.TemporaryDirectory(prefix="rg-s5-", dir="/tmp") as scratch:
            root = Path(scratch).resolve(strict=True)
            settings = replace(live, home=root, sqlite=root / "library.sqlite3")
            with closing(sqlite3.connect("file:" + str(live.sqlite) + "?mode=ro", uri=True)) as original:
                with closing(sqlite3.connect(settings.sqlite)) as target:
                    original.backup(target)
            with closing(open_db(settings.sqlite)) as db:
                # The copied database may be in IMPORT while a user prepares
                # documents. This store-finalization test requires QUERY mode.
                with transaction(db):
                    db.execute("UPDATE system_state SET mode='QUERY' WHERE singleton=1")
                instance = system_state(db)["data_instance_id"]
                store = RetrievalStore(db, settings)
                self.assertEqual(store.active(), [])
                kb = db.execute("SELECT kb_id FROM knowledge_bases WHERE current_generation_id IS NOT NULL ORDER BY kb_id LIMIT 1").fetchone()
                self.assertIsNotNone(kb)
                session = str(uuid4())
                store.create_session(instance, session, kb["kb_id"])
                a, b = str(uuid4()), str(uuid4())
                rows = {key: store.admit(instance, session, key, {"direct_store_check": key})["request"] for key in (a, b)}

                def trace(row):
                    return {"schema": "raghub.retrieval-trace.v1", "request_id": row["request_id"],
                            "session_id": row["session_id"], "created_at": row["created_at"],
                            "scope": decode(row["snapshot_json"]), "input": decode(row["input_json"]),
                            "observed_stages": "STORE_FINALIZATION_ONLY", "answer_generated": False,
                            "completion": {"requested_state": "SUCCEEDED", "error": None}}

                quote = chr(39)
                with transaction(db):
                    db.execute("CREATE TRIGGER hold_a_completion BEFORE UPDATE OF state ON retrieval_requests "
                               "WHEN NEW.request_id=" + quote + a + quote + " BEGIN SELECT RAISE(ABORT," +
                               quote + "FINALIZATION_TX_FAULT" + quote + "); END")
                deadline = time.monotonic() + 180
                with self.assertRaisesRegex(sqlite3.IntegrityError, "FINALIZATION_TX_FAULT"):
                    store.finish(instance, a, "SUCCEEDED", trace(rows[a]), deadline=deadline)
                path = root / "data/retrieval" / a / "trace.json"
                first_bytes, first_inode = path.read_bytes(), path.stat().st_ino
                first_sha = hashlib.sha256(first_bytes).hexdigest()
                self.assertEqual(store.read(instance, a)["state"], "RUNNING")
                self.assertEqual(db.execute("SELECT state FROM generation_readers WHERE request_id=?", (a,)).fetchone()[0], "OPEN")

                async def drain_checks():
                    controller = RetrievalService.__new__(RetrievalService)
                    controller.settings, controller.profile = settings, {}
                    controller.db = AsyncStore(settings)
                    controller.http = httpx.AsyncClient(trust_env=False, follow_redirects=False)
                    controller.tasks, controller.io_work, controller.pending = {}, {}, {}
                    controller.pending[b] = {"row": rows[b], "trace": trace(rows[b]), "state": "SUCCEEDED",
                        "error": None, "deadline": deadline,
                        "model": AsyncModelClient(settings, {}, controller.http, controller.db, instance, b)}
                    try:
                        with self.assertLogs("raghub.retrieval.service", level="ERROR"):
                            await controller.drain_once()
                        blocked = await controller.db.call("read", instance, a)
                        done_b = await controller.db.call("read", instance, b)
                        self.assertEqual(blocked["state"], "RUNNING")
                        self.assertEqual(blocked["error"]["code"], "FINALIZATION_BLOCKED")
                        self.assertEqual(done_b["state"], "SUCCEEDED")
                        self.assertEqual(db.execute("SELECT state FROM generation_readers WHERE request_id=?", (a,)).fetchone()[0], "OPEN")
                        self.assertEqual(db.execute("SELECT state FROM generation_readers WHERE request_id=?", (b,)).fetchone()[0], "CLOSED")
                        with transaction(db):
                            db.execute("DROP TRIGGER hold_a_completion")
                        controller.pending[a]["state"] = "INTERRUPTED"
                        controller.pending[a]["error"] = {"code": "LATER_IN_MEMORY_COMPLETION"}
                        controller.pending[a]["trace"]["completion"] = {"requested_state": "INTERRUPTED", "error": controller.pending[a]["error"]}
                        await controller.drain_once()
                        done_a = await controller.db.call("read", instance, a)
                        self.assertEqual(done_a["state"], "SUCCEEDED")
                        self.assertEqual(done_a["trace"]["completion"], {"requested_state": "SUCCEEDED", "error": None})
                        self.assertEqual(path.read_bytes(), first_bytes)
                        self.assertEqual(path.stat().st_ino, first_inode)
                        self.assertEqual(done_a["trace_sha256"], first_sha)
                        self.assertEqual(db.execute("SELECT state FROM generation_readers WHERE request_id=?", (a,)).fetchone()[0], "CLOSED")
                        c = str(uuid4())
                        store.admit(instance, session, c, {"direct_store_check": c})
                        partial = root / "data/retrieval" / c
                        partial.mkdir()
                        (partial / "trace.json").write_bytes(b"{\n")
                        with self.assertLogs("raghub.retrieval.service", level="ERROR"):
                            await controller.drain_once()
                        blocked_c = await controller.db.call("read", instance, c)
                        self.assertEqual(blocked_c["state"], "RUNNING")
                        self.assertEqual(blocked_c["error"]["code"], "FINALIZATION_BLOCKED")
                        self.assertEqual((partial / "trace.json").read_bytes(), b"{\n")
                        self.assertEqual(db.execute("SELECT state FROM generation_readers WHERE request_id=?", (c,)).fetchone()[0], "OPEN")
                        print(json.dumps({"case": "sqlite_finalization", "a_first_file_sha256": first_sha,
                            "a_before": blocked["state"], "a_after": done_a["state"],
                            "b_same_batch": done_b["state"], "partial_record": blocked_c["state"],
                            "partial_diagnostic": blocked_c["error"], "a_first_inode": first_inode}, ensure_ascii=False))
                    finally:
                        await controller.http.aclose()
                        await controller.db.close()

                asyncio.run(drain_checks())


if __name__ == "__main__":
    unittest.main()
