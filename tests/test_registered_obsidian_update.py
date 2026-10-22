"""Registered Obsidian updates bind the original document and frozen source."""
import unittest
from contextlib import closing
from unittest.mock import patch
from PIL import Image

import test_obsidian_import as base
from raghub.db import new_id, open_db
from raghub.obsidian import Vault
from raghub.obsidian_state import ObsidianState


class RegisteredUpdateTest(unittest.TestCase):
    setUp = base.ObsidianFunctionalTest.setUp
    post = base.ObsidianFunctionalTest.post
    get = base.ObsidianFunctionalTest.get
    accept_preview = base.ObsidianFunctionalTest.accept_preview
    archive_task = base.ObsidianFunctionalTest.archive_task
    initial_document = base.ObsidianFunctionalTest.initial_document

    def target(self, document):
        return self.get("/api/ui/documents/" + document["document_id"] + "/obsidian-target?fresh=true")

    def preview_body(self, document, target, path="notes/002.md"):
        entries = self.post("/api/ui/obsidian/selection", {"paths": [path], "directories": []})["entries"]
        return {"kb_id": self.kb, "document_id": document["document_id"],
                "expected_raw_version_id": document["current_raw_version_id"],
                "registered_source_id": target["source_id"],
                "entries": [{"relative_path": path, "signature": entries[0]["signature"]}]}

    def test_unchanged_original_is_reported_without_creating_update_tasks(self):
        document = self.initial_document("notes/002.md")
        with closing(open_db(self.settings.sqlite)) as db:
            count = db.execute("select count(*) from tasks").fetchone()[0]
        target = self.target(document)
        self.assertTrue(target["available"])
        self.assertEqual(target["state"], "UNCHANGED")
        self.assertEqual(target["relative_path"], "notes/002.md")
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("select count(*) from tasks").fetchone()[0], count)
            self.assertEqual(db.execute("select count(*) from import_workflows").fetchone()[0], 0)

    def test_changed_original_freezes_only_that_note_and_reuses_request_identity(self):
        document = self.initial_document("notes/002.md")
        path = self.vault / "notes/002.md"
        path.write_text(path.read_text() + "\n新增的真实测试修订。\n")
        target = self.target(document)
        self.assertEqual(target["state"], "CHANGED")
        preview = self.post("/api/ui/obsidian/preview", self.preview_body(document, target))
        files = preview["manifest"]["files"]
        self.assertEqual([(row["relative_path"], row["document_id"]) for row in files],
                         [("notes/002.md", document["document_id"])])
        body = {"kb_id": self.kb, "preview_id": preview["preview_id"],
                "preview_sha256": preview["preview_sha256"], "confirmed": True}
        key = new_id()
        first = self.post("/api/ui/obsidian/workflows", body, key=key)
        second = self.post("/api/ui/obsidian/workflows", body, key=key)
        self.assertEqual(first["workflow_id"], second["workflow_id"])
        self.assertEqual(first["total"], 1)
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("select count(*) from import_workflows").fetchone()[0], 1)
            self.assertEqual(db.execute("select count(*) from documents where kb_id=?", (self.kb,)).fetchone()[0], 1)

    def test_a_different_note_cannot_replace_the_registered_original(self):
        document = self.initial_document("notes/002.md")
        target = self.target(document)
        response = self.client.post("/api/ui/obsidian/preview",
                                    json=self.preview_body(document, target, "notes/003.md"), headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "OBSIDIAN_SOURCE_BINDING_CHANGED")

    def test_a_changed_document_version_is_rejected_before_preview(self):
        document = self.initial_document("notes/002.md")
        target = self.target(document)
        body = self.preview_body(document, target)
        body["expected_raw_version_id"] = new_id()
        response = self.client.post("/api/ui/obsidian/preview", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "OBSIDIAN_SOURCE_BINDING_CHANGED")

    def test_image_change_is_detected_and_missing_original_keeps_archive(self):
        document = self.initial_document("notes/000.md")
        self.assertEqual(self.target(document)["state"], "UNCHANGED")
        Image.new("RGB", (2, 2), "red").save(self.vault / "assets/picture.png")
        target = self.target(document)
        self.assertEqual(target["state"], "CHANGED")
        self.assertEqual(target["note_state"], "UNCHANGED")
        self.assertEqual(target["changes"][0]["kind"], "IMAGE")
        (self.vault / "notes/000.md").unlink()
        self.assertFalse(self.target(document)["available"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("select current_raw_version_id from documents where document_id=?",
                                       (document["document_id"],)).fetchone()[0], document["current_raw_version_id"])

    def test_document_list_only_offers_update_for_the_fixed_obsidian_root(self):
        from raghub import ui
        document = self.initial_document("notes/002.md")
        registered = ObsidianState(Vault(self.settings, self.vault))
        with patch.object(ui, "settings", self.settings), patch.object(ui.obsidian_router, "registered_sources", registered):
            self.assertTrue(ui.documents(self.kb, 0, 50)["items"][0]["can_update"])
        other = self.root / "other-local-directory"
        other.mkdir()
        registered = ObsidianState(Vault(self.settings, other))
        with patch.object(ui, "settings", self.settings), patch.object(ui.obsidian_router, "registered_sources", registered):
            self.assertFalse(ui.documents(self.kb, 0, 50)["items"][0]["can_update"])
