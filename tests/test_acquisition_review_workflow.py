"""Acquisition resource flags do not prevent creation of a real parsing report."""
import json
import unittest
from contextlib import closing
import test_import_workflow as fixtures
from raghub.db import open_db


class AcquisitionReviewWorkflowTest(unittest.TestCase):
    setUp = fixtures.WorkflowTest.setUp
    post = fixtures.WorkflowTest.post
    get = fixtures.WorkflowTest.get
    plan = fixtures.WorkflowTest.plan
    create = fixtures.WorkflowTest.create
    archive_task = fixtures.WorkflowTest.archive_task

    def archived(self):
        _, flow = self.create()
        self.engine.tick()
        self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            archive = db.execute("SELECT task_id FROM tasks").fetchone()[0]
        self.archive_task(archive)
        return flow

    def test_acquisition_review_queues_one_parse_without_calling_it_ready(self):
        flow = self.archived()
        with closing(open_db(self.settings.sqlite)) as db:
            p = db.execute("SELECT current_processing_version_id FROM documents").fetchone()[0]
            self.assertEqual(db.execute("SELECT count(*) FROM processing_configs").fetchone()[0], 0)
            db.execute("UPDATE processing_versions SET status='REVIEW_REQUIRED' WHERE processing_version_id=?", (p,))
        self.engine.tick()
        first = self.engine.read(self.instance, flow["workflow_id"])
        self.assertEqual(first["items"][0]["state"], "PARSE_QUEUED")
        self.assertIsNotNone(first["items"][0]["parse_task_id"])
        self.assertIsNone(first["build_task_id"])
        self.engine.tick()
        second = self.engine.read(self.instance, flow["workflow_id"])
        self.assertEqual(second["items"][0]["parse_task_id"], first["items"][0]["parse_task_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM processing_configs").fetchone()[0], 1)

    def test_existing_parsed_review_is_neither_reparsed_nor_published(self):
        flow = self.archived()
        self.engine.tick()
        value = self.engine.read(self.instance, flow["workflow_id"])
        parse = value["items"][0]["parse_task_id"]
        self.archive_task(parse)
        with closing(open_db(self.settings.sqlite)) as db:
            p = db.execute("SELECT current_processing_version_id FROM documents").fetchone()[0]
            db.execute("UPDATE processing_versions SET status='REVIEW_REQUIRED' WHERE processing_version_id=?", (p,))
            before = db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        self.engine.tick()
        value = self.engine.read(self.instance, flow["workflow_id"])
        self.assertEqual(value["items"][0]["state"], "REVIEW_REQUIRED")
        self.assertEqual(value["items"][0]["parse_task_id"], parse)
        self.assertIsNone(value["build_task_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
