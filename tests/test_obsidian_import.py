"""One isolated functional vault: 51 sample notes, no private-vault writes or providers."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image

from raghub.config import Settings
from raghub.db import migrate, new_id, open_db, system_state
from raghub.ingestion import router as intake
from raghub.ingestion.archive import assemble_archive, local_members, verify_archive, json_bytes, write_closed
from raghub.ingestion.contracts import DirectoryPreviewRequest
from raghub.ingestion.store import BusinessError, IngestionStore
from raghub.obsidian import create_router, create_directory_update_router
from raghub.ingestion.pipeline import prepare_document
from raghub.worker_runtime import WorkerRuntime

PROJECT = Path(__file__).resolve().parents[1]


class ObsidianFunctionalTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-obs-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home, self.vault = self.root / "home", self.root / "vault"
        (self.home / "tmp").mkdir(parents=True)
        (self.vault / "notes/deep").mkdir(parents=True)
        (self.vault / "assets").mkdir()
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian/config.json").write_text("{}")
        (self.vault / "unsupported.exe").write_text("functional sample")
        for index in range(51):
            path = self.vault / ("notes/deep/050.md" if index == 50 else f"notes/{index:03}.md")
            text = f"# Functional sample {index:03}\n隔离功能样本，不是用户资料。\n"
            if index == 0:
                text += "![示例](../assets/picture.png)\n"
            if index == 1:
                text += "长文分段。\n" * 2000
            path.write_text(text, encoding="utf-8")
        Image.new("RGB", (2, 2), "green").save(self.vault / "assets/picture.png")
        self.settings = replace(Settings.load(), home=self.home, project=PROJECT, sqlite=self.home / "state.sqlite3")
        with closing(open_db(self.settings.sqlite, create=True)) as db:
            migrate(db, PROJECT / "backend/migrations")
            self.instance = system_state(db)["data_instance_id"]
            store = IngestionStore(db, self.settings)
            self.kb = store.create_kb(self.instance, "isolated functional samples")["kb_id"]
            self.other_kb = store.create_kb(self.instance, "legacy upload regression")["kb_id"]
        self.headers = {"X-RAG-Data-Instance": self.instance}
        self.settings_patch = patch.object(intake, "settings", self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        app = FastAPI()
        app.include_router(intake.router)
        app.include_router(create_router(self.settings, self.vault), prefix="/api/ui")
        app.include_router(create_directory_update_router(self.settings), prefix="/api/ui")
        @app.exception_handler(BusinessError)
        async def business_error(_request: Request, error: BusinessError):
            return JSONResponse({"code": error.code, "detail": str(error)}, status_code=error.status)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def post(self, path, body, *, key=None):
        headers = {**self.headers, **({"X-Request-ID": key} if key else {})}
        response = self.client.post(path, json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def get(self, path):
        response = self.client.get(path, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def selection(self):
        return self.post("/api/ui/obsidian/selection", {"directories": ["notes"], "paths": []})["entries"]

    def test_browse_preview_selection_and_changed_file(self):
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE system_state SET mode='QUERY' WHERE singleton=1")
        tree = self.get("/api/ui/obsidian/tree")
        excluded = next(item for item in tree["items"] if item["filename"] == ".obsidian")
        self.assertEqual(excluded["skip_reason"], "EXCLUDED_DIRECTORY")
        first = self.get("/api/ui/obsidian/tree?path=notes&limit=10")
        self.assertEqual((len(first["items"]), first["total"]), (10, 51))
        search = self.get("/api/ui/obsidian/tree?search=notes%2F001")
        self.assertEqual([item["relative_path"] for item in search["items"]], ["notes/001.md"])
        first_part = self.get("/api/ui/obsidian/markdown?path=notes%2F001.md")
        second_part = self.get("/api/ui/obsidian/markdown?path=notes%2F001.md&offset=8000")
        self.assertEqual((first_part["offset"], first_part["end"], second_part["offset"]), (0, 8000, 8000))
        selected = self.selection()
        self.assertEqual(len(selected), 51)
        (self.vault / "notes/new.md").write_text("# Added after selection")
        (self.vault / "notes/001.md").write_text("# Changed sample")
        checks = self.post("/api/ui/obsidian/check", {"entries": [
            {"relative_path": item["relative_path"], "signature": item["signature"]} for item in selected
        ]})["items"]
        self.assertEqual(len(checks), 51)
        self.assertNotIn("notes/new.md", {item["relative_path"] for item in checks})
        self.assertEqual(next(item["status"] for item in checks if item["relative_path"] == "notes/001.md"), "CHANGED")
        forbidden = self.client.post("/api/ui/obsidian/preview",
            json={"kb_id": self.kb, "entries": [{"relative_path": selected[0]["relative_path"],
                                               "signature": selected[0]["signature"]}]}, headers=self.headers)
        self.assertEqual(forbidden.status_code, 200, forbidden.text)
        self.assertEqual(forbidden.json()["file_count"], 1)
        updated = self.get("/api/ui/obsidian/markdown?path=notes%2F001.md")
        self.assertEqual(updated["text"], "# Changed sample")
        self.assertEqual(next(item["current_signature"] for item in checks if item["relative_path"] == "notes/001.md"), updated["signature"])
        (self.vault / "notes").rename(self.vault / "notes-moved")
        missing = self.client.get("/api/ui/obsidian/tree?path=notes", headers=self.headers)
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "OBSIDIAN_DIRECTORY_MISSING"))
        self.assertIn("notes-moved", {item["relative_path"] for item in self.get("/api/ui/obsidian/tree")["items"]})
        gone = self.post("/api/ui/obsidian/check", {"entries": [{"relative_path": "notes/001.md", "signature": updated["signature"]}]})["items"][0]
        self.assertEqual((gone["status"], gone["current_signature"]), ("MISSING", None))
        absent_note = self.client.get("/api/ui/obsidian/markdown?path=notes%2F001.md", headers=self.headers)
        self.assertEqual((absent_note.status_code, absent_note.json()["code"]), (404, "OBSIDIAN_PREVIEW_MISSING"))
        (self.vault / "notes").write_text("not a directory")
        invalid = self.client.get("/api/ui/obsidian/tree?path=notes", headers=self.headers)
        self.assertEqual((invalid.status_code, invalid.json()["code"]), (409, "OBSIDIAN_DIRECTORY_INVALID"))
        invalid_note = self.client.get("/api/ui/obsidian/markdown?path=notes%2F001.md", headers=self.headers)
        self.assertEqual((invalid_note.status_code, invalid_note.json()["code"]), (409, "OBSIDIAN_PREVIEW_INVALID"))
        invalid_check = self.post("/api/ui/obsidian/check", {"entries": [{"relative_path": "notes/001.md", "signature": updated["signature"]}]})["items"][0]
        self.assertEqual(invalid_check["status"], "MISSING")
        self.vault.rename(self.root / "vault-unavailable")
        unavailable = self.client.get("/api/ui/obsidian/tree", headers=self.headers)
        self.assertEqual((unavailable.status_code, unavailable.json()["code"]), (422, "OBSIDIAN_ROOT_UNAVAILABLE"))
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_same_named_directories_keep_distinct_paths_and_live_changes(self):
        for relative in ("AI/面试题库","面试题库/评测与安全"):
            (self.vault/relative).mkdir(parents=True)
        (self.vault/"AI/面试题库/旧目录笔记.md").write_text("# old folder")
        root_rows=self.get("/api/ui/obsidian/tree")["items"]
        self.assertIn("面试题库",{row["relative_path"] for row in root_rows})
        old=self.get("/api/ui/obsidian/tree?path=AI%2F面试题库")["items"]
        current=self.get("/api/ui/obsidian/tree?path=面试题库")["items"]
        self.assertEqual([row["relative_path"] for row in old],["AI/面试题库/旧目录笔记.md"])
        self.assertEqual([row["relative_path"] for row in current],["面试题库/评测与安全"])
        (self.vault/"面试题库/训练与微调").mkdir()
        updated=self.get("/api/ui/obsidian/tree?path=面试题库")["items"]
        self.assertEqual({row["relative_path"] for row in updated},
                         {"面试题库/评测与安全","面试题库/训练与微调"})
        self.assertEqual(self.get("/api/ui/obsidian/tree?path=AI%2F面试题库")["items"],old)

    def test_51_files_two_batches_recovery_and_image_snapshot(self):
        entries = self.selection()
        preview = self.post("/api/ui/obsidian/preview", {"kb_id": self.kb, "entries": [
            {"relative_path": item["relative_path"], "signature": item["signature"]} for item in entries]})
        self.assertEqual((preview["file_count"], preview["manifest"]["scope"], preview["missing"]), (51, "SELECTION", []))
        self.assertEqual([item["relative_path"] for item in preview["dependency_entries"]], ["assets/picture.png"])
        self.post("/api/import/directory/previews/" + preview["preview_id"] + "/confirm",
                  {"preview_sha256": preview["preview_sha256"], "confirmed": True})
        batches = []
        for files in (preview["manifest"]["files"][:50], preview["manifest"]["files"][50:]):
            key = new_id()
            missing = self.client.get("/api/import/batches/by-request/" + key)
            self.assertEqual((missing.status_code, missing.json()["code"]), (404, "REQUEST_NOT_FOUND"))
            body = {"kb_id": self.kb, "items": files, "preview_id": preview["preview_id"],
                    "preview_sha256": preview["preview_sha256"]}
            batch = self.post("/api/import/batches", body, key=key)
            batches.append(batch)
            self.assertEqual(self.post("/api/import/batches", body, key=key)["batch_id"], batch["batch_id"])
        self.assertEqual([len(batch["items"]) for batch in batches], [50, 1])
        self.assertEqual(len({item["metadata"]["relative_path"] for batch in batches for item in batch["items"]}), 51)
        first = batches[0]["items"][0]
        receive_path = "/api/ui/obsidian/batches/" + batches[0]["batch_id"] + "/items/" + first["item_id"] + "/receive"
        original = self.post(receive_path, {})
        # A fresh status read represents manual resumption after an interruption.
        for batch in batches:
            recovered = self.get("/api/import/batches/" + batch["batch_id"])
            for item in recovered["items"]:
                if item["state"] != "ACCEPTED":
                    self.post("/api/ui/obsidian/batches/" + recovered["batch_id"] + "/items/" + item["item_id"] + "/receive", {})
        duplicate = self.post(receive_path, {})
        self.assertEqual((duplicate["task_id"], duplicate["already_accepted"]), (original["task_id"], True))
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM task_inputs WHERE kb_id=?", (self.kb,)).fetchone()[0], 51)
            self.assertEqual(db.execute("SELECT count(*) FROM task_inputs WHERE kb_id=?", (self.other_kb,)).fetchone()[0], 0)
            payload = IngestionStore(db, self.settings).input_for(original["task_id"])
        # Changing the live image after intake must not alter archived resources.
        (self.vault / "assets/picture.png").write_bytes(b"changed after intake")
        stage = self.home / "stage"
        stage.mkdir()
        members, details, derived = local_members(payload, stage)
        archive = assemble_archive(self.settings, self.home / "archive", members, derived=derived)
        asset = details["local_asset_members"][0]
        member = next(item for item in archive["files"] if item["path"] == asset["archive_member"])
        self.assertEqual(member["sha256"], preview["dependency_entries"][0]["sha256"])
        self.assertEqual(details["directory_context"]["image_relations"][0]["image_sha256"], member["sha256"])
        self.assertIn("obsidian-resources.json", {item["path"] for item in archive["files"]})
        # Selecting one file never reports the other fifty sources as missing.
        subset = self.post("/api/ui/obsidian/selection", {"paths": ["notes/002.md"], "directories": []})["entries"]
        next_preview = self.post("/api/ui/obsidian/preview", {"kb_id": self.kb, "entries": [
            {"relative_path": item["relative_path"], "signature": item["signature"]} for item in subset]})
        self.assertEqual(next_preview["missing"], [])
        legacy = DirectoryPreviewRequest.model_validate({**next_preview["manifest"], "scope": "DIRECTORY"})
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(len(IngestionStore(db, self.settings).preview(self.instance, legacy)["missing"]), 50)

    def accept_preview(self, preview, receive_prefix="/api/ui/obsidian"):
        self.post("/api/import/directory/previews/" + preview["preview_id"] + "/confirm",
                  {"preview_sha256": preview["preview_sha256"], "confirmed": True})
        batch = self.post("/api/import/batches", {"kb_id": preview["manifest"]["kb_id"],
            "items": preview["manifest"]["files"], "preview_id": preview["preview_id"],
            "preview_sha256": preview["preview_sha256"]}, key=new_id())
        item = batch["items"][0]
        path = receive_prefix + "/batches/" + batch["batch_id"] + "/items/" + item["item_id"] + "/receive"
        received = self.post(path, {})
        self.assertEqual(self.post(path, {})["task_id"], received["task_id"])
        return received["task_id"]

    def archive_task(self, task_id):
        with closing(open_db(self.settings.sqlite)) as db:
            attempt = db.execute("SELECT current_attempt_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        runtime = getattr(self, "archive_runtime", None)
        if runtime is None:
            runtime = WorkerRuntime(self.settings, "document-worker", "document-worker@raghub")
            runtime.start()
            runtime.ready()
            self.archive_runtime = runtime
            self.addCleanup(runtime.shutdown)
        delivery = new_id()
        runtime.prerun(delivery, "raghub.prepare_document", [self.instance, task_id, attempt], {})
        try:
            prepare_document(self.settings, runtime, self.instance, task_id, attempt, delivery, {})
        finally:
            runtime.postrun(delivery)
        with closing(open_db(self.settings.sqlite)) as db:
            task = db.execute("SELECT status,last_error_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            self.assertEqual(task["status"], "SUCCEEDED", task["last_error_json"])
            document_id = IngestionStore(db, self.settings).input_for(task_id)["document_id"]
            return dict(db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone())

    def test_registered_changes_track_note_and_image_without_importing_neighbors(self):
        document = self.initial_document()
        original_raw = document["current_raw_version_id"]
        endpoint = "/api/ui/obsidian/changes?kb_id=" + self.kb
        first = self.get(endpoint)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(first["items"][0]["state"], "UNCHANGED")
        self.assertFalse(first["items"][0]["latest_archive_published"])
        (self.vault / "notes/not-selected.md").write_text("Not selected")
        self.assertEqual(len(self.get(endpoint)["items"]), 1)
        Image.new("RGB", (2, 2), "blue").save(self.vault / "assets/picture.png")
        changed = self.get(endpoint)["items"][0]
        self.assertEqual(changed["note_state"], "UNCHANGED")
        self.assertEqual([(item["kind"], item["state"]) for item in changed["changes"]],
                         [("IMAGE", "CHANGED")])
        note = self.vault / "notes/000.md"
        note.write_text(note.read_text() + "\nA revised condition.\n")
        changed = self.get(endpoint)["items"][0]
        self.assertEqual(changed["note_state"], "CHANGED")
        self.assertEqual({item["kind"] for item in changed["changes"]}, {"NOTE", "IMAGE"})
        note.unlink()
        self.assertEqual(self.get(endpoint)["items"][0]["note_state"], "MISSING")
        with closing(open_db(self.settings.sqlite)) as db:
            current = db.execute("SELECT current_raw_version_id,removed_at FROM documents WHERE document_id=?",
                                 (document["document_id"],)).fetchone()
            self.assertEqual(current["current_raw_version_id"], original_raw)
            self.assertIsNone(current["removed_at"])

    def test_obsidian_target_is_registered_current_note_and_removed_source_is_denied(self):
        from urllib.parse import parse_qs, urlsplit
        path = "notes/中文 空格.md"
        note = self.vault / path
        note.write_text("# A registered note\nOriginal condition.\n")
        document = self.initial_document(path)
        endpoint = "/api/ui/documents/" + document["document_id"] + "/obsidian-target"
        target = self.get(endpoint + "?raw_version_id=" + document["current_raw_version_id"])
        self.assertTrue(target["available"])
        self.assertEqual(parse_qs(urlsplit(target["uri"]).query)["path"], [str(note)])
        self.assertFalse(target["current_note_differs_from_archive"])
        note.write_text("# A registered note\nChanged condition.\n")
        target = self.get(endpoint + "?raw_version_id=" + document["current_raw_version_id"])
        self.assertTrue(target["current_note_differs_from_archive"])
        other = self.initial_document("notes/002.md")
        invalid = self.client.get(endpoint + "?raw_version_id=" + other["current_raw_version_id"],
                                  headers=self.headers)
        self.assertEqual(invalid.status_code, 404)
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE documents SET removed_at=? WHERE document_id=?",
                       ("2026-10-14T00:00:00+00:00", document["document_id"]))
        self.assertEqual(self.client.get(endpoint, headers=self.headers).status_code, 404)
        remaining = self.get("/api/ui/obsidian/changes?kb_id=" + self.kb)["items"]
        self.assertNotIn(document["document_id"], [item["document_id"] for item in remaining])

    def test_registered_change_pagination_and_instance_scope(self):
        first_document = self.initial_document()
        second_document = self.initial_document("notes/002.md")
        endpoint = "/api/ui/obsidian/changes?kb_id=" + self.kb + "&limit=1"
        first = self.get(endpoint)
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        second = self.get(endpoint + "&cursor=" + first["next_cursor"])
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])
        observed = {first["items"][0]["document_id"], second["items"][0]["document_id"]}
        self.assertEqual(observed, {first_document["document_id"], second_document["document_id"]})
        self.assertEqual(self.get("/api/ui/obsidian/changes?kb_id=" + self.other_kb)["items"], [])
        rejected = self.client.get(endpoint, headers={"X-RAG-Data-Instance": new_id()})
        self.assertIn(rejected.status_code, (409, 422))
        self.vault.rename(self.root / "temporarily-unavailable-vault")
        self.assertEqual(self.get("/api/ui/obsidian/changes?kb_id=" + self.other_kb)["items"], [])
        unavailable = self.client.get(endpoint, headers=self.headers)
        self.assertEqual(unavailable.status_code, 422)
        self.assertEqual(unavailable.json()["code"], "OBSIDIAN_ROOT_UNAVAILABLE")

    def initial_document(self, path="notes/000.md"):
        selected = self.post("/api/ui/obsidian/selection", {"paths": [path], "directories": []})["entries"]
        preview = self.post("/api/ui/obsidian/preview", {"kb_id": self.kb, "entries": [
            {"relative_path": item["relative_path"], "signature": item["signature"]} for item in selected]})
        return self.archive_task(self.accept_preview(preview))

    def test_obsidian_update_keeps_document_and_previous_raw_version(self):
        original = self.initial_document()
        path = self.vault / "notes/000.md"
        path.write_text(path.read_text() + "\nA new revision.\n")
        selected = self.post("/api/ui/obsidian/selection", {"paths": ["notes/000.md"], "directories": []})["entries"]
        preview = self.post("/api/ui/obsidian/preview", {"kb_id": self.kb, "document_id": original["document_id"],
            "entries": [{"relative_path": item["relative_path"], "signature": item["signature"]} for item in selected]})
        self.assertEqual(preview["manifest"]["files"][0]["document_id"], original["document_id"])
        self.assertEqual((preview["file_count"], preview["missing"]), (1, []))
        updated = self.archive_task(self.accept_preview(preview))
        self.assertEqual(updated["document_id"], original["document_id"])
        self.assertNotEqual(updated["current_raw_version_id"], original["current_raw_version_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM documents WHERE kb_id=?", (self.kb,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM raw_versions WHERE document_id=?", (original["document_id"],)).fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)

        unchanged_preview = self.post("/api/ui/obsidian/preview", {"kb_id": self.kb, "document_id": original["document_id"],
            "entries": [{"relative_path": item["relative_path"], "signature": item["signature"]} for item in selected]})
        unchanged = self.archive_task(self.accept_preview(unchanged_preview))
        self.assertEqual(unchanged["current_raw_version_id"], updated["current_raw_version_id"])
        self.assertEqual(unchanged["current_processing_version_id"], updated["current_processing_version_id"])

    def test_image_only_update_creates_new_raw_version(self):
        original = self.initial_document()
        Image.new("RGB", (2, 2), "red").save(self.vault / "assets/picture.png")
        selected = self.post("/api/ui/obsidian/selection", {"paths": ["notes/000.md"], "directories": []})["entries"]
        preview = self.post("/api/ui/obsidian/preview", {"kb_id": self.kb, "document_id": original["document_id"],
            "entries": [{"relative_path": item["relative_path"], "signature": item["signature"]} for item in selected]})
        updated = self.archive_task(self.accept_preview(preview))
        self.assertEqual(updated["document_id"], original["document_id"])
        self.assertNotEqual(updated["current_raw_version_id"], original["current_raw_version_id"])

    def test_legacy_multi_member_archive_retains_its_read_identity(self):
        legacy = self.home / "legacy-archive"
        legacy.mkdir()
        contents = {"obsidian-resources.json": b"{}\n", "original.md": b"# Original\n"}
        files = []
        for name, content in sorted(contents.items()):
            write_closed(legacy / name, content)
            files.append({"path": name, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        manifest = legacy / "archive-manifest.json"
        write_closed(manifest, json_bytes({"schema_version": 1, "files": files}))
        old = verify_archive(manifest)
        self.assertEqual(old["content_sha256"], hashlib.sha256(contents["obsidian-resources.json"]).hexdigest())
        current = assemble_archive(self.settings, self.home / "current-archive", {name: legacy / name for name in contents})
        self.assertEqual(json.loads(Path(current["manifest_path"]).read_text())["schema_version"], 2)
        self.assertEqual(current["content_sha256"], hashlib.sha256(Path(current["manifest_path"]).read_bytes()).hexdigest())
        self.assertEqual(verify_archive(manifest), old)

    def test_directory_update_keeps_target_and_frozen_image(self):
        original = self.initial_document()
        root = self.root / "local-update"
        (root / "assets").mkdir(parents=True)
        content = b"# Directory revision\n![image](assets/picture.png)\n"
        (root / "replacement.md").write_bytes(content)
        (root / "unselected.md").write_text("Must remain unselected")
        image = (self.vault / "assets/picture.png").read_bytes()
        (root / "assets/picture.png").write_bytes(image)
        preview = self.post("/api/ui/directory-update/preview", {"kb_id": self.kb, "document_id": original["document_id"],
            "absolute_root": str(root), "file": {"filename": "replacement.md", "relative_path": "replacement.md",
            "document_id": original["document_id"], "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}})
        self.assertEqual((preview["file_count"], preview["missing"]), (1, []))
        self.assertEqual(preview["manifest"]["files"][0]["document_id"], original["document_id"])
        self.assertEqual(len(preview["dependency_entries"]), 1)
        task_id = self.accept_preview(preview, "/api/ui/directory-update")
        (root / "assets/picture.png").write_bytes(b"changed after intake")
        updated = self.archive_task(task_id)
        self.assertEqual(updated["document_id"], original["document_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM documents WHERE kb_id=?", (self.kb,)).fetchone()[0], 1)
            payload = IngestionStore(db, self.settings).input_for(task_id)
        stage = self.home / "image-check"
        stage.mkdir()
        members, details, derived = local_members(payload, stage)
        archive = assemble_archive(self.settings, self.home / "image-check-archive", members, derived=derived)
        member_name = details["local_asset_members"][0]["archive_member"]
        self.assertEqual(next(item["sha256"] for item in archive["files"] if item["path"] == member_name), hashlib.sha256(image).hexdigest())

    def test_update_selection_rejects_multiple_cross_kb_and_removed_target(self):
        original = self.initial_document("notes/002.md")
        entries = self.selection()[:2]
        selected = [{"relative_path": item["relative_path"], "signature": item["signature"]} for item in entries]
        body = {"kb_id": self.kb, "document_id": original["document_id"], "entries": selected}
        multiple = self.client.post("/api/ui/obsidian/preview", json=body, headers=self.headers)
        self.assertEqual((multiple.status_code, multiple.json()["code"]), (422, "UPDATE_SINGLE_FILE_REQUIRED"))
        body["entries"] = selected[:1]
        body["kb_id"] = self.other_kb
        other = self.client.post("/api/ui/obsidian/preview", json=body, headers=self.headers)
        self.assertEqual((other.status_code, other.json()["code"]), (404, "UPDATE_DOCUMENT_NOT_AVAILABLE"))
        body["kb_id"] = self.kb
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE documents SET removed_at=? WHERE document_id=?", ("removed", original["document_id"]))
        removed = self.client.post("/api/ui/obsidian/preview", json=body, headers=self.headers)
        self.assertEqual((removed.status_code, removed.json()["code"]), (404, "UPDATE_DOCUMENT_NOT_AVAILABLE"))
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_directory_update_rejects_browser_byte_mismatch_before_preview(self):
        original = self.initial_document("notes/002.md")
        before = None
        with closing(open_db(self.settings.sqlite)) as db:
            before = db.execute("SELECT count(*) FROM import_previews").fetchone()[0]
        response = self.client.post("/api/ui/directory-update/preview", headers=self.headers, json={
            "kb_id": self.kb, "document_id": original["document_id"], "absolute_root": str(self.vault),
            "file": {"filename": "002.md", "relative_path": "notes/002.md", "sha256": "0" * 64,
                     "size_bytes": (self.vault / "notes/002.md").stat().st_size}})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "DIRECTORY_UPDATE_FILE_CHANGED"))
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM import_previews").fetchone()[0], before)
            db.execute("UPDATE system_state SET mode=? WHERE singleton=1", ("QUERY",))
        blocked = self.client.post("/api/ui/directory-update/preview", headers=self.headers, json={
            "kb_id": self.kb, "document_id": original["document_id"], "absolute_root": str(self.vault),
            "file": {"filename": "002.md", "relative_path": "notes/002.md", "sha256": "0" * 64, "size_bytes": 0}})
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "IMPORT_REQUIRED"))


    def test_update_cannot_take_over_another_document_source(self):
        source_document = self.initial_document("notes/000.md")
        target_document = self.initial_document("notes/002.md")
        selected = self.post("/api/ui/obsidian/selection", {"paths": ["notes/000.md"], "directories": []})["entries"]
        preview = self.post("/api/ui/obsidian/preview", {"kb_id": self.kb, "document_id": target_document["document_id"],
            "entries": [{"relative_path": item["relative_path"], "signature": item["signature"]} for item in selected]})
        self.post("/api/import/directory/previews/" + preview["preview_id"] + "/confirm",
                  {"preview_sha256": preview["preview_sha256"], "confirmed": True})
        batch = self.post("/api/import/batches", {"kb_id": self.kb, "items": preview["manifest"]["files"],
            "preview_id": preview["preview_id"], "preview_sha256": preview["preview_sha256"]}, key=new_id())
        response = self.client.post("/api/ui/obsidian/batches/" + batch["batch_id"] + "/items/" + batch["items"][0]["item_id"] + "/receive",
                                    json={}, headers=self.headers)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "SOURCE_DOCUMENT_CONFLICT"))
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
            for document in (source_document, target_document):
                self.assertEqual(db.execute("SELECT current_raw_version_id FROM documents WHERE document_id=?",
                    (document["document_id"],)).fetchone()[0], document["current_raw_version_id"])


    def test_outside_path_and_existing_file_upload(self):
        rejected = self.client.post("/api/ui/obsidian/selection",
                                    json={"paths": ["../outside.md"], "directories": []}, headers=self.headers)
        self.assertEqual((rejected.status_code, rejected.json()["code"]), (422, "OBSIDIAN_PATH_INVALID"))
        (self.root / "outside.md").write_text("outside sentinel")
        (self.vault / "link.md").symlink_to(self.root / "outside.md")
        linked = self.client.post("/api/ui/obsidian/selection",
                                  json={"paths": ["link.md"], "directories": []}, headers=self.headers)
        self.assertEqual((linked.status_code, linked.json()["code"]), (422, "OBSIDIAN_FILE_UNSUPPORTED"))
        content = b"# Existing local upload regression\n"
        batch = self.post("/api/import/batches", {"kb_id": self.other_kb, "items": [{
            "filename": "legacy.md", "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "provenance": {"nature": "ORIGINAL"}}]}, key=new_id())
        response = self.client.post("/api/import/batches/" + batch["batch_id"] + "/items/" + batch["items"][0]["item_id"] + "/file",
                                    files={"file": ("legacy.md", content, "text/markdown")}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.get("/api/import/batches/" + batch["batch_id"])["items"][0]["state"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
