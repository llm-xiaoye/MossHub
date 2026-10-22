from __future__ import annotations
import hashlib
import tempfile
import unittest
from pathlib import Path
from raghub.parsing import formats

# Verbatim public Obsidian help callouts; no reference answer is ingested.
CALLOUTS = [
    ("> [!info]+ Alternative access method\n> If using the **Obsidian frame** Window frame style, you can also access the Obsidian console via the title menu. \n", "info", "+", "Alternative access method"),
    ("> [!warning]+ Mobile console access\n> Mobile devices and tablets have limited access to their developer consoles. For these users, we recommend using the community plugin [Logstravaganza](https://community.obsidian.md/plugins/logstravaganza) to automatically generate the console log data in a markdown note.\n", "warning", "+", "Mobile console access"),
]

class ObsidianCalloutAlignmentTests(unittest.TestCase):
    def parse(self, callout, transform=None, neighbours=True):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"callout.md"
            source=("# Before verified\n\n" if neighbours else "")+callout+("\n# After verified\n" if neighbours else "")
            path.write_text(source, encoding="utf-8")
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            parsed=formats.native_document(path,"md",source_digest=digest)
            if transform is not None:
                transform(parsed)
            return source,formats.bind_native(parsed,path,"md",digest)

    def test_both_real_callouts_bind_complete_native_fragments(self):
        for note,kind,fold,title in CALLOUTS:
            with self.subTest(kind=kind):
                source,p=self.parse(note)
                self.assertFalse([q for q in p.quality if q["code"]=="COARSE_SOURCE_MAPPING"])
                regions=p.details["obsidian_callout_regions"]
                self.assertEqual(len(regions),1)
                region=regions[0]
                self.assertTrue(region["complete_binding"])
                self.assertEqual((region["type"],region["fold"],region["title"]),(kind,fold,title))
                self.assertEqual(region["body"],note.split("\n> ",1)[1].rstrip("\n"))
                self.assertEqual(len(region["field_pointers"]),3)
                for pointer in region["field_pointers"]:
                    loc=p.locations[pointer]
                    self.assertEqual(loc["basis"],"T17_EXACT_COMPLETE_OBSIDIAN_CALLOUT")
                    self.assertEqual(loc["granularity"],"REGION")
                    raw=source[loc["source_start"]:loc["source_end"]]
                    self.assertEqual(raw,note)
                    self.assertIn(pointer,p.details["protected_fields"])

    def reject(self, transform):
        _source,p=self.parse(CALLOUTS[1][0],transform)
        self.assertFalse(p.details["obsidian_callout_regions"][0]["complete_binding"])
        self.assertTrue([q for q in p.quality if q["code"]=="COARSE_SOURCE_MAPPING"])

    def test_changed_condition_is_not_automatically_bound(self):
        self.reject(lambda p:setattr(next(t for t in p.document.texts if "limited access" in t.text),"text",next(t.text for t in p.document.texts if "limited access" in t.text).replace("limited access","unrestricted access")))

    def test_missing_body_is_not_automatically_bound(self):
        self.reject(lambda p:setattr(next(t for t in p.document.texts if t.text.startswith("to automatically")),"text",""))

    def test_changed_fold_marker_is_not_automatically_bound(self):
        self.reject(lambda p:setattr(next(t for t in p.document.texts if "[!warning]+" in t.text),"text",next(t.text for t in p.document.texts if "[!warning]+" in t.text).replace("[!warning]+","[!warning]-")))

    def test_reordered_fragments_are_not_automatically_bound(self):
        def reorder(p):
            first=next(t for t in p.document.texts if "[!warning]+" in t.text)
            second=next(t for t in p.document.texts if t.text=="Logstravaganza")
            first.text,second.text=second.text,first.text
        self.reject(reorder)

    def test_duplicate_original_blocks_remain_ambiguous(self):
        _source,p=self.parse(CALLOUTS[0][0]+"\n"+CALLOUTS[0][0])
        self.assertEqual(len(p.details["obsidian_callout_regions"]),2)
        self.assertTrue(all(not r["complete_binding"] for r in p.details["obsidian_callout_regions"]))

    def test_no_verified_neighbour_retains_review(self):
        _source,p=self.parse(CALLOUTS[0][0],neighbours=False)
        self.assertFalse(p.details["obsidian_callout_regions"][0]["complete_binding"])
        self.assertTrue(p.quality)

if __name__=="__main__":
    unittest.main()

