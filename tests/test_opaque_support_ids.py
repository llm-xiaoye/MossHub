"""Reference IDs select exact current units; source ranges remain authoritative."""
import copy
import unittest
from raghub.config import Settings
from raghub.retrieval.evidence import EvidenceAssembler
from raghub.retrieval.support_scope import support_texts
from raghub.retrieval.tokenizer import EvidenceTokenizer
from test_evidence_assembler import artifacts_fixture

class OpaqueSupportIdsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.assembler=EvidenceAssembler(EvidenceTokenizer(Settings.load()))

    def test_request_scope_and_source_bindings_survive_opaque_numbering(self):
        a,seeds,groups=artifacts_fixture(table=True)
        x=self.assembler.assemble('first-request',a,seeds,groups)
        y=self.assembler.assemble('second-request',a,seeds,groups)
        wx,wy=x['support_unit_whitelist'],y['support_unit_whitelist']
        self.assertEqual(len(wx),2);self.assertFalse(set(wx)&set(wy))
        self.assertEqual(list(wx),['Rfirst-request-E001-U001','Rfirst-request-E001-U002'])
        tx=support_texts(x['groups'],wx,kb_id='kb',generation_id='generation')
        ty=support_texts(y['groups'],wy,kb_id='kb',generation_id='generation')
        self.assertEqual(list(tx.values()),list(ty.values()))
        self.assertTrue(all('单位：百万；仅标准场适用' in t for t in tx.values()))
        for u in wx.values():
            l,r=u['parent_text_interval']
            self.assertIn(a.parents['parent']['text'][l:r],tx[u['support_unit_id']])
        again=self.assembler.assemble('first-request',a,seeds,groups)
        self.assertEqual(x['groups'],again['groups'])
        self.assertEqual(wx,again['support_unit_whitelist'])

    def test_unknown_id_is_rejected_even_when_its_text_matches(self):
        a,seeds,groups=artifacts_fixture(table=True)
        value=self.assembler.assemble('request',a,seeds,groups)
        changed=copy.deepcopy(value['groups'])
        changed[0]['support_units'][0]['support_unit_id']='Rrequest-E001-U999'
        with self.assertRaises((ValueError,KeyError)):
            support_texts(changed,value['support_unit_whitelist'],kb_id='kb',generation_id='generation')

if __name__=='__main__':unittest.main()
