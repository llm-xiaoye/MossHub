from __future__ import annotations
import hashlib
import tempfile
import unittest
from pathlib import Path
from raghub.parsing import formats
from raghub.parsing.alignment import align


class MarkdownHeadingIdentityTests(unittest.TestCase):
    def parse(self, source, mutate=None):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"heading.md"
            path.write_text(source,encoding="utf-8")
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            parsed=formats.native_document(path,"md",source_digest=digest)
            if mutate is not None:
                mutate(parsed.document)
            return formats.bind_native(parsed,path,"md",digest)

    def mapping_warnings(self, parsed):
        return [q for q in parsed.quality if q["code"]=="COARSE_SOURCE_MAPPING"]

    def test_real_note_duplicate_title_and_section_bind_distinct_original_lines(self):
        titles=["RAG 的 Query 改写：判断你是否真做过项目的一道题",
                "AI Agent 与传统 RAG 的核心区别"]
        for title in titles:
            with self.subTest(title=title):
                source="# "+title+"\n\n## "+title+"\n\n后续正文。\n"
                parsed=self.parse(source)
                self.assertEqual(self.mapping_warnings(parsed),[])
                locations=[parsed.locations["/texts/"+str(i)+"/text"] for i in (0,1)]
                self.assertEqual([loc["line_start"] for loc in locations],[1,3])
                self.assertEqual([source[loc["source_start"]:loc["source_end"]] for loc in locations],
                                 ["# "+title+"\n","## "+title+"\n"])
                self.assertTrue(all(loc["basis"]=="T18_EXACT_COMPLETE_MARKDOWN_HEADING_LEVEL" for loc in locations))

    def test_duplicate_same_level_headings_remain_ambiguous(self):
        for marker in ("#","##"):
            with self.subTest(marker=marker):
                parsed=self.parse(marker+" 重复标题\n\n"+marker+" 重复标题\n\n正文。\n")
                self.assertNotIn("/texts/0/text",parsed.locations)
                self.assertNotIn("/texts/1/text",parsed.locations)
                self.assertEqual(len(self.mapping_warnings(parsed)),2)

    def test_all_markdown_heading_levels_preserve_distinct_source_identity(self):
        source="\n".join("#"*level+" 相同文字\n" for level in range(1,7))+"\n正文。\n"
        parsed=self.parse(source)
        self.assertEqual(self.mapping_warnings(parsed),[])
        self.assertEqual([parsed.locations["/texts/"+str(i)+"/text"]["line_start"] for i in range(6)],
                         [1,3,5,7,9,11])

    def test_incorrect_native_level_does_not_borrow_equal_text(self):
        def mutate(doc):
            doc.texts[1].level=2
        parsed=self.parse("# 同名\n\n## 同名\n\n正文。\n",mutate)
        self.assertNotIn("/texts/1/text",parsed.locations)
        self.assertTrue(any(q["field"]=="/texts/1/text" for q in self.mapping_warnings(parsed)))

    def test_changed_native_heading_text_stays_unbound(self):
        def mutate(doc):
            doc.texts[1].text="已经改写的标题"
        parsed=self.parse("# 同名\n\n## 同名\n\n正文。\n",mutate)
        self.assertNotIn("/texts/1/text",parsed.locations)
        self.assertTrue(self.mapping_warnings(parsed))

    def test_heading_and_equal_body_text_keep_separate_original_ranges(self):
        source="# 相同文字\n\n相同文字\n\n结束。\n"
        parsed=self.parse(source)
        self.assertEqual(self.mapping_warnings(parsed),[])
        heading=parsed.locations["/texts/0/text"]
        body=parsed.locations["/texts/1/text"]
        self.assertEqual((heading["line_start"],body["line_start"]),(1,3))
        self.assertNotEqual(heading["source_start"],body["source_start"])

    def atom(self,text,level=None,complete=False,pointer=None):
        value={"id":text,"text":text,"kind":"paragraph","plain":False,"locator":None}
        if pointer is not None:
            value["pointer"]=pointer
        if level is not None:
            value["markdown_heading_level"]=level
        if complete:
            value["complete_heading_text"]=text
        return value

    def test_reversed_heading_order_rejects_both_crossing_bindings(self):
        anchors=[self.atom("同名",1,True),self.atom("同名",2,True)]
        fields=[self.atom("同名",2,pointer="/texts/0/text"),
                self.atom("同名",1,pointer="/texts/1/text")]
        accepted,diagnostics=align(anchors,fields)
        self.assertEqual(accepted,[])
        self.assertEqual(len(diagnostics),2)

    def test_unique_text_with_wrong_or_missing_structure_is_not_enough(self):
        for level in (None,2,0,True):
            with self.subTest(level=level):
                accepted,diagnostics=align([self.atom("唯一标题",1,True)],
                                           [self.atom("唯一标题",level,pointer="/texts/0/text")])
                self.assertEqual(accepted,[])
                self.assertEqual(len(diagnostics),1)

    def test_heading_like_text_in_code_is_not_a_heading(self):
        source="# 同名\n\n```markdown\n## 同名\n```\n\n正文。\n"
        parsed=self.parse(source)
        self.assertEqual(self.mapping_warnings(parsed),[])
        code=[item for item in parsed.document.texts if item.label.value=="code"]
        self.assertEqual(len(code),1)
        self.assertEqual(code[0].text,"## 同名")


if __name__=="__main__":
    unittest.main()

