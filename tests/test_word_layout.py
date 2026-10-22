"""DOCX layout tables keep their content and exact package-part provenance."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches
from docling.backend.msword_backend import MsWordDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.backend_options import MsWordBackendOptions
from docling.document_converter import DocumentConverter, WordFormatOption
from raghub.parsing.source_backends import word_backend


class WordLayoutTest(unittest.TestCase):
    def make_file(self, root, *, rows=1, explicit=None):
        doc = Document()
        body = doc.add_table(rows=1, cols=2)
        body.cell(0, 0).text, body.cell(0, 1).text = "Body A", "Body B"
        for name in ("header", "footer"):
            part = getattr(doc.sections[0], name)
            part.paragraphs[0].text = name + " paragraph"
            table = part.add_table(rows=rows, cols=2, width=Inches(4))
            for i, row in enumerate(table.rows):
                row.cells[0].text, row.cells[1].text = name + str(i) + " A", name + str(i) + " B"
            if explicit is not None:
                flag = OxmlElement("w:tblHeader")
                flag.set(qn("w:val"), explicit)
                table.rows[0]._tr.get_or_add_trPr().append(flag)
        path = root / "layout.docx"
        doc.save(path)
        return path

    def convert(self, path, backend):
        return DocumentConverter(format_options={
            InputFormat.DOCX: WordFormatOption(backend=backend,
                backend_options=MsWordBackendOptions(enable_local_fetch=False, enable_remote_fetch=False))
        }).convert(path).document

    def test_bound_one_row_header_footer_are_content_and_body_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="rag-word-") as temp:
            path = self.make_file(Path(temp))
            baseline = self.convert(path, MsWordDocumentBackend)
            observations = {}
            doc = self.convert(path, word_backend("a" * 64, observations))
        self.assertEqual(len(doc.tables), 3)
        self.assertEqual(doc.tables[0].model_dump(), baseline.tables[0].model_dump())
        self.assertEqual({r["part"] for r in observations["_docx_layout_tables"]},
                         {"word/header1.xml", "word/footer1.xml"})
        for table, part in zip(doc.tables[1:], ("word/header1.xml", "word/footer1.xml")):
            self.assertTrue(all(not c.column_header for c in table.data.table_cells))
            self.assertEqual(table.data.num_rows, 1)
            for i, cell in enumerate(table.data.table_cells):
                binding = observations[table.self_ref.removeprefix("#") + "/data/table_cells/" + str(i) + "/text"]
                self.assertEqual(binding["text"], cell.text)
                self.assertEqual({s["part"] for s in binding["locator"]["source_group"]}, {part})
        paragraph_parts = {r["locator"]["source_group"][0]["part"]
                           for k, r in observations.items() if k.startswith("/texts/")}
        self.assertIn("word/header1.xml", paragraph_parts)
        self.assertIn("word/footer1.xml", paragraph_parts)

    def test_explicit_header_and_multiple_rows_are_not_reclassified(self):
        for rows, explicit in ((1, "1"), (1, "0"), (2, None)):
            with self.subTest(rows=rows, explicit=explicit), tempfile.TemporaryDirectory(prefix="rag-word-") as temp:
                path = self.make_file(Path(temp), rows=rows, explicit=explicit)
                baseline = self.convert(path, MsWordDocumentBackend)
                observations = {}
                doc = self.convert(path, word_backend("a" * 64, observations))
                self.assertEqual([t.model_dump() for t in doc.tables], [t.model_dump() for t in baseline.tables])
                self.assertNotIn("_docx_layout_tables", observations)

    def test_unknown_package_part_is_not_bound_or_reclassified(self):
        observations = {}
        base = word_backend("a" * 64, observations)
        class UnknownPart(base):
            def _source_part(self, element):
                return None
        with tempfile.TemporaryDirectory(prefix="rag-word-") as temp:
            path = self.make_file(Path(temp))
            baseline = self.convert(path, MsWordDocumentBackend)
            doc = self.convert(path, UnknownPart)
        self.assertEqual([t.model_dump() for t in doc.tables], [t.model_dump() for t in baseline.tables])
        self.assertFalse(any(k.startswith("/texts/") or k.startswith("/tables/") for k in observations))

    def test_partial_cell_binding_does_not_reclassify_the_table(self):
        original = MsWordDocumentBackend._handle_tables
        def inconsistent(backend, element, doc):
            refs = original(backend, element, doc)
            if element.getroottree().getroot().tag in {qn("w:hdr"), qn("w:ftr")}:
                refs[0].resolve(doc).data.table_cells[0].text += " component mismatch"
            return refs
        observations = {}
        with tempfile.TemporaryDirectory(prefix="rag-word-") as temp:
            path = self.make_file(Path(temp))
            with patch.object(MsWordDocumentBackend, "_handle_tables", inconsistent):
                doc = self.convert(path, word_backend("a" * 64, observations))
        self.assertTrue(all(c.column_header for t in doc.tables[1:] for c in t.data.table_cells))
        self.assertNotIn("_docx_layout_tables", observations)


if __name__ == "__main__":
    unittest.main()

