"""A failed build can continue only after the original publication is known aborted."""
import json
import unittest
from contextlib import closing
import test_import_workflow as fixtures
from raghub.db import new_id, now, open_db
from raghub.ingestion.store import BusinessError


class BuildRecoveryTest(unittest.TestCase):
    setUp = fixtures.WorkflowTest.setUp
    post = fixtures.WorkflowTest.post
    get = fixtures.WorkflowTest.get
    archive_task = fixtures.WorkflowTest.archive_task
    plan = fixtures.WorkflowTest.plan
    create = fixtures.WorkflowTest.create

    def queued(self):
        _, flow = self.create()
        self.engine.tick(); self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            archive = db.execute("SELECT task_id FROM tasks").fetchone()[0]
        self.archive_task(archive); self.engine.tick()
        parse = self.engine.read(self.instance, flow["workflow_id"])["items"][0]["parse_task_id"]
        self.archive_task(parse); self.engine.tick()
        value = self.engine.read(self.instance, flow["workflow_id"])
        return flow["workflow_id"], value["build_task_id"]

    def stopped(self, *, candidate_state="ABORTED", create_candidate=True):
        flow, task = self.queued()
        generation = new_id()
        with closing(open_db(self.settings.sqlite)) as db:
            payload = json.loads(db.execute("SELECT payload_json FROM generation_inputs WHERE task_id=?", (task,)).fetchone()[0])
            record = db.execute("SELECT * FROM tasks WHERE task_id=?", (task,)).fetchone()
            if create_candidate:
                db.execute("INSERT INTO generations(generation_id,kb_id,task_id,attempt_id,state,collection_name,"
                           "embedding_space_id,execution_profile_id,input_sha256,expected_previous_generation_id,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (generation, self.kb, task, record["current_attempt_id"], candidate_state,
                            "test_" + generation.replace("-", ""), payload["profile"]["embedding_space_id"],
                            payload["profile"]["execution_profile_id"], record["request_sha256"],
                            payload["expected_previous_generation_id"], now()))
            db.execute("UPDATE tasks SET status='FAILED' WHERE task_id=?", (task,))
            db.execute("UPDATE outbox SET state='CANCELLED' WHERE task_id=?", (task,))
            db.commit()
        self.engine.tick()
        self.assertEqual(self.engine.read(self.instance, flow)["state"], "FAILED")
        return flow, task, generation, record["request_key"]

    def test_aborted_attempt_is_preserved_and_new_build_is_queued_once(self):
        flow, task, generation, key = self.stopped()
        resumed = self.engine.action(self.instance, flow, "resume")
        self.assertIsNone(resumed["build_task_id"])
        self.engine.tick()
        replacement = self.engine.read(self.instance, flow)["build_task_id"]
        self.assertNotEqual(replacement, task)
        self.engine.action(self.instance, flow, "resume")
        self.engine.tick()
        self.assertEqual(self.engine.read(self.instance, flow)["build_task_id"], replacement)
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT state FROM generations WHERE generation_id=?", (generation,)).fetchone()[0], "ABORTED")
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "FAILED")
            self.assertNotEqual(db.execute("SELECT request_key FROM tasks WHERE task_id=?", (replacement,)).fetchone()[0], key)
            data = json.loads(db.execute("SELECT data_json FROM import_workflows WHERE workflow_id=?", (flow,)).fetchone()[0])
            self.assertEqual(data["prior_builds"][0]["task_id"], task)
            self.assertEqual(data["prior_builds"][0]["generation_id"], generation)

    def test_live_or_unknown_model_work_prevents_a_new_build_key(self):
        from unittest.mock import patch
        flow, task, _, key = self.stopped()
        with patch("raghub.indexing.model_client.require_task_operations_stopped",
                   side_effect=BusinessError("OLD_MODEL_OPERATION_ACTIVE_OR_UNKNOWN", "still running")) as gate:
            with self.assertRaises(BusinessError):
                self.engine.action(self.instance, flow, "resume")
            gate.assert_called_once()
        with closing(open_db(self.settings.sqlite)) as db:
            data = json.loads(db.execute("SELECT data_json FROM import_workflows WHERE workflow_id=?", (flow,)).fetchone()[0])
            self.assertEqual(data["build_request_key"], key)
            self.assertEqual(data["build_task_id"], task)

    def assert_blocked_candidate(self, state, present):
        flow, task, _, key = self.stopped(candidate_state=state, create_candidate=present)
        with self.assertRaises(BusinessError) as error:
            self.engine.action(self.instance, flow, "resume")
        self.assertEqual(error.exception.code, "WORKFLOW_BUILD_RECOVERY_UNCONFIRMED")
        with closing(open_db(self.settings.sqlite)) as db:
            data = json.loads(db.execute("SELECT data_json FROM import_workflows WHERE workflow_id=?", (flow,)).fetchone()[0])
            self.assertEqual(data["build_request_key"], key)
            self.assertEqual(data["build_task_id"], task)

    def test_missing_candidate_cannot_get_a_new_request(self):
        self.assert_blocked_candidate("ABORTED", False)

    def test_unconfirmed_candidate_cannot_get_a_new_request(self):
        self.assert_blocked_candidate("CHECKING", True)


if __name__ == "__main__":
    unittest.main()
