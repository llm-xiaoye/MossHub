"""Real image-format and Markdown dependency regression tests; no network/provider."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from raghub.svg_image import read_svg
from raghub.parsing.formats import image_document

class MarkdownImageSupportTest(unittest.TestCase):
    def scanner(self):
        path=Path(__file__).resolve().parents[1]/'scripts/directory.py'
        spec=importlib.util.spec_from_file_location('directory_images_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_html_images_and_fenced_examples_are_distinguished(self):
        scanner=self.scanner()
        content=b'<div><img src="./chart.png" /></div>\n\nInline <img src="./hero.svg"/> ![a](./movie.gif)\n\n'+bytes([96])*3+b'html\n<img src="./example.png">\n'+bytes([96])*3+b'\n'
        refs=scanner._references(content)
        self.assertEqual(set(refs),{('HTML','./chart.png'),('HTML','./hero.svg'),('MARKDOWN','./movie.gif')})
        self.assertEqual(scanner._relation('note.md','HTML','./hero.svg',{'hero.svg'})['status'],'RESOLVED')

    def test_svg_visible_text_and_numbers_bind_without_ocr(self):
        with tempfile.TemporaryDirectory(prefix='rag-svg-') as root:
            path=Path(root)/'chart.svg'
            path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100"><title>Metadata only</title><text x="0" y="20">Model <tspan>4.7</tspan></text></svg>')
            raw,rows,size=read_svg(path)
            self.assertEqual([row['text'] for row in rows],['Model 4.7'])
            self.assertEqual(size,(300,100))
            with patch('raghub.parsing.formats.rapid_engine',side_effect=AssertionError('SVG must not initialize OCR')):
                parsed=image_document(path,{})
            self.assertEqual(parsed.document.texts[0].text,'Model 4.7')
            self.assertIn('svg_xml_path',parsed.locations['/texts/0/text']['transform'])

    def test_svg_active_and_external_content_rejected(self):
        cases=['<script>alert(1)</script>','<rect onclick="x()"/>','<use href="https://example.com/a.svg#x"/>','<rect fill="url(https://example.com/a)"/>','<foreignObject><p>test</p></foreignObject>']
        with tempfile.TemporaryDirectory(prefix='rag-svg-') as root:
            for i,body in enumerate(cases):
                with self.subTest(body=body):
                    path=Path(root)/(str(i)+'.svg');path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'+body+'</svg>')
                    with self.assertRaises(ValueError):read_svg(path)

    def test_gif_processes_all_frames_and_deduplicates_identical_observations(self):
        with tempfile.TemporaryDirectory(prefix='rag-gif-') as root:
            path=Path(root)/'movie.gif'
            frames=[Image.new('RGB',(30,20),color) for color in ['red','blue']]
            frames[0].save(path,save_all=True,append_images=frames[1:],duration=100,loop=0)
            calls=[]
            def ocr(array,**kwargs):
                calls.append(array.shape)
                return SimpleNamespace(boxes=[[[0,0],[20,0],[20,10],[0,10]]],txts=['Score 42'],scores=[.99])
            with patch('raghub.parsing.formats.rapid_engine',return_value=ocr):
                parsed=image_document(path,{},cache_dir=Path(root))
            self.assertEqual(len(calls),2)
            self.assertEqual(parsed.details['decoded_frame_count'],2)
            self.assertEqual(len(parsed.document.texts),1)
            self.assertEqual([x['frame'] for x in parsed.locations['/texts/0/text']['source_group']],[1,2])
            self.assertTrue(all(page.image is None for page in parsed.document.pages.values()))
            self.assertTrue(all((Path(root)/image['path']).is_file() for image in parsed.images))
            self.assertEqual(parsed.quality[0]['code'],'OCR_REVIEW_CANDIDATE')

    def test_gif_changed_numbers_keep_distinct_text_and_frames(self):
        with tempfile.TemporaryDirectory(prefix='rag-gif-') as root:
            path=Path(root)/'movie.gif'
            frames=[Image.new('RGB',(30,20),color) for color in ['red','blue']]
            frames[0].save(path,save_all=True,append_images=frames[1:],duration=100)
            values=iter(['Score 42','Score 43'])
            def ocr(array,**kwargs):return SimpleNamespace(boxes=[[[0,0],[20,0],[20,10],[0,10]]],txts=[next(values)],scores=[.99])
            with patch('raghub.parsing.formats.rapid_engine',return_value=ocr):parsed=image_document(path,{})
            self.assertEqual([x.text for x in parsed.document.texts],['Score 42','Score 43'])
            self.assertEqual([x['frame'] for x in parsed.locations.values()],[1,2])

    def test_large_static_bitmap_tiles_without_downsampling_original(self):
        from raghub.ingestion.archive import hash_file
        with tempfile.TemporaryDirectory(prefix='rag-large-bitmap-') as root:
            path=Path(root)/'chart.png';Image.new('RGB',(5000,4200),'white').save(path)
            calls=[]
            def ocr(array,**kwargs):
                calls.append(array.shape)
                return SimpleNamespace(boxes=[[[0,0],[20,0],[20,10],[0,10]]],txts=['Value 42'],scores=[.99])
            with patch('raghub.parsing.formats.rapid_engine',return_value=ocr):
                parsed=image_document(path,{},cache_dir=Path(root))
            self.assertGreater(len(calls),1)
            self.assertTrue(all(max(shape[:2])<=1536 for shape in calls))
            self.assertEqual(parsed.details['decoded_pixels'],21000000)
            self.assertEqual(parsed.document.pages[1].size.width,5000)
            self.assertTrue(parsed.details['tiled_ocr'])
            self.assertIsNone(parsed.document.pages[1].image)
            self.assertEqual(hash_file(Path(root)/parsed.images[0]['path']),hash_file(path))

if __name__=='__main__':unittest.main()

