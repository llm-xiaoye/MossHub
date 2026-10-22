from __future__ import annotations
import copy
import unittest
from types import SimpleNamespace

from raghub.retrieval.evidence import child_window, proven_empty_table_row
from raghub.retrieval.ranking import select_seeds


class Locator:
    def model_dump(self, mode="json"):
        return {"source": "test-cell"}


class EmptyTableSeedTests(unittest.TestCase):
    def fixture(self):
        text="第2行：=（空白）"
        part={"constructed":False,"start":5,"end":5,"cleaned_start":0,"cleaned_end":0,
              "field_pointer":"/tables/0/data/table_cells/0/text","purpose":"BODY",
              "locator":{"source":"test-cell"}}
        source={"source_record_id":"s-empty","parts":[{"constructed":True,"start":0,"end":5},part,
                         {"constructed":True,"start":5,"end":len(text)}]}
        parent={"parent_id":"p-empty","content_kind":"table","body":text,"text":text,
                "heading_scope":[],"source_record_id":"s-empty"}
        child={"chunk_id":"empty","parent_id":"p-empty","text":text,"source_record_id":"s-empty"}
        item={"chunk_id":"empty","parent_id":"p-empty","text":text,"content_kind":"table",
              "source_record_id":"s-empty","document_id":"d","raw_version_id":"r",
              "processing_version_id":"v","fusion_rank":1,"rerank_rank":1,
              "reranker_raw_logit":5.0,"equivalence_key":"empty"}
        field=SimpleNamespace(cleaned_text="",locator=Locator(),mappings=[])
        artifacts=SimpleNamespace(parents={"p-empty":parent},children={"empty":child},
            sources={"s-empty":source},fields={("v",part["field_pointer"]):field},
            snapshot={"kb_id":"kb","generation_id":"g"})
        return artifacts,item,source,field

    def test_verified_empty_row_does_not_take_seed_and_next_fact_is_selected(self):
        artifacts,item,source,field=self.fixture()
        before=copy.deepcopy(source)
        artifacts.parents["p-fact"]={"parent_id":"p-fact","content_kind":"text","body":"真实正文",
                                      "text":"真实正文","heading_scope":[]}
        fact={**item,"chunk_id":"fact","parent_id":"p-fact","text":"真实正文",
              "content_kind":"text","fusion_rank":2,"rerank_rank":2,"equivalence_key":"fact"}
        seeds,groups,decisions=select_seeds(artifacts,[item,fact])
        self.assertEqual([s["chunk_id"] for s in seeds],["fact"])
        skipped=next(d for d in decisions if d["chunk_id"]=="empty")
        self.assertEqual(skipped["decision"],"PROVEN_EMPTY_TABLE_ROW_SKIPPED")
        self.assertFalse(skipped["sent_as_support_unit"])
        self.assertEqual(source,before)

    def test_any_real_cell_text_is_not_treated_as_an_empty_row(self):
        artifacts,item,source,field=self.fixture()
        source["parts"][1]["end"]=6
        source["parts"][1]["cleaned_end"]=1
        field.cleaned_text="5"
        self.assertIsNone(proven_empty_table_row(artifacts,item))

    def test_zero_interval_cannot_hide_nonempty_source_field(self):
        artifacts,item,source,field=self.fixture()
        field.cleaned_text="原文其实有数字"
        self.assertIsNone(proven_empty_table_row(artifacts,item))
        with self.assertRaisesRegex(ValueError,"no bound source body"):
            child_window(artifacts.parents["p-empty"],source,artifacts.children["empty"],source)

    def test_constructed_text_without_cell_binding_is_not_exempted(self):
        artifacts,item,source,field=self.fixture()
        source["parts"]=[{"constructed":True,"start":0,"end":len(item["text"])}]
        self.assertIsNone(proven_empty_table_row(artifacts,item))

    def test_wrong_source_location_fails_instead_of_silently_skipping(self):
        artifacts,item,source,field=self.fixture()
        source["parts"][1]["locator"]={"source":"wrong-cell"}
        with self.assertRaisesRegex(ValueError,"actual cleaned field/location"):
            proven_empty_table_row(artifacts,item)


if __name__=="__main__":
    unittest.main()
