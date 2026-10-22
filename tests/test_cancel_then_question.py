from __future__ import annotations
import unittest
from raghub.chat.query import knowledge_view,protected_conditions


class CancelThenQuestionTests(unittest.TestCase):
    def assert_owned(self,raw,expected):
        view=knowledge_view(raw)
        self.assertTrue(view["boundary_reliable"])
        self.assertEqual(view["control_intent"],"cancel")
        self.assertEqual(view["effective_original_query"],expected.replace("\r\n","\n").replace("\r","\n"))
        self.assertEqual(len(view["knowledge_intervals"]),1)
        self.assertEqual(len(view["control_removed_intervals"]),1)
        new=view["knowledge_intervals"][0]
        old=view["control_removed_intervals"][0]
        self.assertEqual(raw[new["start"]:new["end"]],expected)
        self.assertEqual(raw[old["start"]:old["end"]],old["text"])
        self.assertLessEqual(old["end"],new["start"])
        self.assertEqual(view["resolution_intent_required"],"cancel")
        return view

    def test_real_cancel_then_note_question_keeps_complete_new_question(self):
        new="现在按Agent与传统RAG笔记，说明怎样防止工具死循环。"
        self.assert_owned("取消刚才的问题。"+new,new)

    def test_finite_polite_prefixes_belong_to_cancel_interval(self):
        new="现在说明Agent的终止条件。"
        for prefix in ("","请","先","请先"):
            with self.subTest(prefix=prefix):
                raw="  "+prefix+"取消上一个问题。 \n"+new+"  "
                view=self.assert_owned(raw,new)
                self.assertEqual(view["control_removed_intervals"][0]["text"],prefix+"取消上一个问题。 \n")

    def test_new_question_leads_are_not_a_second_cancellation(self):
        for new in ("改问：怎样防工具死循环？","换题，只讨论Agent的最大迭代与终止条件。",
                    "换个问题，现在说明工具结果校验。","请改查Query改写的覆盖范围。"):
            with self.subTest(new=new):
                self.assert_owned("取消旧问题。"+new,new)

    def test_conflicting_second_control_does_not_partially_cancel(self):
        for new in ("请保留旧问题，再说明Agent。","现在撤销取消。说明Agent。",
                    "现在不要取消旧问题。说明Agent。","现在保留当前问题。说明Agent。",
                    "说明Agent，然后取消旧问题。"):
            with self.subTest(new=new):
                view=knowledge_view("取消刚才的问题。"+new)
                self.assertFalse(view["boundary_reliable"])
                self.assertIsNone(view["effective_original_query"])
                self.assertEqual(view["control_removed_intervals"],[])

    def test_multiple_or_modified_old_targets_are_not_partially_matched(self):
        for raw in ("取消刚才的问题和上一个问题。说明Agent。",
                    "取消刚才的问题里的限制。说明Agent。"):
            with self.subTest(raw=raw):
                view=knowledge_view(raw)
                self.assertFalse(view["boundary_reliable"])
                self.assertEqual(view["control_removed_intervals"],[])

    def test_negative_or_missing_separator_stays_unresolved(self):
        for raw in ("不要取消刚才的问题。现在说明Agent。",
                    "取消刚才的问题现在说明Agent。"):
            with self.subTest(raw=raw):
                view=knowledge_view(raw)
                self.assertFalse(view["boundary_reliable"])
                self.assertEqual(view["control_removed_intervals"],[])

    def test_unicode_newlines_and_new_conditions_keep_original_ranges(self):
        new="现在按v3.2解释Agent🧭的终止条件，\r\n最多3次，不能跳过结果校验。"
        view=self.assert_owned("  取消v2.1的问题。\r\n"+new+"  ",new)
        conditions=protected_conditions(view["effective_original_query"])
        versions=[item["text"] for item in conditions if item["kind"]=="version"]
        self.assertEqual(versions,["v3.2"])
        self.assertTrue(any("最多3次" in item["text"] for item in conditions))
        self.assertTrue(any("不能跳过" in item["text"] for item in conditions))

    def test_standalone_knowledge_query_is_unchanged(self):
        raw="Agent为什么需要终止条件？"
        view=knowledge_view(raw)
        self.assertTrue(view["boundary_reliable"])
        self.assertIsNone(view["control_intent"])
        self.assertEqual(view["effective_original_query"],raw)
        self.assertEqual(view["control_removed_intervals"],[])

    def test_unambiguous_control_and_greeting_stay_whole_message_rules(self):
        for raw,expected in (("取消","cancel"),("你好","greeting")):
            with self.subTest(raw=raw):
                view=knowledge_view(raw)
                self.assertEqual(view["control_intent"],expected)
                self.assertEqual(view["ownership_basis"],"EXACT_COMPLETE_CONTROL")
                self.assertIsNone(view["effective_original_query"])

    def test_missing_new_knowledge_is_preserved_for_existing_understanding(self):
        self.assert_owned("取消上一个问题。换题。","换题。")


    def test_standalone_explicit_topic_switch_uses_complete_message(self):
        for raw in ("换个问题。按Query笔记说明改写解决什么问题。",
                    "改问：Agent怎样设置终止条件？",
                    "换话题，只讨论长上下文的适用条件。"):
            with self.subTest(raw=raw):
                view=knowledge_view(raw)
                self.assertTrue(view["boundary_reliable"])
                self.assertIsNone(view["control_intent"])
                self.assertEqual(view["effective_original_query"],raw)
                self.assertEqual(view["control_removed_intervals"],[])
                self.assertEqual(view["resolution_intent_required"],"replace")

    def test_cancellation_concept_is_knowledge_not_old_issue_control(self):
        for raw in ("取消操作如何实现幂等？","撤销操作的适用条件是什么？",
                    "取消任务和回滚数据有什么区别？"):
            with self.subTest(raw=raw):
                view=knowledge_view(raw)
                self.assertTrue(view["boundary_reliable"])
                self.assertIsNone(view["control_intent"])
                self.assertEqual(view["effective_original_query"],raw)
                self.assertEqual(view["control_removed_intervals"],[])
                self.assert_owned("取消旧问题。"+raw,raw)

    def test_cancellation_concept_in_later_clause_keeps_new_conditions(self):
        new="说明任务幂等，取消操作最多提交一次，不能盲目重试。"
        self.assert_owned("取消上一个问题。"+new,new)


    def test_topic_words_without_a_complete_switch_do_not_force_resolution(self):
        for raw in ("“换个问题”这句话如何理解？","换个问题。",
                    "不要换个问题。先解释原问题。","请不要换话题。",
                    "解释换话题和截断上下文的区别。"):
            with self.subTest(raw=raw):
                view=knowledge_view(raw)
                self.assertTrue(view["boundary_reliable"])
                self.assertIsNone(view["resolution_intent_required"])
                self.assertEqual(view["effective_original_query"],raw)

    def test_explicit_switch_keeps_unicode_conditions_and_original_intervals(self):
        raw=" \r\n请改问：按v3.2解释Agent🧭，最多3次，不能跳过结果校验。  "
        view=knowledge_view(raw)
        self.assertTrue(view["boundary_reliable"])
        self.assertEqual(view["resolution_intent_required"],"replace")
        self.assertIsNone(view["control_intent"])
        self.assertEqual(view["control_removed_intervals"],[])
        self.assertEqual(view["effective_original_query"],raw.strip().replace("\r\n","\n"))
        interval=view["knowledge_intervals"][0]
        self.assertEqual(raw[interval["start"]:interval["end"]],interval["text"])
        conditions=protected_conditions(view["effective_original_query"])
        self.assertTrue(any(item["text"]=="v3.2" for item in conditions))
        self.assertTrue(any("最多3次" in item["text"] for item in conditions))
        self.assertTrue(any("不能跳过" in item["text"] for item in conditions))

    def test_pure_cancel_and_ambiguous_old_control_have_distinct_requirements(self):
        self.assertEqual(knowledge_view("取消")["resolution_intent_required"],"cancel")
        self.assertIsNone(knowledge_view("你好")["resolution_intent_required"])
        for raw in ("不要取消刚才的问题。现在说明Agent。",
                    "取消刚才的问题里的限制。说明Agent。",
                    "取消上一个问题。现在保留旧问题。"):
            with self.subTest(raw=raw):
                view=knowledge_view(raw)
                self.assertFalse(view["boundary_reliable"])
                self.assertIsNone(view["resolution_intent_required"])


if __name__=="__main__":
    unittest.main()
