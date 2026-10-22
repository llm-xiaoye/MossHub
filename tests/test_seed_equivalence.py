"""Behavior regressions for complete-context seed equivalence and provenance."""
import copy
import unittest
from types import SimpleNamespace

from raghub.retrieval.artifacts import parsed_content_equivalence
from raghub.retrieval.ranking import select_seeds


def fixture(index, *, ending="\n", condition="仅标准场适用", scale="单位：分钟", origin=None):
    body = ending.join(("适用条件：" + condition, "标准场开始时间为上午9:00。"))
    title = "时间安排"
    supplement = SimpleNamespace(
        adapter="markdown", config_sha256="config", model_lock_sha256="model",
        scope={"selected": "DOCUMENT_BODY"}, quality=[], images=[],
        format_details={"format": "md", "source_observation": {
            "filename": str(index) + ".md", "raw_version_id": "raw" + str(index),
            "relative_path": str(index) + ".md", "source_root_id": "root",
            "acquisition_attempt_manifest": "archive" + str(index), "unchanged": False,
            "directory_context": None,
            "provenance": {"origin_local_path": origin or "/copies/" + str(index) + ".md",
                           "source_revision": "V1", "source_date": "2026-10-12"}}},
        fields=[SimpleNamespace(field_pointer="/texts/0/text", original_text=title, cleaned_text=title),
                SimpleNamespace(field_pointer="/texts/1/text", original_text=body, cleaned_text=body),
                SimpleNamespace(field_pointer="/texts/2/text", original_text=scale, cleaned_text=scale)])
    parent = {"parent_id": "p" + str(index), "text": title + ending + scale + ending + body,
              "body": body, "content_kind": "text", "heading_scope": ["/texts/0"]}
    item = {"chunk_id": "c" + str(index), "parent_id": parent["parent_id"],
            "text": body, "equivalence_key": "legacy" + str(index),
            "processing_version_id": "pv" + str(index), "raw_version_id": "raw" + str(index),
            "document_id": "doc" + str(index), "content_kind": "text",
            "rerank_rank": index + 1, "fusion_rank": index + 1, "reranker_raw_logit": 10 - index}
    return supplement, parent, item


def choose(fixtures):
    parents, contexts, ranked = {}, {}, []
    for supplement, parent, item in fixtures:
        parents[parent["parent_id"]] = parent
        contexts[parent["parent_id"]] = parsed_content_equivalence(supplement, [parent])
        ranked.append(item)
    return select_seeds(SimpleNamespace(parents=parents, equivalence_contexts=contexts), ranked)


class CompleteSeedEquivalenceChecks(unittest.TestCase):
    def test_different_copy_paths_share_one_seed_and_keep_each_source(self):
        first, second, third = fixture(0), fixture(1), fixture(2)
        seeds, groups, decisions = choose([first, second, third])
        self.assertEqual([item["chunk_id"] for item in seeds], ["c0"])
        self.assertEqual(len(groups), 1)
        aliases = [item for item in decisions if item["decision"] == "PROVEN_EQUIVALENT_SKIPPED"]
        self.assertEqual({item["source"]["document_id"] for item in aliases}, {"doc1", "doc2"})
        self.assertTrue(all(item["equivalent_to"] == "c0" for item in aliases))
        self.assertTrue(all(item["sent_as_support_unit"] is False for item in aliases))

    def test_crlf_comparison_does_not_mutate_original_or_parent_coordinates(self):
        first, second = fixture(0), fixture(1, ending="\r\n")
        before = copy.deepcopy(second)
        seeds, _, _ = choose([first, second])
        self.assertEqual(len(seeds), 1)
        self.assertEqual(second[0].fields[1].original_text, before[0].fields[1].original_text)
        self.assertEqual(second[1], before[1])
        self.assertIn("\r\n", second[1]["text"])

    def test_condition_difference_outside_identical_child_keeps_two_seeds(self):
        first, second = fixture(0), fixture(1, condition="仅受限场适用")
        second[2]["text"] = first[2]["text"]
        seeds, _, _ = choose([first, second])
        self.assertEqual(len(seeds), 2)

    def test_scale_and_footnote_are_part_of_complete_context(self):
        for scale in ("单位：小时", "单位：分钟；仅工作日适用"):
            with self.subTest(scale=scale):
                self.assertEqual(len(choose([fixture(0), fixture(1, scale=scale)])[0]), 2)

    def test_revision_date_and_unknown_provenance_stay_independent(self):
        for key, value in (("source_revision", "V2"), ("source_date", "2026-10-13"),
                           ("permission_scope", "restricted"), ("unknown_condition", "only nights")):
            with self.subTest(key=key):
                first, second = fixture(0), fixture(1)
                second[0].format_details["source_observation"]["provenance"][key] = value
                self.assertEqual(len(choose([first, second])[0]), 2)

    def test_directory_context_is_not_copy_identity(self):
        first, second = fixture(0), fixture(1)
        second[0].format_details["source_observation"]["directory_context"] = {"region": "CN"}
        self.assertEqual(len(choose([first, second])[0]), 2)

    def test_config_quality_images_and_scope_are_not_discarded(self):
        changes = (("adapter", "pdf"), ("config_sha256", "different"),
                   ("model_lock_sha256", "different"), ("quality", [{"warning": "unreviewed"}]),
                   ("images", [{"sha256": "different-image"}]), ("scope", {"selected": "OTHER"}))
        for key, value in changes:
            with self.subTest(key=key):
                first, second = fixture(0), fixture(1)
                setattr(second[0], key, value)
                self.assertEqual(len(choose([first, second])[0]), 2)

    def test_bare_cr_and_significant_spaces_are_not_normalized(self):
        for body in ("适用条件：仅标准场适用\r标准场开始时间为上午9:00。",
                     "适用条件：仅 标准场适用\n标准场开始时间为上午9:00。"):
            with self.subTest(body=body):
                first, second = fixture(0), fixture(1)
                second[0].fields[1].original_text = body
                second[0].fields[1].cleaned_text = body
                self.assertEqual(len(choose([first, second])[0]), 2)

    def test_heading_difference_keeps_identical_body_independent(self):
        first, second = fixture(0), fixture(1)
        second[0].fields[0].original_text = "另一版本时间安排"
        second[0].fields[0].cleaned_text = "另一版本时间安排"
        self.assertEqual(len(choose([first, second])[0]), 2)

    def test_unknown_complete_context_does_not_merge_cross_version(self):
        first, second = fixture(0), fixture(1)
        artifacts = SimpleNamespace(parents={item[1]["parent_id"]: item[1] for item in (first, second)},
                                    equivalence_contexts={})
        seeds, _, _ = select_seeds(artifacts, [first[2], second[2]])
        self.assertEqual(len(seeds), 2)

    def test_hash_collision_does_not_replace_complete_context_comparison(self):
        first, second = fixture(0), fixture(1)
        artifacts = SimpleNamespace(parents={item[1]["parent_id"]: item[1] for item in (first, second)},
                                    equivalence_contexts={
                                        "p0": {"sha256": "same", "serialized": "condition A"},
                                        "p1": {"sha256": "same", "serialized": "condition B"}})
        self.assertEqual(len(select_seeds(artifacts, [first[2], second[2]])[0]), 2)


if __name__ == "__main__":
    unittest.main()
