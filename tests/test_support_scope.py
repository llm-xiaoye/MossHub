"""Citation-contract checks; these do not assert semantic support."""
import copy
import hashlib
import unittest
from raghub.retrieval.support_scope import SUPPORT_SCOPE, COMPOSITE_FIELDS, support_texts

def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def fixture():
    context, body = "单位：百万元\n", "传感器 | 918\n"
    identity = {"kb_id":"kb", "generation_id":"gen", "document_id":"doc",
                "raw_version_id":"raw", "processing_version_id":"pv", "source_record_id":"source"}
    unit = {**identity, "evidence_id":"E1", "support_unit_id":"U1", "parent_id":"P1",
        "parent_text_interval":[7,17], "text_sha256":sha(body), "parent_text_length":26,
        "support_scope":SUPPORT_SCOPE, "support_text_sha256":sha("单位：百万元\n传感器 | 918\n"),
        "support_parts":[{"role":"CONTEXT","parent_text_interval":[0,7],"text_sha256":sha(context)},
                         {"role":"BODY","parent_text_interval":[7,17],"text_sha256":sha(body)}],
        "sources":[{**identity,"purpose":"HEADER","support_role":"CONTEXT","parent_text_interval":[0,7]},
                   {**identity,"purpose":"BODY","support_role":"BODY","parent_text_interval":[7,17]}]}
    group = {"evidence_id":"E1","document_id":"doc","raw_version_id":"raw","processing_version_id":"pv",
             "context_text":context,"support_scope":SUPPORT_SCOPE,
             "support_units":[{"support_unit_id":"U1","text":body,"kind":"table_row"}]}
    return [group], {"U1":unit}

class SupportScopeTests(unittest.TestCase):
    def run_contract(self, groups, whitelist):
        return support_texts(groups, whitelist, kb_id="kb", generation_id="gen")

    def test_exact_context_and_selected_body_exclude_other_rows(self):
        groups, whitelist = fixture()
        self.assertEqual(self.run_contract(groups,whitelist),
                         {"U1":"单位：百万元\n传感器 | 918\n"})
        self.assertNotIn("其他 | 100",self.run_contract(groups,whitelist)["U1"])

    def test_budget_excluded_support_is_not_citable(self):
        groups, whitelist = fixture()
        with self.assertRaisesRegex(ValueError,"unsent"):
            self.run_contract([],whitelist)

    def test_context_change_with_unchanged_body_hash_is_rejected(self):
        groups, whitelist = fixture()
        groups[0]["context_text"] = "单位：千万元\n"
        with self.assertRaisesRegex(ValueError,"context or composite"):
            self.run_contract(groups,whitelist)

    def test_unknown_scope_and_incomplete_parts_are_rejected(self):
        for mutate in ("unknown_scope","missing_parts","missing_hash","missing_parent_length"):
            with self.subTest(mutate=mutate):
                groups, whitelist = fixture()
                if mutate == "unknown_scope":
                    groups[0]["support_scope"] = "UNKNOWN"
                else:
                    key = {"missing_parts":"support_parts","missing_hash":"support_text_sha256",
                           "missing_parent_length":"parent_text_length"}[mutate]
                    del whitelist["U1"][key]
                with self.assertRaises(ValueError):
                    self.run_contract(groups,whitelist)

    def test_legacy_body_only_remains_readable_but_partial_metadata_is_rejected(self):
        groups, whitelist = fixture()
        del groups[0]["support_scope"]
        for key in COMPOSITE_FIELDS:
            whitelist["U1"].pop(key)
        whitelist["U1"]["sources"] = [whitelist["U1"]["sources"][1]]
        del whitelist["U1"]["sources"][0]["support_role"]
        self.assertEqual(self.run_contract(groups,whitelist),{"U1":"传感器 | 918\n"})
        whitelist["U1"]["support_text_sha256"] = sha("传感器 | 918\n")
        with self.assertRaisesRegex(ValueError,"partial composite"):
            self.run_contract(groups,whitelist)

    def test_context_source_cannot_cross_raw_kb_or_generation(self):
        for key in ("raw_version_id","kb_id","generation_id"):
            with self.subTest(key=key):
                groups, whitelist = fixture()
                whitelist["U1"]["sources"][0][key] = "other"
                with self.assertRaisesRegex(ValueError,"crosses"):
                    self.run_contract(groups,whitelist)

    def test_context_alone_cannot_replace_body(self):
        groups, whitelist = fixture()
        whitelist["U1"]["sources"] = [whitelist["U1"]["sources"][0]]
        with self.assertRaisesRegex(ValueError,"context alone"):
            self.run_contract(groups,whitelist)

    def test_body_range_cannot_expand_to_unselected_row_or_context(self):
        for bounds in ([0,17],[7,26]):
            with self.subTest(bounds=bounds):
                groups, whitelist = fixture()
                whitelist["U1"]["support_parts"][1]["parent_text_interval"] = bounds
                with self.assertRaises(ValueError):
                    self.run_contract(groups,whitelist)

    def test_wrong_source_role_and_duplicate_unit_are_rejected(self):
        groups, whitelist = fixture()
        whitelist["U1"]["sources"][1]["support_role"] = "CONTEXT"
        with self.assertRaisesRegex(ValueError,"source position"):
            self.run_contract(groups,whitelist)
        groups, whitelist = fixture()
        groups[0]["support_units"].append(copy.deepcopy(groups[0]["support_units"][0]))
        with self.assertRaisesRegex(ValueError,"duplicate support"):
            self.run_contract(groups,whitelist)

    def test_empty_context_does_not_require_invented_context_source(self):
        groups, whitelist = fixture()
        body = "传感器 | 918\n"
        groups[0]["context_text"] = ""
        unit = whitelist["U1"]
        unit.update(parent_text_interval=[0,10],parent_text_length=10,support_text_sha256=sha(body),
                    support_parts=[{"role":"BODY","parent_text_interval":[0,10],"text_sha256":sha(body)}])
        unit["sources"] = [unit["sources"][1]]
        unit["sources"][0]["parent_text_interval"] = [0,10]
        self.assertEqual(self.run_contract(groups,whitelist),{"U1":body})

if __name__ == "__main__":
    unittest.main()

