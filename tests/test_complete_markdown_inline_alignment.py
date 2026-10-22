from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from raghub.parsing import formats
from raghub.parsing.alignment import align


def parsed(items):
    value=SimpleNamespace(document=SimpleNamespace(iterate_items=lambda:iter(items)),details={},locations={},quality=[])
    value.issue=lambda severity,code,**details:value.quality.append({'severity':severity,'code':code,**details})
    return value


def text_item(index,text,label='list_item'):
    return SimpleNamespace(self_ref='#/texts/'+str(index),text=text,label=SimpleNamespace(value=label),children=[])


# The installed Markdown backend emits H1 as title; section_header requires
# an explicit native level. Keep these inline-only fixtures faithful to H1.
class CompleteMarkdownInlineTests(unittest.TestCase):
    def bind(self, markdown, actual):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'note.md'
            path.write_text('# 前标\n\n'+markdown+'\n\n# 后标\n',encoding='utf-8')
            anchors=formats.raw_anchors(path,'md','a'*64)
            value=parsed([(text_item(0,'前标','title'),0),(text_item(1,actual),0),(text_item(2,'后标','title'),0)])
            formats.bind_native(value,path,'md','a'*64)
            return anchors,value

    def test_joined_bold_and_code_keep_original_region_and_protection(self):
        text='症状： /skill-name 指令找不到，但 .md 文件存在。'
        anchors,value=self.bind('**症状：** `/skill-name` 指令找不到，但 `.md` 文件存在。',text)
        body=[item for item in anchors if item['locator'] and item['locator']['line_start']==3]
        self.assertTrue(any(item['kind']=='code' for item in body))
        self.assertEqual({item['rendered_block_text'] for item in body},{text})
        self.assertIn('/texts/1/text',value.locations)
        self.assertIn('/texts/1/text',value.details['protected_fields'])
        self.assertEqual(value.quality,[])

    def test_softbreak_preserves_full_condition(self):
        _,value=self.bind('命中的片段有头没尾。\n但只适用于当前版本。','命中的片段有头没尾。 但只适用于当前版本。')
        self.assertEqual(value.locations['/texts/1/text']['line_end'],4)
        self.assertEqual(value.quality,[])

    def test_changed_condition_is_not_bound(self):
        _,value=self.bind('仅适用于**当前版本**。','仅适用于所有版本。')
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)

    def test_spaces_inside_inline_code_are_not_collapsed(self):
        anchors,value=self.bind('前 `a  b` 后。','前 a b 后。')
        self.assertIn('a  b',[item['text'] for item in anchors if item['kind']=='code'])
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)

    def test_split_code_fields_keep_native_code_kind(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'note.md'
            path.write_text('# 前标\n\n症状： `/skill-name` 找不到。\n\n# 后标\n',encoding='utf-8')
            value=parsed([(text_item(0,'前标','title'),0),(text_item(1,'症状：'),0),(text_item(2,'/skill-name','code'),0),(text_item(3,'找不到。'),0),(text_item(4,'后标','title'),0)])
            formats.bind_native(value,path,'md','a'*64)
            self.assertEqual(value.quality,[])
            self.assertIn('/texts/2/text',value.details['protected_fields'])

    def test_repeated_protected_atoms_bind_only_in_unique_neighbour_windows(self):
        texts=['前标','/skill-name','中标','/skill-name','后标']
        kinds=['paragraph','code','paragraph','code','paragraph']
        anchors=[{'id':str(i),'text':text,'kind':kinds[i],'plain':False} for i,text in enumerate(texts)]
        fields=[{'pointer':'/'+str(i),'text':text,'kind':kinds[i],'plain':False} for i,text in enumerate(texts)]
        accepted,diagnostics=align(anchors,fields)
        self.assertEqual(diagnostics,[])
        self.assertEqual({j for _,target,_ in accepted for j in target},set(range(5)))

    def test_repeated_protected_atoms_in_one_window_remain_unbound(self):
        texts=['前标','/same','/same','后标']
        kinds=['paragraph','code','code','paragraph']
        anchors=[{'id':str(i),'text':text,'kind':kinds[i],'plain':False} for i,text in enumerate(texts)]
        fields=[{'pointer':'/'+str(i),'text':text,'kind':kinds[i],'plain':False} for i,text in enumerate(texts)]
        accepted,diagnostics=align(anchors,fields)
        self.assertFalse(any(1 in target or 2 in target for _,target,_ in accepted))
        self.assertEqual({item['field'] for item in diagnostics},{'/1','/2'})

    def table(self, changed=False):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'note.md'
            path.write_text('# 前标\n\n|名称|值|\n|---|---|\n|甲|1|\n|乙|1|\n\n# 后标\n',encoding='utf-8')
            texts=['名称','值','甲','2' if changed else '1','乙','1']
            table=SimpleNamespace(self_ref='#/tables/0',data=SimpleNamespace(table_cells=[SimpleNamespace(text=text) for text in texts]))
            value=parsed([(text_item(0,'前标','title'),0),(table,0),(text_item(1,'后标','title'),0)])
            formats.bind_native(value,path,'md','a'*64)
            return value

    def test_table_cells_bind_original_rows_including_repeated_values(self):
        value=self.table()
        self.assertEqual(value.quality,[])
        self.assertEqual(value.locations['/tables/0/data/table_cells/3/text']['line_start'],5)
        self.assertEqual(value.locations['/tables/0/data/table_cells/5/text']['line_start'],6)
        self.assertIn('/tables/0/data/table_cells/3/text',value.details['protected_fields'])

    def test_changed_table_value_is_not_bound(self):
        value=self.table(changed=True)
        self.assertNotIn('/tables/0/data/table_cells/3/text',value.locations)
        self.assertTrue(value.quality)

    def test_field_enumeration_keeps_container_and_leaf_for_v1_bundle_contract(self):
        native={'texts':[{'text':'','label':'list_item','children':[{'cref':'#/texts/1'}]},{'text':'正文','label':'list_item','children':[]},{'text':'','label':'list_item','children':[]}],'tables':[]}
        self.assertEqual([pointer for pointer,_,_ in formats.field_values(native)],['/texts/0/text','/texts/1/text','/texts/2/text'])
        self.assertEqual(native['texts'][0]['children'],[{'cref':'#/texts/1'}])


    def test_currency_delimiters_numbers_and_units_are_preserved(self):
        text='The $300 DRA fee can be paid in $100 annual installments over three years.'
        anchors,value=self.bind(text,text)
        self.assertEqual(value.quality,[])
        self.assertIn('/texts/1/text',value.locations)
        self.assertIn('/texts/1/text',value.details['protected_fields'])
        rendered=[a['rendered_block_text'] for a in anchors if a.get('rendered_block_text')]
        self.assertTrue(rendered)
        self.assertTrue(all(item==text and item.count('$')==2 for item in rendered))

    def test_changed_currency_amount_or_unit_remains_unbound(self):
        original='The $300 DRA fee can be paid in $100 annual installments.'
        for actual in (original.replace('$300','$200'),original.replace('annual','monthly')):
            with self.subTest(actual=actual):
                _,value=self.bind(original,actual)
                self.assertNotIn('/texts/1/text',value.locations)
                self.assertTrue(value.quality)

    def test_dropped_currency_delimiters_remain_unbound(self):
        text='The $300 DRA fee can be paid in $100 annual installments.'
        _,value=self.bind(text,text.replace('$',''))
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)

    def test_inline_formula_delimiters_are_preserved_and_protected(self):
        text='仅当 $x + 1$ 大于零时适用。'
        _,value=self.bind(text,text)
        self.assertEqual(value.quality,[])
        self.assertIn('/texts/1/text',value.details['protected_fields'])

    def test_plain_complete_block_accepts_only_whitespace_normalization(self):
        original='时间：2021-12-29 14:58:03\t\t\r \t\u00a0来源：海外网\t\t仅适用于当前版本。'
        actual='时间：2021-12-29 14:58:03 来源：海外网\t\t仅适用于当前版本。'
        _,value=self.bind(original,actual)
        self.assertEqual(value.quality,[])
        self.assertIn('/texts/1/text',value.locations)

    def test_plain_normalization_does_not_accept_changed_condition(self):
        _,value=self.bind('适用于\t**当前版本**。','适用于 所有版本。')
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)

    def test_duplicate_complete_source_blocks_remain_ambiguous(self):
        text='只适用于**当前版本**。'
        _,value=self.bind(text+'\n\n'+text,'只适用于当前版本。')
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)


    def test_plain_normalization_keeps_different_dates_unbound(self):
        source='仅适用于当前版本。\n开始日期：2021年1月17日。'
        _,value=self.bind(source,'仅适用于当前版本。 开始日期：2022年1月17日。')
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)

    def test_normalized_duplicate_blocks_remain_ambiguous(self):
        source='条件：\t仅限当前版本。\n结束。\n\n条件： 仅限当前版本。\n结束。'
        _,value=self.bind(source,'条件： 仅限当前版本。 结束。')
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)

    def test_original_block_budget_is_checked_before_normalization(self):
        _,value=self.bind('条件：'+'\t'*4100+'\n仅限当前版本。','条件： 仅限当前版本。')
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)

    def test_native_block_budget_is_checked_before_normalization(self):
        _,value=self.bind('条件：\n仅限当前版本。','条件：'+'\t'*4100+'仅限当前版本。')
        self.assertNotIn('/texts/1/text',value.locations)
        self.assertTrue(value.quality)


if __name__=='__main__':
    unittest.main()
