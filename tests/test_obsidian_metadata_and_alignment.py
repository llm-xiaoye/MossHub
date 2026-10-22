from __future__ import annotations
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import yaml
from raghub.parsing import formats
from raghub.parsing.alignment import align


class ObsidianMetadataAndAlignmentTests(unittest.TestCase):
    def metadata(self, front):
        with tempfile.TemporaryDirectory() as root:
            note=Path(root)/'note.md'
            note.write_text('---\n'+front+'\n---\n\n正文。\n',encoding='utf-8')
            work=Path(root)/'work'
            work.mkdir()
            parsed=SimpleNamespace(details={})
            with patch.object(formats,'native_document',return_value=parsed), patch.object(formats,'bind_native',side_effect=lambda value,*args:value), patch.object(formats,'append_bound_resources'):
                return formats.convert(note,{}, {},work).details['front_matter']

    def test_unquoted_dates_remain_json_metadata_text(self):
        value=self.metadata('date: 2025-01-16\ncreated: 2026-07-04\nupdated: 2026-07-05T10:00:00Z\ncount: 3')
        self.assertEqual(value,{'date':'2025-01-16','created':'2026-07-04','updated':'2026-07-05T10:00:00Z','count':3})
        self.assertEqual(json.loads(json.dumps(value)),value)
        self.assertIsInstance(yaml.safe_load('date: 2025-01-16')['date'],date)

    def test_duplicate_metadata_keys_remain_rejected(self):
        with self.assertRaises(ValueError):
            self.metadata('date: 2025-01-16\ndate: 2026-07-04')

    def alignment(self, texts, actual, field_plain=True, protected=False, duplicate=False):
        loc={'kind':'md','file_sha256':'a'*64,'line_start':8,'line_end':11,'source_start':100,'source_end':180,'granularity':'REGION','basis':'ORIGINAL_MARKDOWN_INLINE_NODE_IN_BLOCK'}
        anchors=[{'id':'before','text':'前面的标题','kind':'paragraph','plain':False}]
        anchors.extend({'id':str(i),'text':text,'kind':'paragraph','plain':not protected,'locator':dict(loc)} for i,text in enumerate(texts))
        anchors.append({'id':'after','text':'后面的标题','kind':'paragraph','plain':False})
        fields=[{'pointer':'/texts/0/text','text':'前面的标题','kind':'paragraph','plain':False}, {'pointer':'/texts/1/text','text':actual,'kind':'paragraph','plain':field_plain}]
        if duplicate: fields.append({'pointer':'/texts/2/text','text':actual,'kind':'paragraph','plain':field_plain})
        fields.append({'pointer':'/texts/9/text','text':'后面的标题','kind':'paragraph','plain':False})
        return align(anchors,fields)

    def test_complete_four_line_paragraph_is_bound(self):
        accepted,diagnostics=self.alignment(['第一句。','第二句。','第三句。','第四句。'],'第一句。 第二句。 第三句。 第四句。')
        self.assertIn(((1,2,3,4),(1,),'T13_EXACT_COMPLETE_MARKDOWN_BLOCK'),accepted)
        self.assertEqual(diagnostics,[])

    def test_list_item_continuation_is_bound_without_plain_conversion(self):
        accepted,diagnostics=self.alignment(['命中的片段有头没尾。','但只适用于当前版本。'],'命中的片段有头没尾。 但只适用于当前版本。',field_plain=False)
        self.assertIn(((1,2),(1,),'T13_EXACT_COMPLETE_MARKDOWN_BLOCK'),accepted)
        self.assertEqual(diagnostics,[])

    def test_changed_condition_is_not_bound(self):
        accepted,diagnostics=self.alignment(['第一句。','仅当前版本。'],'第一句。 所有版本。',field_plain=False)
        self.assertFalse(any(1 in target for _,target,_ in accepted))
        self.assertTrue(diagnostics)

    def test_protected_inline_content_is_not_joined(self):
        accepted,diagnostics=self.alignment(['第一句。','代码。'],'第一句。 代码。',field_plain=False,protected=True)
        self.assertFalse(any(1 in target for _,target,_ in accepted))

    def test_duplicate_target_fields_are_not_arbitrarily_bound(self):
        accepted,diagnostics=self.alignment(['第一句。','第二句。'],'第一句。 第二句。',field_plain=False,duplicate=True)
        self.assertFalse(any(1 in target or 2 in target for _,target,_ in accepted))


if __name__=='__main__':
    unittest.main()
