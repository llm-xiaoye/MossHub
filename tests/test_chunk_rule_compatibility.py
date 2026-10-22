"""Compatibility at the actual chunk artifact consumer, with strict version dispatch."""
from copy import deepcopy
import unittest

from raghub.indexing.storage import verify_chunk_rules


def historical_rules(version):
    return {"schema_version": version, "hierarchical_merge_list_items": False,
            "child_tokens": 512, "overlap_tokens": 64, "parent_tokens": 1536,
            "indivisible_tokens": 1024, "table_view": "SOURCE_COLUMN_PATH_EQUALS_VALUE_V1",
            "text_utf8_max": 65535, "source_sha256": "0" * 64, "docling_core_version": "fixture"}


class ChunkRuleCompatibilityTests(unittest.TestCase):
    def test_historical_versions_are_read_without_new_fields_or_mutation(self):
        for version in (1, 2):
            with self.subTest(version=version):
                rules = historical_rules(version)
                before = deepcopy(rules)
                self.assertEqual(verify_chunk_rules(rules), version)
                self.assertEqual(rules, before)
                self.assertNotIn("list_children", rules)

    def test_current_builder_rules_pass_actual_consumer(self):
        from raghub.indexing.chunks import RULES
        rules = {**RULES, "source_sha256": "0" * 64, "docling_core_version": "fixture"}
        self.assertEqual(verify_chunk_rules(rules), 5)

    def test_previous_three_and_four_remain_readable_without_html_policy(self):
        for version in (3, 4):
            rules = {**historical_rules(version),
                     "list_children": "SOURCE_LIST_ITEM_BOUNDARY_PROSE_GROUP_V1"}
            if version == 4:
                rules["table_intro_context"] = "MD_ADJACENT_EXPLICIT_UNIT_INTRO_V2"
            before = deepcopy(rules)
            self.assertEqual(verify_chunk_rules(rules), version)
            self.assertEqual(rules, before)
            rules["html_inline_blocks"] = "COMPLETE_DOM_BLOCK_BEFORE_INLINE_CODE_SPLIT_V1"
            with self.assertRaisesRegex(ValueError, "non-exact"):
                verify_chunk_rules(rules)

    def test_current_html_policy_is_required_exactly(self):
        from raghub.indexing.chunks import RULES
        good = {**RULES, "source_sha256": "0" * 64, "docling_core_version": "fixture"}
        missing = deepcopy(good)
        del missing["html_inline_blocks"]
        for invalid in (missing, {**good, "html_inline_blocks": "MERGE_SAME_XPATH"}):
            with self.assertRaises(ValueError):
                verify_chunk_rules(invalid)

    def test_historical_version_cannot_claim_new_semantics(self):
        for version in (1, 2):
            with self.subTest(version=version):
                rules = historical_rules(version)
                rules["list_children"] = "SOURCE_LIST_ITEM_BOUNDARY_PROSE_GROUP_V1"
                with self.assertRaisesRegex(ValueError, "non-exact"):
                    verify_chunk_rules(rules)

    def test_missing_unknown_or_misdeclared_current_policy_is_rejected(self):
        good = {**historical_rules(3), "list_children": "SOURCE_LIST_ITEM_BOUNDARY_PROSE_GROUP_V1"}
        invalid = [historical_rules(3), {**good, "list_children": "MERGE_SIMILAR_ITEMS"},
                   {**good, "unknown": True}]
        for rules in invalid:
            with self.subTest(rules=rules):
                with self.assertRaises(ValueError):
                    verify_chunk_rules(rules)

    def test_unknown_noninteger_and_missing_versions_are_rejected(self):
        for version in (0, 6, True, 3.0, "3", None):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    verify_chunk_rules(historical_rules(version))
        with self.assertRaises(ValueError):
            verify_chunk_rules({})
        with self.assertRaises(ValueError):
            verify_chunk_rules([])


if __name__ == "__main__":
    unittest.main()
