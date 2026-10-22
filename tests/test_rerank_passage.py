import copy
import unittest
from raghub.retrieval.ranking import rerank_passage

class BoundRerankPassageTest(unittest.TestCase):
    def test_full_body_precedes_a_source_bound_heading_without_changing_evidence(self):
        text='标题：安装\npip install -U FlagEmbedding'
        parts=[{'start':0,'end':3,'constructed':True,'purpose':'HEADING'},
               {'start':3,'end':5,'constructed':False,'purpose':'HEADING'}]
        original=copy.deepcopy(parts)
        self.assertEqual(rerank_passage(text,parts),'pip install -U FlagEmbedding\n标题：安装')
        self.assertEqual(parts,original)
        self.assertEqual(text,'标题：安装\npip install -U FlagEmbedding')

    def test_unproven_or_body_source_never_authorizes_a_heading_move(self):
        text='标题：安装\npip install -U FlagEmbedding'
        self.assertEqual(rerank_passage(text,[]),text)
        self.assertEqual(rerank_passage(text,[{'start':3,'end':5,'constructed':False,'purpose':'BODY'}]),text)
        self.assertEqual(rerank_passage(text,[{'start':3,'end':8,'constructed':False,'purpose':'HEADING'}]),text)

    def test_nonheading_prose_and_crlf_are_unchanged(self):
        text='普通文字\n内容';self.assertEqual(rerank_passage(text,[]),text)
        text='标题：安装\r\npip install';self.assertEqual(rerank_passage(text,[]),text)

if __name__=='__main__':unittest.main()
