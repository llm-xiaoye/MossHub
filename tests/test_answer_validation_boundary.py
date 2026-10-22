from __future__ import annotations

import copy
import json
import unittest

from raghub.chat.answer_validation import validate_generated_answer
from raghub.ingestion.store import BusinessError


class AnswerValidationBoundaryTests(unittest.TestCase):
    def answer(self, units):
        return {
            "status": "answered",
            "paragraphs": [{"text": "资料给出对应结论。", "support_unit_ids": list(units)}],
            "limitations": [],
        }

    def assert_rejected_without_repair(self, raw):
        before = copy.deepcopy(raw)
        chat = {"stage": "VALIDATE_AND_COMMIT"}
        with self.assertRaises(BusinessError) as caught:
            validate_generated_answer(raw, chat)
        self.assertEqual(caught.exception.code, "ANSWER_SCHEMA_INVALID")
        self.assertIn("答案未提交", str(caught.exception))
        self.assertIn("检索记录已保留", str(caught.exception))
        self.assertIn("先查看本轮状态", str(caught.exception))
        self.assertEqual(raw, before)
        self.assertNotIn("formal_result", chat)
        failure = chat["answer_validation_failure"]
        self.assertFalse(failure["automatic_repair"])
        self.assertFalse(failure["answer_committed"])
        self.assertTrue(failure["violations"])
        json.dumps(chat, ensure_ascii=False)
        return failure

    def test_valid_six_units_and_evidence_insufficient_are_preserved(self):
        answers = [
            self.answer([f"U{index}" for index in range(6)]),
            {"status": "insufficient", "paragraphs": [], "limitations": ["资料缺少所问事实。"]},
        ]
        for raw in answers:
            with self.subTest(status=raw["status"]):
                before = copy.deepcopy(raw)
                chat = {}
                self.assertEqual(validate_generated_answer(raw, chat), before)
                self.assertEqual(raw, before)
                self.assertNotIn("answer_validation_failure", chat)

    def test_eight_units_fail_without_truncation_or_formal_commit(self):
        raw = self.answer([f"U{index}" for index in range(8)])
        failure = self.assert_rejected_without_repair(raw)
        self.assertEqual(len(raw["paragraphs"][0]["support_unit_ids"]), 8)
        self.assertEqual(failure["violations"][0]["location"], ["paragraphs", 0, "support_unit_ids"])
        self.assertEqual(failure["violations"][0]["type"], "too_long")

    def test_repeated_units_fail_without_deduplication(self):
        raw = self.answer(["U1", "U1"])
        self.assert_rejected_without_repair(raw)
        self.assertEqual(raw["paragraphs"][0]["support_unit_ids"], ["U1", "U1"])

    def test_missing_and_unknown_fields_keep_strict_contract(self):
        missing = self.answer(["U1"])
        del missing["limitations"]
        unknown = self.answer(["U1"])
        unknown["unrequested_field"] = True
        for raw in (missing, unknown):
            with self.subTest(keys=sorted(raw)):
                self.assert_rejected_without_repair(raw)


if __name__ == "__main__":
    unittest.main()

