"""Original Word sections and import-slot reads remain plain, scoped and read-only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from docx import Document
import test_import_workflow as fixtures
from raghub.docx_source import original_rows
from raghub import ui


class WordOriginalSourceTest(unittest.TestCase):
    def test_body_header_footer_are_visible_with_separate_xml_bindings(self):
        with tempfile.TemporaryDirectory(prefix="rag-word-source-") as temp:
            path = Path(temp) / "original.docx"
            doc = Document()
            doc.add_paragraph("Main text")
            doc.sections[0].header.paragraphs[0].text = "Header text"
            doc.sections[0].footer.paragraphs[0].text = "Footer text"
            doc.save(path)
            before = path.read_bytes()
            rows = original_rows(path)
            self.assertEqual(path.read_bytes(), before)
        selected = {row["text"]: row for row in rows}
        self.assertEqual(selected["Main text"]["part"], "word/document.xml")
        self.assertEqual(selected["Main text"]["section"], "正文")
        self.assertEqual(selected["Header text"]["part"], "word/header1.xml")
        self.assertEqual(selected["Header text"]["section"], "页眉")
        self.assertTrue(selected["Header text"]["xml_path"].startswith("/w:hdr/"))
        self.assertEqual(selected["Footer text"]["part"], "word/footer1.xml")
        self.assertEqual(selected["Footer text"]["section"], "页脚")
        self.assertTrue(selected["Footer text"]["xml_path"].startswith("/w:ftr/"))


class IntakeSlotTest(unittest.TestCase):
    setUp = fixtures.WorkflowTest.setUp
    post = fixtures.WorkflowTest.post
    get = fixtures.WorkflowTest.get
    plan = fixtures.WorkflowTest.plan
    create = fixtures.WorkflowTest.create

    def test_slot_is_busy_until_the_original_batch_has_ended(self):
        with patch.object(ui, "settings", self.settings):
            self.assertEqual(ui.import_availability(self.instance), {"busy": False})
            _, flow = self.create()
            self.assertEqual(ui.import_availability(self.instance), {"busy": True})
            self.engine.action(self.instance, flow["workflow_id"], "finish")
            self.assertEqual(ui.import_availability(self.instance), {"busy": False})


if __name__ == "__main__":
    unittest.main()

