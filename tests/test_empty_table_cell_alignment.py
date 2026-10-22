from __future__ import annotations
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from raghub.parsing import formats


class EmptyTableCellAlignmentTests(unittest.TestCase):
    def bind(self, values):
        cells=[SimpleNamespace(text=value, start_row_offset_idx=i//2, start_col_offset_idx=i%2) for i,value in enumerate(values)]
        table=SimpleNamespace(self_ref='#/tables/0',data=SimpleNamespace(table_cells=cells))
        document=SimpleNamespace(iterate_items=lambda:iter([(table,0)]))
        parsed=SimpleNamespace(document=document,details={},locations={},quality=[])
        parsed.issue=lambda severity,code,**details:parsed.quality.append({'severity':severity,'code':code,**details})
        anchors=[]
        for i,value in enumerate(values):
            if value in {'条件','2021年标准场','17日'}:
                anchors.append({'id':str(i),'text':value,'kind':'cell','plain':False,
                    'locator':{'kind':'md','file_sha256':'a'*64,'line_start':i+1,'line_end':i+1,'granularity':'REGION'}})
        with patch.object(formats,'raw_anchors',return_value=anchors):
            result=formats.bind_native(parsed,Path('table.md'),'md','a'*64)
        return result,table,cells

    def test_empty_cell_keeps_grid_and_nonempty_conditions_bound(self):
        result,table,cells=self.bind(['','条件','2021年标准场','17日'])
        self.assertEqual(result.quality,[])
        self.assertIs(table.data.table_cells,cells)
        self.assertEqual([c.text for c in cells],['','条件','2021年标准场','17日'])
        self.assertNotIn('/tables/0/data/table_cells/0/text',result.locations)
        for index in (1,2,3):
            pointer=f'/tables/0/data/table_cells/{index}/text'
            self.assertIn(pointer,result.locations)
            self.assertIn(pointer,result.details['protected_fields'])

    def test_unmatched_nonempty_cell_still_requires_review(self):
        result,_,_=self.bind(['','条件','未绑定的新条件','17日'])
        self.assertTrue(any(q['code']=='COARSE_SOURCE_MAPPING' and q['field']=='/tables/0/data/table_cells/2/text' for q in result.quality))

    def test_whitespace_cell_is_not_exempted_as_exact_empty(self):
        result,_,_=self.bind([' ','条件','2021年标准场','17日'])
        self.assertTrue(any(q['code']=='COARSE_SOURCE_MAPPING' and q['field']=='/tables/0/data/table_cells/0/text' for q in result.quality))

    def test_bundle_field_contract_retains_empty_cells_and_coordinates(self):
        native={'texts':[],'tables':[{'data':{'num_rows':2,'num_cols':2,'table_cells':[
            {'text':'','start_row_offset_idx':0,'start_col_offset_idx':0},
            {'text':'条件','start_row_offset_idx':0,'start_col_offset_idx':1},
            {'text':'2021年标准场','start_row_offset_idx':1,'start_col_offset_idx':0},
            {'text':'17日','start_row_offset_idx':1,'start_col_offset_idx':1}]}}]}
        before=repr(native)
        values=list(formats.field_values(native))
        self.assertEqual(len(values),4)
        self.assertEqual(values[0],('/tables/0/data/table_cells/0/text','','cell'))
        self.assertEqual(repr(native),before)


if __name__=='__main__':
    unittest.main()
