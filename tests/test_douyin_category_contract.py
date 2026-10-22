"""Regression for the real DY catalog path/segment mismatch; no network or vault writes."""
import copy
import unittest

from raghub.douyin_worker import _category_options, _note_analysis
from test_douyin_intake import NoteEvidenceTest


class CategoryContractTest(unittest.TestCase):
    def test_existing_catalog_paths_are_sent_as_relative_directory_arrays(self):
        value = {"notes_root": "知识库", "categories": [
            "知识库/AI/Agent/基础与架构/多智能体协作",
            "知识库/方法与应用/Prompt与意图理解",
        ]}
        original = copy.deepcopy(value)
        self.assertEqual(_category_options(value), [
            ["AI", "Agent", "基础与架构", "多智能体协作"],
            ["方法与应用", "Prompt与意图理解"],
        ])
        self.assertEqual(value, original)

    def test_catalog_root_is_exact_and_no_directory_is_guessed(self):
        for path in ["别的知识库/AI/Agent", "知识库/AI/../Agent",
                     "知识库/AI/.隐藏", "知识库/AI/Agent/一/二/三"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                _category_options({"notes_root": "知识库", "categories": [path]})

    def test_the_observed_single_slash_path_is_rejected_before_save(self):
        value = NoteEvidenceTest().value()
        value["analysis"]["category"] = ["知识库/AI/Agent/基础与架构/多智能体协作"]
        with self.assertRaisesRegex(ValueError, "category"):
            _note_analysis(value, "工具需要明确输入，而且仍需核对输出。")

    def test_invalid_directory_names_do_not_reach_commit(self):
        for category in [["AI", "Agent/基础与架构"], ["AI", "../Agent"],
                         ["AI", ".Agent"], ["AI", " "], ["AI", "a" * 25], "AI/Agent"]:
            value = NoteEvidenceTest().value()
            value["analysis"]["category"] = category
            with self.subTest(category=category), self.assertRaises(ValueError):
                _note_analysis(value, "工具需要明确输入，而且仍需核对输出。")

    def test_valid_metadata_keeps_source_evidence_and_category(self):
        value = NoteEvidenceTest().value()
        value["analysis"]["category"] = ["AI", "Agent", "基础与架构", "多智能体协作"]
        body, analysis = _note_analysis(value, "工具需要明确输入，而且仍需核对输出。")
        self.assertEqual(analysis["category"], value["analysis"]["category"])
        self.assertIn("工具需要明确输入", body)
        self.assertIn("尚未人工复审", body)
