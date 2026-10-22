from __future__ import annotations
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from raghub.parsing import formats


class EmptyListContainerTests(unittest.TestCase):
    def bind(self, children):
        container=SimpleNamespace(self_ref='#/texts/0',text='',label=SimpleNamespace(value='list_item'),children=children)
        child=SimpleNamespace(self_ref='#/texts/1',text='只适用于当前版本。',label=SimpleNamespace(value='list_item'),children=[])
        items=[(container,0),(child,1)]
        document=SimpleNamespace(iterate_items=lambda:iter(items))
        parsed=SimpleNamespace(document=document,details={},locations={},quality=[])
        parsed.issue=lambda severity,code,**details:parsed.quality.append({'severity':severity,'code':code,**details})
        locator={'kind':'md','file_sha256':'a'*64,'line_start':2,'line_end':3,'source_start':2,'source_end':20,'granularity':'REGION'}
        anchors=[{'id':'child','text':child.text,'kind':'text','plain':False,'locator':locator}]
        with patch.object(formats,'raw_anchors',return_value=anchors):
            result=formats.bind_native(parsed,Path('note.md'),'md','a'*64)
        return result,container,child

    def test_empty_parent_is_skipped_and_child_stays_bound(self):
        children=[SimpleNamespace(cref='#/texts/1')]
        result,container,child=self.bind(children)
        self.assertEqual(result.quality,[])
        self.assertIn('/texts/1/text',result.locations)
        self.assertNotIn('/texts/0/text',result.locations)
        self.assertIs(container.children,children)
        self.assertEqual(child.text,'只适用于当前版本。')
        self.assertIn('/texts/1/text',result.details['protected_fields'])

    def test_field_enumeration_retains_empty_container_for_v1_bundle_compatibility(self):
        native={"texts":[{"label":"list_item","text":"","children":[{"cref":"#/texts/1"}]},
                         {"label":"list_item","text":"只适用于当前版本。","children":[]}],"tables":[]}
        self.assertEqual(list(formats.field_values(native)),
            [("/texts/0/text","","list_item"),("/texts/1/text","只适用于当前版本。","list_item")])

    def test_empty_leaf_is_not_silently_accepted(self):
        result,_,_=self.bind([])
        self.assertIn('/texts/1/text',result.locations)
        self.assertTrue(any(issue['code']=='COARSE_SOURCE_MAPPING' and issue['field']=='/texts/0/text' for issue in result.quality))


if __name__=='__main__':
    unittest.main()
