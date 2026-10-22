"""Assembler regressions for context/body role coverage through real token counting."""
import unittest
from types import SimpleNamespace

from raghub.config import Settings
from raghub.retrieval.evidence import EvidenceAssembler
from raghub.retrieval.support_scope import support_texts
from raghub.retrieval.tokenizer import EvidenceTokenizer


class Locator:
    def __init__(self, pointer):
        self.value = {"field_pointer": pointer, "kind": "TEST_BOUND_TEXT"}

    def model_dump(self, mode=None):
        return dict(self.value)


def artifacts_fixture(*, table=False, context=True, body_purpose="BODY"):
    document = {"document_id": "doc", "raw_version_id": "raw", "processing_version_id": "pv"}
    text, parts, fields = "", [], {}

    def append(value, pointer=None, purpose="BODY"):
        nonlocal text
        start = len(text)
        text += value
        part = {"start": start, "end": len(text), "constructed": pointer is None}
        if pointer is not None:
            locator = Locator(pointer)
            length = len(value)
            mapping = SimpleNamespace(input_intervals=[[0, length]], input_start=0,
                                      input_end=length, output_start=0, output_end=length,
                                      operation="UNCHANGED", precision="EXACT_UNCHANGED_INTERVAL")
            fields[("pv", pointer)] = SimpleNamespace(original_text=value, cleaned_text=value,
                                                     mappings=[mapping], locator=locator)
            part.update(field_pointer=pointer, purpose=purpose, cleaned_start=0,
                        cleaned_end=length, locator=locator.model_dump())
        parts.append(part)

    if context:
        append("标题：")
        append("时刻与数字", "/title", "TITLE")
        append("\n")
        append("单位：百万；仅标准场适用", "/condition", "CONTEXT")
        append("\n")
    body_start = len(text)
    if table:
        append("第1行：")
        append("FY2018 | 918", "/row1", body_purpose)
        append("\n")
        append("第2行：")
        append("FY2019 | 924", "/row2", body_purpose)
    else:
        append("标准场开始时间为上午9:00。", "/body", body_purpose)
    parent = {"parent_id": "parent", "source_record_id": "source", "text": text,
              "body": text[body_start:], "content_kind": "table" if table else "text"}
    child = {"chunk_id": "child", "source_record_id": "source", "parent_id": "parent",
             "text": text}
    artifacts = SimpleNamespace(
        snapshot={"kb_id": "kb", "generation_id": "generation",
                  "removal_snapshot": {"revision": 0, "removed_document_ids": []}},
        parents={"parent": parent}, children={"child": child},
        sources={"source": {"source_record_id": "source", "parts": parts}},
        fields=fields, document_by_child={"child": document})
    seeds = [{"chunk_id": "child", "parent_id": "parent"}]
    groups = [{"parent_id": "parent", "selected_seed_ids": ["child"],
               "all_hit_child_ids": ["child"]}]
    return artifacts, seeds, groups


class EvidenceAssemblerCoverageChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assembler = EvidenceAssembler(EvidenceTokenizer(Settings.load()))

    def assemble(self, **kwargs):
        artifacts, seeds, groups = artifacts_fixture(**kwargs)
        value = self.assembler.assemble("assembly-test", artifacts, seeds, groups)
        if value["groups"]:
            texts = support_texts(value["groups"], value["support_unit_whitelist"],
                                  kb_id="kb", generation_id="generation")
        else:
            texts = {}
        return value, texts

    def test_bound_heading_and_body_are_checked_against_their_own_roles(self):
        value, texts = self.assemble()
        self.assertFalse(value["no_evidence"])
        self.assertEqual(len(texts), 1)
        excerpt = next(iter(texts.values()))
        self.assertIn("单位：百万；仅标准场适用", excerpt)
        self.assertIn("标准场开始时间为上午9:00。", excerpt)
        roles = {source["support_role"] for unit in value["support_unit_whitelist"].values()
                 for source in unit["sources"]}
        self.assertEqual(roles, {"CONTEXT", "BODY"})
        self.assertLessEqual(value["evidence_token_count"], 8000)

    def test_table_rows_keep_both_operands_and_bound_scale(self):
        value, texts = self.assemble(table=True)
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("FY2018 | 918" in text for text in texts.values()))
        self.assertTrue(any("FY2019 | 924" in text for text in texts.values()))
        self.assertTrue(all("单位：百万；仅标准场适用" in text for text in texts.values()))
        for unit in value["support_unit_whitelist"].values():
            bodies = [source for source in unit["sources"] if source["support_role"] == "BODY"]
            self.assertEqual(len(bodies), 1)
            self.assertIn(bodies[0]["field_pointer"], {"/row1", "/row2"})

    def test_no_prefix_remains_valid_body_evidence(self):
        value, texts = self.assemble(context=False)
        self.assertEqual(len(texts), 1)
        self.assertEqual(next(iter(texts.values())), "标准场开始时间为上午9:00。")
        self.assertEqual({part["role"] for unit in value["support_unit_whitelist"].values()
                          for part in unit["support_parts"]}, {"BODY"})

    def test_heading_and_conditions_cannot_stand_in_for_missing_body_support(self):
        value, texts = self.assemble(body_purpose="HEADER")
        self.assertTrue(value["no_evidence"])
        self.assertEqual(texts, {})
        self.assertEqual(value["support_unit_whitelist"], {})


if __name__ == "__main__":
    unittest.main()
