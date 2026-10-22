from __future__ import annotations
import unittest
from raghub.parsing.alignment import align


class MarkdownSoftbreakAlignmentTests(unittest.TestCase):
    def observe(self, second_location=None, actual='第一句。 第二句。'):
        loc={'kind':'md','file_sha256':'a'*64,'line_start':8,'line_end':9,
             'source_start':100,'source_end':120,'granularity':'REGION',
             'basis':'ORIGINAL_MARKDOWN_INLINE_NODE_IN_BLOCK'}
        anchors=[{'id':'before','text':'前面的标题','kind':'paragraph','plain':False},
                 {'id':'block-0','text':'第一句。','kind':'paragraph','plain':True,'locator':loc},
                 {'id':'block-1','text':'第二句。','kind':'paragraph','plain':True,'locator':second_location or dict(loc)},
                 {'id':'after','text':'后面的标题','kind':'paragraph','plain':False}]
        fields=[{'pointer':'/texts/0/text','text':'前面的标题','kind':'paragraph','plain':False},
                {'pointer':'/texts/1/text','text':actual,'kind':'paragraph','plain':True},
                {'pointer':'/texts/2/text','text':'后面的标题','kind':'paragraph','plain':False}]
        return align(anchors,fields)

    def test_exact_softbreak_in_one_original_block_is_bound(self):
        accepted,diagnostics=self.observe()
        self.assertIn(((1,2),(1,),'T14_EXACT_NORMALIZED_CONCAT'),accepted)
        self.assertEqual(diagnostics,[])

    def test_other_block_does_not_gain_softbreak_equivalence(self):
        accepted,diagnostics=self.observe({'kind':'md','line_start':12,'line_end':12})
        self.assertFalse(any(1 in target for _,target,_ in accepted))
        self.assertEqual([d['field'] for d in diagnostics],['/texts/1/text'])

    def test_changed_condition_remains_unbound(self):
        accepted,diagnostics=self.observe(actual='第一句。 仅旧版本第二句。')
        self.assertFalse(any(1 in target for _,target,_ in accepted))
        self.assertEqual([d['field'] for d in diagnostics],['/texts/1/text'])


if __name__=='__main__':
    unittest.main()

