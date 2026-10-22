"""Regression for the frozen official multilingual evidence-tokenizer pipeline."""
import json
import unittest
from unittest.mock import Mock
from raghub.config import Settings
from raghub.retrieval.tokenizer import EvidenceTokenizer


class EvidenceTokenizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.counter = EvidenceTokenizer(Settings.load())

    def test_chinese_text_is_counted_without_loss(self):
        text = "必要上下文核对"
        self.assertEqual(self.counter.count(text), 3)
        ids = self.counter.tokenizer.encode(text, add_special_tokens=True).ids
        self.assertEqual(self.counter.tokenizer.decode(ids, skip_special_tokens=False), text)

    def test_whole_json_includes_chinese_and_wrappers(self):
        serialized = '{"groups":[{"text":"资料"}]}'
        self.assertEqual(self.counter.count(serialized), 10)
        self.assertGreater(self.counter.count(serialized), self.counter.count("资料"))

    def test_units_negation_and_multilingual_text_roundtrip(self):
        for text in ["百万美元；不包含硬件，适用于2018年。", "日本語の表 📚\nRAG\r\n918 million", "<｜User｜>中文<｜Assistant｜>"]:
            with self.subTest(text=text):
                encoded = self.counter.tokenizer.encode(text, add_special_tokens=True)
                self.assertGreater(self.counter.count(text), 0)
                self.assertEqual(self.counter.tokenizer.decode(encoded.ids, skip_special_tokens=False), text)

    def test_evidence_above_hard_budget_is_not_silently_truncated(self):
        text = json.dumps({"text": "必要上下文核对" * 3000}, ensure_ascii=False)
        self.assertGreater(self.counter.count(text), 8000)
        encoded = self.counter.tokenizer.encode(text, add_special_tokens=True)
        self.assertEqual(self.counter.tokenizer.decode(encoded.ids, skip_special_tokens=False), text)

    def test_non_string_is_rejected(self):
        for value in [None, [], {}, 123]:
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.counter.count(value)

    def test_nonempty_zero_token_result_fails_closed(self):
        broken = object.__new__(EvidenceTokenizer)
        broken.tokenizer = Mock()
        broken.tokenizer.encode.return_value.ids = []
        with self.assertRaisesRegex(ValueError, "nonempty evidence"):
            broken.count("中文")


if __name__ == "__main__":
    unittest.main()

