"""Section coverage regressions for procedure questions; no model or network."""
import copy
import unittest
from types import SimpleNamespace

from raghub.retrieval.ranking import query_section_match, select_seeds


def evidence():
    parents, fields, sources, ranked = {}, {}, {}, []
    fields[("version", "/texts/0/text")] = SimpleNamespace(cleaned_text="Function Calling")
    for index in range(13):
        title = "五步流程" if index == 10 else "其他主题" + str(index)
        pointer = "/texts/" + str(index + 1) + "/text"
        pid, cid, source_id = "parent" + str(index), "child" + str(index), "source" + str(index)
        body = "本节原文 " + str(index)
        parents[pid] = {"text": "标题：Function Calling / " + title + "\n" + body, "body": body,
                        "content_kind": "text", "source_record_id": source_id,
                        "heading_scope": ["#/texts/0", "#/texts/" + str(index + 1)]}
        fields[("version", pointer)] = SimpleNamespace(cleaned_text=title)
        sources[source_id] = {"parts": [
            {"constructed": True, "start": 0, "end": 3},
            {"constructed": False, "purpose": "HEADING", "field_pointer": "/texts/0/text",
             "cleaned_start": 0, "cleaned_end": len("Function Calling")},
            {"constructed": False, "purpose": "HEADING", "field_pointer": pointer,
             "cleaned_start": 0, "cleaned_end": len(title)},
        ]}
        ranked.append({"chunk_id": cid, "parent_id": pid, "document_id": "document",
                       "raw_version_id": "raw", "processing_version_id": "version",
                       "content_kind": "text", "text": body, "equivalence_key": cid,
                       "fusion_rank": index + 1, "rerank_rank": index + 1,
                       "reranker_raw_logit": 13 - index})
    return SimpleNamespace(parents=parents, fields=fields, sources=sources, equivalence_contexts={}), ranked

class QuerySectionCoverageTest(unittest.TestCase):
    def test_procedure_section_survives_low_rerank_without_raising_seed_limit(self):
        artifacts, ranked = evidence()
        original = copy.deepcopy(ranked)
        baseline = select_seeds(artifacts, ranked)[0]
        self.assertNotIn("child10", [row["chunk_id"] for row in baseline])
        seeds, groups, decisions = select_seeds(artifacts, ranked, "Function Calling 的完整流程分为哪几步")
        ids = [row["chunk_id"] for row in seeds]
        self.assertIn("child10", ids)
        self.assertEqual(len(seeds), 8)
        self.assertTrue({"child0", "child1", "child2", "child3"}.issubset(ids))
        self.assertEqual([row["rerank_rank"] for row in seeds], sorted(row["rerank_rank"] for row in seeds))
        match = next(row for row in decisions if row["chunk_id"] == "child10")
        self.assertEqual(match["selection_origin"], "QUERY_SECTION_HEADING")
        self.assertEqual(match["heading_match"]["matched_terms"], ["流程"])
        self.assertFalse(match["heading_match"]["semantic_relevance_proven"])
        self.assertEqual(ranked, original)

    def test_shared_document_title_does_not_promote_every_section(self):
        artifacts, ranked = evidence()
        artifacts.parents["parent10"]["heading_scope"] = ["#/texts/0"]
        artifacts.fields[("version", "/texts/0/text")] = SimpleNamespace(cleaned_text="Function Calling 五步流程")
        self.assertIsNone(query_section_match(artifacts, ranked[10], "Function Calling 的流程"))

    def test_another_processing_version_cannot_supply_heading_text(self):
        artifacts, ranked = evidence()
        ranked[10]["processing_version_id"] = "different-version"
        self.assertIsNone(query_section_match(artifacts, ranked[10], "流程分为哪几步"))

    def test_generic_model_heading_is_only_selected_when_explicitly_asked(self):
        artifacts, ranked = evidence()
        artifacts.fields[("version", "/texts/11/text")] = SimpleNamespace(cleaned_text="模型判断")
        artifacts.sources["source10"]["parts"][-1]["cleaned_end"] = len("模型判断")
        self.assertIsNone(query_section_match(artifacts, ranked[10], "模型流程是什么"))
        self.assertIsNotNone(query_section_match(artifacts, ranked[10], "请解释模型判断"))

    def test_unbound_display_heading_is_not_used(self):
        artifacts, ranked = evidence()
        for part in artifacts.sources["source10"]["parts"]:
            part["constructed"] = True
        self.assertIsNone(query_section_match(artifacts, ranked[10], "流程"))

    def test_no_match_keeps_previous_membership_and_source_scope(self):
        artifacts, ranked = evidence()
        old = [row["chunk_id"] for row in select_seeds(artifacts, ranked)[0]]
        new = [row["chunk_id"] for row in select_seeds(artifacts, ranked, "价格是多少")[0]]
        self.assertEqual(new, old)
        self.assertTrue(all(row["document_id"] == "document" for row in select_seeds(artifacts, ranked, "流程")[0]))

    def test_heading_focus_uses_at_most_two_remaining_slots(self):
        artifacts, ranked = evidence()
        for index in range(8, 13):
            artifacts.fields[("version", "/texts/" + str(index + 1) + "/text")] = SimpleNamespace(cleaned_text="流程说明")
            artifacts.sources["source" + str(index)]["parts"][-1]["cleaned_end"] = len("流程说明")
        seeds, _, decisions = select_seeds(artifacts, ranked, "流程")
        focused = [row for row in decisions if row.get("selection_origin") == "QUERY_SECTION_HEADING"]
        self.assertEqual(len(focused), 2)
        self.assertEqual(len(seeds), 8)
