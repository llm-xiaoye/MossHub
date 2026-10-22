import copy
import unittest
from raghub.retrieval.ranking import rerank_candidates,rerank_batches,order_reranked
from raghub.retrieval.evidence import body_ranges
from test_table_leading_context import make_table
from raghub.indexing.chunks import table_units

class RecallRouteCoverageTest(unittest.TestCase):
    def candidates(self,route_count=2):
        result=[]
        for r in range(route_count):
            for rank in range(1,41):
                number=r*40+rank
                result.append({'chunk_id':str(number),'fusion_rank':number,'token_count':rank,
                    'route_hits':[{'query_id':'Q'+str(r//2+1),'route':['bm25','dense'][r%2],'route_rank':rank}]})
        return result

    def test_high_bm25_scale_cannot_remove_all_dense_candidates(self):
        candidates=self.candidates();selected=rerank_candidates(candidates)
        ids={x['chunk_id'] for x in selected}
        self.assertEqual(len(selected),40)
        self.assertTrue({str(n) for n in range(41,51)}<=ids)
        self.assertTrue({str(n) for n in range(1,11)}<=ids)
        self.assertEqual([x['fusion_rank'] for x in selected],sorted(x['fusion_rank'] for x in selected))
        batches=rerank_batches(candidates)
        self.assertEqual({x['chunk_id'] for b in batches for x in b},ids)
        replies=[{'items':[{'item_id':x['chunk_id'],'score':float(x['chunk_id'])} for x in b]} for b in batches]
        self.assertEqual({x['chunk_id'] for x in order_reranked(candidates,replies)},ids)

    def test_two_queries_protect_each_of_four_recall_routes(self):
        selected=rerank_candidates(self.candidates(4));ids={x['chunk_id'] for x in selected}
        self.assertEqual(len(ids),40)
        for offset in (0,40,80,120):
            self.assertTrue({str(offset+n) for n in range(1,6)}<=ids)

    def test_overlap_and_small_pools_keep_every_available_unique_candidate(self):
        candidates=self.candidates()[:6]
        for x in candidates:x['route_hits'].append({'query_id':'Q1','route':'dense','route_rank':int(x['chunk_id'])})
        self.assertEqual(rerank_candidates(candidates),candidates)

    def test_header_only_evidence_keeps_context_and_rejects_partial_or_false_headers(self):
        table,fields=make_table([['Scope','2026'],['Name','Value']],[[False,False],[True,True]])
        units,_,_=table_units(table,fields);stream=units[0]
        parent={'text':stream.text,'body':stream.text,'content_kind':'table'};source={'parts':stream.pieces}
        self.assertEqual(body_ranges(parent,source,0,0,len(stream.text)),[(0,len(stream.text),'text_span')])
        with self.assertRaisesRegex(ValueError,'complete source context'):body_ranges(parent,source,0,1,len(stream.text))
        damaged=copy.deepcopy(source)
        next(p for p in damaged['parts'] if not p['constructed'])['purpose']='BODY'
        with self.assertRaisesRegex(ValueError,'complete-row boundaries'):body_ranges(parent,damaged,0,0,len(stream.text))

if __name__=='__main__':unittest.main()
