"""Passive HTML navigation preserves text without resolving archived href targets."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from raghub.parsing.formats import native_document


class ArchivedHtmlNavigationTest(unittest.TestCase):
    def convert(self, markup, sources=None):
        with tempfile.TemporaryDirectory(prefix="rag-html-links-") as root:
            path = Path(root) / "original.html"
            original = markup.encode("utf-8")
            path.write_bytes(original)
            with patch("docling.backend.html_backend.HTMLDocumentBackend._resolve_relative_path",
                       side_effect=AssertionError("navigation must remain passive")):
                parsed = native_document(path, "html", source_nodes=sources)
            self.assertEqual(path.read_bytes(), original)
        return parsed

    def test_local_html_parent_navigation_keeps_visible_text_and_archive(self):
        parsed = self.convert('<html><body><p>Use <a href="../builtins/functions.html#print">print</a> for output.</p></body></html>')
        text = "\n".join(item.text for item in parsed.document.texts)
        self.assertIn("print", text)
        self.assertIn("for output.", text)
        self.assertEqual(parsed.details["hyperlink_policy"], "ARCHIVE_ONLY_LINK_TARGETS_TEXT_PRESERVED")

    def test_annotated_web_body_keeps_source_binding(self):
        parsed = self.convert('<html><body><p data-raghub-source-node="n1">Handle <a href="../errors.html">exceptions</a> safely.</p></body></html>',
                              {"n1": {"xpath": "/html/body/p", "protected": False}})
        self.assertTrue(any("exceptions" in item.text for item in parsed.document.texts))
        self.assertTrue(any(locator.get("xpath") == "/html/body/p" for locator in parsed.locations.values()))

    def test_link_schemes_are_passive_and_text_is_not_dropped(self):
        parsed = self.convert('<html><body><p><a href="file:///private/outside">Local label</a> <a href="javascript:alert(1)">Script label</a> <a href="https://example.org/path">Remote label</a></p></body></html>')
        text = "\n".join(item.text for item in parsed.document.texts)
        for label in ("Local label", "Script label", "Remote label"):
            self.assertIn(label, text)


if __name__ == "__main__":
    unittest.main()
