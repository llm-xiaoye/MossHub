"""Source-list boundaries preserve retrieval granularity and complete mapped context."""
from types import SimpleNamespace
import unittest

from raghub.indexing.chunks import (
    RULES, ChunkingUnsupported, Stream, field_stream, joined,
    ordinary_plans, retrieval, structural_plain_plans,
)
from raghub.model_profiles import token_count


class CharacterTokenizer:
    def encode(self, text, *, add_special_tokens=True, truncation=False):
        return list(text) + ([0, 1] if add_special_tokens else [])


class SourceListBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tokens = CharacterTokenizer()
        self.context = Stream.empty()
        self.context.construct("标题：共同主题")

    def stream(self, number, text):
        return field_stream(SimpleNamespace(field_pointer=f"/texts/{number}/text", cleaned_text=text))

    def plans(self, entries):
        return list(structural_plain_plans(self.tokens, entries, self.context))

    def assert_bounds(self, plans):
        for parent, children in plans:
            self.assertLessEqual(token_count(self.tokens, retrieval(self.context, parent).text), 1536)
            for child, limit in children:
                self.assertEqual(limit, 512)
                self.assertLessEqual(token_count(self.tokens, retrieval(self.context, child).text), 512)

    def test_four_short_list_items_share_parent_but_not_child(self):
        entries = [(self.stream(i, text), True) for i, text in enumerate(
            ["索引：父子块保留上下文", "融合：重排后调权须配置并验证相关性",
             "评估：监控标签质量", "改写：检查问题约束是否一致"])]
        plans = self.plans(entries)
        self.assertEqual(len(plans), 1)
        children = plans[0][1]
        self.assertEqual([child.text for child, _limit in children], [s.text for s, _flag in entries])
        for i, (child, _limit) in enumerate(children):
            bound = [p for p in child.pieces if not p["constructed"]]
            self.assertEqual([(p["field_pointer"], p["cleaned_start"], p["cleaned_end"]) for p in bound],
                             [(f"/texts/{i}/text", 0, len(entries[i][0].text))])
        self.assertEqual(plans[0][0].text, joined([s for s, _flag in entries]).text)
        self.assert_bounds(plans)

    def test_prose_groups_remain_continuous_on_each_side_of_list(self):
        entries = [(self.stream(0, "先说明对象。"), False),
                   (self.stream(1, "这一方案有条件。"), False),
                   (self.stream(2, "一项独立列表说明"), True),
                   (self.stream(3, "然后解释限制。"), False),
                   (self.stream(4, "因此要实际核验。"), False)]
        plans = self.plans(entries)
        self.assertEqual([c.text for _p, cs in plans for c, _limit in cs],
                         ["先说明对象。\n这一方案有条件。", "一项独立列表说明", "然后解释限制。\n因此要实际核验。"])
        self.assert_bounds(plans)

    def test_complete_list_item_moves_to_next_parent_when_remainder_is_small(self):
        first, second = self.stream(0, "a" * 1450), self.stream(1, "b" * 150)
        plans = self.plans([(first, True), (second, True)])
        self.assertEqual([p.text for p, _cs in plans], [first.text, second.text])
        self.assertEqual([c.text for c, _limit in plans[1][1]], [second.text])
        self.assert_bounds(plans)

    def test_oversized_list_item_keeps_exact_coverage_and_only_internal_overlap(self):
        first, second = self.stream(0, "a" * 3400), self.stream(1, "b" * 80)
        plans = self.plans([(first, True), (second, True)])
        self.assertEqual("".join(p.text for p, _cs in plans).replace("\n", ""), first.text + second.text)
        intervals, other = [], []
        for _parent, children in plans:
            for child, _limit in children:
                bound = [p for p in child.pieces if not p["constructed"]]
                self.assertEqual(len({p["field_pointer"] for p in bound}), 1)
                for p in bound:
                    (intervals if p["field_pointer"] == "/texts/0/text" else other).append(
                        (p["cleaned_start"], p["cleaned_end"]))
        covered = set()
        for start, end in intervals:
            covered.update(range(start, end))
        self.assertEqual(covered, set(range(len(first.text))))
        self.assertTrue(any(a2 < b1 for (_a1, b1), (a2, _b2) in zip(intervals, intervals[1:])))
        self.assertEqual(other, [(0, len(second.text))])
        self.assert_bounds(plans)

    def test_plain_prose_keeps_existing_windows_and_context_budget(self):
        entries = [(self.stream(0, "a" * 800), False), (self.stream(1, "b" * 800), False)]
        new = self.plans(entries)
        existing = list(ordinary_plans(self.tokens, joined([s for s, _flag in entries]), self.context))
        self.assertEqual(new, existing)
        self.assert_bounds(new)
        oversized_context = Stream.empty()
        oversized_context.construct("标题" * 300)
        with self.assertRaises(ChunkingUnsupported):
            list(structural_plain_plans(self.tokens, [(self.stream(2, "内容"), True)], oversized_context))

    def test_rule_version_records_changed_list_semantics(self):
        self.assertEqual(RULES["schema_version"], 5)
        self.assertEqual(RULES["html_inline_blocks"], "COMPLETE_DOM_BLOCK_BEFORE_INLINE_CODE_SPLIT_V1")
        self.assertEqual(RULES["list_children"], "SOURCE_LIST_ITEM_BOUNDARY_PROSE_GROUP_V1")


if __name__ == "__main__":
    unittest.main()
