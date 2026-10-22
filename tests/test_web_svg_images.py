"""Actual image-decoder and installed-browser SVG regression coverage."""
import asyncio
import base64
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from PIL import Image
from raghub.ingestion.svg_render import svg_markup, SvgImageError
from raghub.ingestion.web import download_image, image_bytes, AcquisitionError


class SvgPolicyTest(unittest.TestCase):
    def test_external_active_and_animated_vectors_are_rejected(self):
        sources = [
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.org/x.png"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="x"/></svg>',
            b'<!DOCTYPE svg [<!ENTITY x "outside">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
        ]
        for source in sources:
            with self.subTest(source=source), self.assertRaises(SvgImageError):
                svg_markup(source)

    def test_raster_bytes_stay_unchanged_and_unreadable_content_is_named(self):
        original = BytesIO()
        Image.new("RGB", (10, 10), "green").save(original, format="PNG")
        content = original.getvalue()
        self.assertEqual(image_bytes(content), ".png")
        self.assertIsNone(svg_markup(content))
        with self.assertRaises(AcquisitionError) as caught:
            image_bytes(b"not an image")
        self.assertEqual(caught.exception.code, "IMAGE_UNREADABLE")


class SvgArchiveTest(unittest.IsolatedAsyncioTestCase):
    async def test_installed_browser_renders_and_keeps_exact_svg_original(self):
        source = b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40"><rect width="120" height="40" fill="green"/><text x="4" y="24">SQLite</text></svg>'
        uri = "data:image/svg+xml;base64," + base64.b64encode(source).decode("ascii")
        with tempfile.TemporaryDirectory(prefix="rag-svg-render-") as root:
            path, observation = await download_image(None, uri, {}, time.monotonic()+25, Path(root), allow_svg=True)
            self.assertEqual(path.suffix, ".png")
            self.assertEqual(image_bytes(path.read_bytes()), ".png")
            vector = observation["vector_source"]
            original = Path(root) / Path(vector["local_path"]).name
            self.assertEqual(original.read_bytes(), source)
            self.assertEqual(vector["rendering"]["network"], "BLOCKED")
            self.assertEqual(vector["rendering"]["javascript"], "DISABLED")
            self.assertEqual((vector["rendering"]["width"], vector["rendering"]["height"]), (120, 40))


if __name__ == "__main__":
    unittest.main()
