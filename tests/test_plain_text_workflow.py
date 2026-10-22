"""Complete TXT parse-and-clean regression; no cloud or personal-vault writes."""
import json
import unittest
from contextlib import closing
from pathlib import Path

import test_import_workflow as workflow_fixtures
from raghub.db import new_id, open_db
from raghub.parsing.contracts import Supplement, TextLocation


class PlainTextWorkflowTest(unittest.TestCase):
    def setUp(self):
        workflow_fixtures.WorkflowTest.setUp(self)
        from unittest.mock import patch
        from raghub import ui
        bound = patch.object(ui, "settings", self.settings)
        bound.start()
        self.addCleanup(bound.stop)
        self.client.app.include_router(ui.router)
    post = workflow_fixtures.WorkflowTest.post
    get = workflow_fixtures.WorkflowTest.get
    archive_task = workflow_fixtures.WorkflowTest.archive_task
    plan = workflow_fixtures.WorkflowTest.plan

    def test_txt_runs_through_real_parse_clean_and_index_queue(self):
        raw = b'First line\r\n<script>literal only</script>\r\n**Not Markdown**\r\n'
        target = self.vault / "notes" / "plain.txt"
        target.write_bytes(raw)
        preview, body = self.plan("notes/plain.txt")
        flow = self.post("/api/ui/obsidian/workflows", body, key=new_id())
        self.engine.tick(); self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            archive = db.execute("SELECT task_id FROM tasks").fetchone()[0]
        self.archive_task(archive); self.engine.tick()
        queued = self.engine.read(self.instance, flow["workflow_id"])
        parse = queued["items"][0]["parse_task_id"]
        self.assertIsNotNone(parse)
        with closing(open_db(self.settings.sqlite)) as db:
            request_key = db.execute("SELECT request_key FROM tasks WHERE task_id=?", (parse,)).fetchone()[0]
        receipt = self.get("/api/ui/submissions/" + request_key)
        self.assertEqual(receipt["kind"], "task")
        self.assertEqual(receipt["task_id"], parse)
        self.assertEqual(receipt["kb_id"], self.kb)
        self.archive_task(parse); self.engine.tick()
        value = self.engine.read(self.instance, flow["workflow_id"])
        self.assertEqual(value["state"], "INDEX_QUEUED", value)
        self.assertIsNotNone(value["build_task_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            result = db.execute("SELECT bundle_path FROM processing_results WHERE processing_version_id=?",
                (value["items"][0]["processing_version_id"],)).fetchone()
        supplement = Supplement.model_validate_json((Path(result[0]).parent/"supplement.json").read_bytes())
        fields = supplement.fields
        self.assertEqual([f.original_text for f in fields],
                         ["First line", "<script>literal only</script>", "**Not Markdown**"])
        self.assertEqual([f.locator.kind for f in fields], ["text"] * 3)
        self.assertEqual([f.locator.line_start for f in fields], [1, 2, 3])
        self.assertTrue(all(f.locator.line_start == f.locator.line_end for f in fields))
        self.assertEqual(target.read_bytes(), raw)

    def test_text_locator_rejects_reversed_or_zero_lines(self):
        base={"kind":"text","file_sha256":"a"*64,"granularity":"EXACT","basis":"PROJECT_FORMAT_ADAPTER"}
        for start,end in ((0,1),(2,1)):
            with self.subTest(start=start,end=end), self.assertRaises(ValueError):
                TextLocation.model_validate({**base,"line_start":start,"line_end":end})


if __name__ == "__main__":
    unittest.main()
