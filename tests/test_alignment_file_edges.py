from __future__ import annotations

import unittest
from raghub.parsing.alignment import align


class FileEdgeAlignmentTests(unittest.TestCase):
    def paragraph(self, block, line=3):
        parts=['以下为受控测试构造，不表示真实春运史实。','适用版本：V1。','测试线路标准模式的开始日期为1月17日。']
        location={'kind':'md','file_sha256':'a'*64,'line_start':line,'line_end':line+2,
                  'source_start':line*20,'source_end':line*20+51}
        anchors=[{'id':block+'-'+str(i),'text':part,'kind':'paragraph','plain':True,
                  'locator':dict(location),'rendered_block_text':' '.join(parts)} for i,part in enumerate(parts)]
        field={'pointer':'/texts/1/text','text':' '.join(parts),'kind':'paragraph','plain':True}
        return anchors,field

    def heading(self):
        return {'id':'heading','text':'控制资料标题','kind':'paragraph','plain':False}, {
            'pointer':'/texts/0/text','text':'控制资料标题','kind':'paragraph','plain':False}

    def test_last_complete_paragraph_keeps_all_conditions(self):
        body,field=self.paragraph('body'); heading,title=self.heading()
        accepted,diagnostics=align([heading]+body,[title,field])
        self.assertEqual([(aa,bb) for aa,bb,_ in accepted],[((0,),(0,)),((1,2,3),(1,))])
        self.assertEqual(diagnostics,[])

    def test_first_complete_paragraph_is_also_located(self):
        body,field=self.paragraph('body'); heading,title=self.heading()
        accepted,diagnostics=align(body+[heading],[field,title])
        self.assertEqual({(aa,bb) for aa,bb,_ in accepted},{((3,),(1,)),((0,1,2),(0,))})
        self.assertEqual(diagnostics,[])

    def test_two_source_blocks_do_not_choose_an_arbitrary_location(self):
        first,field=self.paragraph('first'); second,_=self.paragraph('second',line=7)
        heading,title=self.heading()
        accepted,diagnostics=align([heading]+first+second,[title,field])
        self.assertFalse(any(1 in bb for _,bb,_ in accepted))
        self.assertEqual([row['field'] for row in diagnostics],['/texts/1/text'])

    def test_different_applicable_condition_is_not_equivalent(self):
        body,field=self.paragraph('body');heading,title=self.heading()
        field['text']=field['text'].replace('标准模式','受限模式')
        accepted,diagnostics=align([heading]+body,[title,field])
        self.assertFalse(any(1 in bb for _,bb,_ in accepted))
        self.assertEqual(len(diagnostics),1)

    def test_without_real_seed_whole_file_stays_unbound(self):
        body,field=self.paragraph('body')
        accepted,diagnostics=align(body,[field])
        self.assertEqual(accepted,[])
        self.assertEqual(len(diagnostics),1)

    def test_oversize_edge_window_stays_unbound(self):
        heading,title=self.heading()
        location={'kind':'md','file_sha256':'a'*64,'line_start':3,'line_end':131}
        parts=[str(i)+'。' for i in range(129)]
        body=[{'id':str(i),'text':part,'kind':'paragraph','plain':True,'locator':location,
               'rendered_block_text':' '.join(parts)} for i,part in enumerate(parts)]
        field={'pointer':'/texts/1/text','text':' '.join(parts),'kind':'paragraph','plain':True}
        accepted,diagnostics=align([heading]+body,[title,field])
        self.assertFalse(any(1 in bb for _,bb,_ in accepted))
        self.assertEqual(len(diagnostics),1)


if __name__=='__main__':
    unittest.main()

