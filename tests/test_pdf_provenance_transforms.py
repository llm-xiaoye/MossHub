import copy
import unittest

from docling.datamodel.base_models import Cluster, TextElement
from docling.models.stages.reading_order.readingorder_model import ReadingOrderOptions
from docling_core.types.doc import BoundingBox, DocItemLabel, DoclingDocument, ProvenanceItem
from raghub.parsing.source_backends import pdf_reading_order, verify_pdf_text_bindings


class PdfProvenanceTransforms(unittest.TestCase):
    def setUp(self):
        self.observations = {}
        self.model = pdf_reading_order('a' * 64, 9, self.observations, restore_formulas=True)(ReadingOrderOptions())
        self.doc = DoclingDocument(name='provenance-regression')

    def element(self, text, label=DocItemLabel.TEXT, number=1):
        return TextElement(label=label, id=number, page_no=9, text=text,
            cluster=Cluster(id=number, label=label, bbox=BoundingBox(l=10, t=20, r=90, b=40)))

    def add(self, text, label=DocItemLabel.TEXT):
        element = self.element(text, label)
        item, _ = self.model._handle_text_element(element, out_doc=self.doc, current_list=None, parent=None, page_height=100)
        return element, item

    def merge(self, before, incoming, expected, intervals):
        element, item = self.add(before)
        original_box = copy.deepcopy(item.prov[0].bbox)
        self.model._merge_elements(element, self.element(incoming, number=2), item, 100)
        self.assertEqual(item.text, expected)
        self.assertEqual([p.charspan for p in item.prov], intervals)
        self.assertEqual(item.prov[0].bbox, original_box)
        self.assertTrue(all(p.page_no == 9 and 0 <= p.charspan[0] <= p.charspan[1] <= len(item.text) for p in item.prov))
        self.assertEqual(self.observations['transforms'][-1]['source_page'], 9)
        return element, item

    def test_space_concat_retains_separate_regions(self):
        self.merge('alpha', 'beta', 'alpha beta', [(0, 5), (6, 10)])

    def test_hard_hyphen_lowercase_rebases_both_regions(self):
        self.merge('inter-', 'national', 'international', [(0, 5), (5, 13)])

    def test_soft_hyphen_rebases_even_uppercase_continuation(self):
        self.merge('inter\u00ad', 'National', 'interNational', [(0, 5), (5, 13)])

    def test_hard_hyphen_uppercase_is_preserved(self):
        self.merge('B-', 'Mode', 'B- Mode', [(0, 2), (3, 7)])

    def test_unicode_offsets_count_codepoints(self):
        self.merge('😀inter-', 'national', '😀international', [(0, 6), (6, 14)])

    def test_repeated_merge_keeps_previous_regions(self):
        element, item = self.merge('inter-', 'nation-', 'internation-', [(0, 5), (5, 12)])
        self.model._merge_elements(element, self.element('al', number=3), item, 100)
        self.assertEqual(item.text, 'international')
        self.assertEqual([p.charspan for p in item.prov], [(0, 5), (5, 11), (11, 13)])

    def test_removed_only_region_stays_in_trace(self):
        element, item = self.add('word-')
        item.prov.append(ProvenanceItem(page_no=9, bbox=copy.deepcopy(item.prov[0].bbox), charspan=(4, 5)))
        self.model._merge_elements(element, self.element('ing', number=2), item, 100)
        self.assertEqual([p.charspan for p in item.prov], [(0, 4), (4, 7)])
        self.assertEqual(self.observations['transforms'][-1]['non_text_provenance'][0]['charspan'], [4, 5])

    def test_empty_continuation_has_no_body_region(self):
        _, item = self.merge('word', '', 'word ', [(0, 4)])
        self.assertEqual(len(self.observations['transforms'][-1]['non_text_provenance']), 1)

    def test_whitespace_continuation_preserves_exact_component_text(self):
        self.merge('word-', '   ', 'word-    ', [(0, 5), (6, 9)])

    def test_invalid_input_span_is_rejected_before_merge(self):
        element, item = self.add('word-')
        item.prov[0].charspan = (0, 50)
        with self.assertRaisesRegex(RuntimeError, 'pre-transform provenance'):
            self.model._merge_elements(element, self.element('ing', number=2), item, 100)
        self.assertEqual(item.text, 'word-')

    def test_list_marker_maps_exact_kept_substring(self):
        _, item = self.add('- alpha 😀\nnext', DocItemLabel.LIST_ITEM)
        self.assertEqual(item.text, 'alpha 😀\nnext')
        self.assertEqual(item.orig, '- alpha 😀\nnext')
        self.assertEqual(item.prov[0].charspan, (0, len(item.text)))
        trace = self.observations['transforms'][0]
        self.assertEqual(trace['kept_input_interval'], [2, len(item.orig)])
        self.assertEqual(trace['before_provenance'][0]['charspan'], [0, len(item.orig)])

    def test_numbered_list_marker_maps_content(self):
        _, item = self.add('12. alpha', DocItemLabel.LIST_ITEM)
        self.assertEqual(item.text, 'alpha')
        self.assertEqual(item.prov[0].charspan, (0, 5))

    def test_formula_uses_actual_extracted_orig_when_enrichment_disabled(self):
        _, item = self.add('x + y = z', DocItemLabel.FORMULA)
        self.assertEqual(item.text, 'x + y = z')
        self.assertEqual(item.prov[0].charspan, (0, 9))
        self.assertEqual(self.observations['transforms'][0]['basis'], 'ORIGINAL_EXTRACTED_TEXT_NOT_FORMULA_ENRICHMENT')

    def test_formula_restoration_is_not_enabled_implicitly(self):
        model = pdf_reading_order('a' * 64, 9, {})(ReadingOrderOptions())
        item, _ = model._handle_text_element(self.element('x+y', DocItemLabel.FORMULA),
            out_doc=self.doc, current_list=None, parent=None, page_height=100)
        self.assertEqual(item.text, '')
        self.assertEqual(item.orig, 'x+y')

    def test_empty_formula_does_not_create_text(self):
        _, item = self.add('', DocItemLabel.FORMULA)
        self.assertEqual(item.text, '')
        self.assertNotIn('transforms', self.observations)

    def test_formula_merge_does_not_lose_first_extracted_region(self):
        element, item = self.add('x+y', DocItemLabel.FORMULA)
        self.model._merge_elements(element, self.element('=z', DocItemLabel.FORMULA, 2), item, 100)
        self.assertEqual(item.text, 'x+y =z')
        self.assertEqual([p.charspan for p in item.prov], [(0, 3), (4, 6)])

    def test_final_binding_rejects_same_length_text_change(self):
        _, item = self.add('alpha')
        self.observations['text_bindings'] = {i.self_ref: {'text': i.text, 'orig': i.orig, 'provenance': [p.model_dump(mode='json') for p in i.prov]} for i in self.doc.texts}
        self.assertEqual(verify_pdf_text_bindings(self.doc, self.observations), 1)
        item.text = 'omega'
        with self.assertRaisesRegex(RuntimeError, 'text changed'):
            verify_pdf_text_bindings(self.doc, self.observations)

    def test_final_binding_rejects_changed_orig(self):
        _, item = self.add('alpha')
        state = {i.self_ref: {'text': i.text, 'orig': i.orig, 'provenance': [p.model_dump(mode='json') for p in i.prov]} for i in self.doc.texts}
        item.orig = 'omega'
        with self.assertRaisesRegex(RuntimeError, 'text changed'):
            verify_pdf_text_bindings(self.doc, {'text_bindings': state})

    def test_final_binding_rejects_in_bounds_provenance_shift(self):
        _, item = self.add('alpha')
        state = {i.self_ref: {'text': i.text, 'orig': i.orig, 'provenance': [p.model_dump(mode='json') for p in i.prov]} for i in self.doc.texts}
        item.prov[0].charspan = (1, 5)
        with self.assertRaisesRegex(RuntimeError, 'text changed'):
            verify_pdf_text_bindings(self.doc, {'text_bindings': state})

    def test_trace_separates_component_merge_from_original_state(self):
        self.merge('inter-', 'national', 'international', [(0, 5), (5, 13)])
        trace = self.observations['transforms'][-1]
        self.assertEqual(trace['before_text_length'], 6)
        self.assertEqual([p['charspan'] for p in trace['before_provenance']], [[0, 6]])
        self.assertEqual([p['charspan'] for p in trace['component_postmerge_provenance']], [[0, 6], [7, 15]])
        self.assertEqual([p['charspan'] for p in trace['after_provenance']], [[0, 5], [5, 13]])


if __name__ == '__main__':
    unittest.main()
