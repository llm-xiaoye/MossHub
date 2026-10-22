"""Document status follows the actual current published snapshot, not parser readiness."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sqlite3
import tempfile
import unittest

from raghub import ui


class DocumentPublicationViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rag-ui-publication-")
        self.path = Path(self.temp.name)/"state.sqlite3"
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE knowledge_bases(kb_id TEXT PRIMARY KEY,current_generation_id TEXT);
                CREATE TABLE generations(generation_id TEXT PRIMARY KEY,kb_id TEXT,state TEXT);
                CREATE TABLE documents(document_id TEXT PRIMARY KEY,kb_id TEXT,current_raw_version_id TEXT,
                    current_processing_version_id TEXT,active_prepare_task_id TEXT,removed_at INTEGER,created_at INTEGER);
                CREATE TABLE processing_versions(processing_version_id TEXT PRIMARY KEY,status TEXT);
                CREATE TABLE tasks(task_id TEXT PRIMARY KEY,status TEXT,stage TEXT);
                CREATE TABLE generation_documents(generation_id TEXT,document_id TEXT,raw_version_id TEXT,
                    processing_version_id TEXT,PRIMARY KEY(generation_id,document_id));
                INSERT INTO knowledge_bases VALUES('kb','gen');
                INSERT INTO generations VALUES('gen','kb','CURRENT');
                INSERT INTO documents VALUES('doc','kb','raw1','pv1',NULL,NULL,1);
                INSERT INTO processing_versions VALUES('pv1','READY');
                INSERT INTO generation_documents VALUES('gen','doc','raw1','pv1');
                ALTER TABLE documents ADD COLUMN display_name TEXT DEFAULT 'example.md';
                CREATE TABLE source_observations(observation_id TEXT,task_id TEXT,source_id TEXT,
                    raw_version_id TEXT,details_json TEXT,created_at TEXT);
                CREATE TABLE task_inputs(task_id TEXT,document_id TEXT,kb_id TEXT,source_id TEXT,payload_json TEXT);
                CREATE TABLE document_sources(source_id TEXT,document_id TEXT,kb_id TEXT,relative_path TEXT,
                    source_root_id TEXT,kind TEXT);
                CREATE TABLE source_roots(source_root_id TEXT,absolute_path TEXT);
            """)
        self.addCleanup(self.temp.cleanup)

    def mutate(self, sql, parameters=()):
        with sqlite3.connect(self.path) as db:
            db.execute(sql, parameters)

    def read(self, expected_visible=True):
        def connect(_path):
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            return db
        with patch.object(ui, "settings", SimpleNamespace(sqlite=self.path)), patch.object(ui, "open_db", connect):
            result = ui.documents("kb", 0, 50)
        self.assertEqual(result["total"], 1 if expected_visible else 0)
        self.assertEqual(len(result["items"]), 1 if expected_visible else 0)
        return result["items"][0] if expected_visible else None

    def test_current_version_matches_the_real_published_snapshot(self):
        row = self.read()
        self.assertIs(row["has_published_version"], True)
        self.assertIs(row["current_version_published"], True)
        self.assertEqual((row["published_generation_id"], row["published_raw_version_id"],
                          row["published_processing_version_id"]), ("gen", "raw1", "pv1"))

    def test_ready_document_without_publication_is_not_answerable_by_readiness(self):
        self.mutate("DELETE FROM generation_documents")
        row = self.read()
        self.assertEqual(row["processing_status"], "READY")
        self.assertIs(row["has_published_version"], False)
        self.assertIs(row["current_version_published"], False)

    def test_reprocessed_version_keeps_old_published_identity_separate(self):
        self.mutate("INSERT INTO processing_versions VALUES('pv2','READY')")
        self.mutate("UPDATE documents SET current_processing_version_id='pv2'")
        row = self.read()
        self.assertIs(row["has_published_version"], True)
        self.assertIs(row["current_version_published"], False)
        self.assertEqual(row["published_processing_version_id"], "pv1")

    def test_new_raw_version_does_not_inherit_old_publication(self):
        self.mutate("UPDATE documents SET current_raw_version_id='raw2',current_processing_version_id=NULL")
        row = self.read()
        self.assertIs(row["has_published_version"], True)
        self.assertIs(row["current_version_published"], False)
        self.assertEqual(row["published_raw_version_id"], "raw1")

    def test_failed_update_still_identifies_the_previous_published_version(self):
        self.mutate("INSERT INTO processing_versions VALUES('pv2','FAILED')")
        self.mutate("UPDATE documents SET current_processing_version_id='pv2'")
        row = self.read()
        self.assertEqual(row["processing_status"], "FAILED")
        self.assertIs(row["has_published_version"], True)
        self.assertIs(row["current_version_published"], False)
        self.assertEqual(row["published_processing_version_id"], "pv1")

    def test_removed_document_is_not_marked_as_published_for_answering(self):
        self.mutate("UPDATE documents SET removed_at=2")
        self.assertIsNone(self.read(expected_visible=False))

    def test_pointer_to_another_knowledge_base_is_not_accepted(self):
        self.mutate("UPDATE generations SET kb_id='other'")
        row = self.read()
        self.assertIs(row["has_published_version"], False)
        self.assertIs(row["current_version_published"], False)
        self.assertIsNone(row["published_generation_id"])

    def test_retired_generation_is_not_presented_as_the_current_publication(self):
        self.mutate("UPDATE generations SET state='RETIRED'")
        row = self.read()
        self.assertIs(row["has_published_version"], False)
        self.assertIs(row["current_version_published"], False)


if __name__ == "__main__":
    unittest.main()
