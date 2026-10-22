from __future__ import annotations
import hashlib
import unittest
from lxml import html,etree
from raghub.ingestion.ui_resources import partition_ui_resources,validate_rules
from raghub.parsing.bundle import web_resource_quality

class WebResourceCoverageTests(unittest.TestCase):
    def page(self):
        doc=html.fromstring('<html><body><div id="sample" class="codetabs"><button class="clipboard-button" data-codetabs-id="sample" onclick="copyCodeToClipboardForCodetabs(this)"><svg viewbox="0 0 24 24"><path d="M1 1L2 2"/></svg></button><pre>SET key value</pre></div></body></html>')
        node=doc.xpath("//svg")[0]
        dependency={"kind":"svg","node_path":doc.getroottree().getpath(node),"source_url":""}
        digest=hashlib.sha256(etree.tostring(node,encoding="utf-8",method="xml",with_tail=False)).hexdigest()
        site={"verified_ui_resources":[{"kind":"svg","content_sha256":digest,"context":"REDIS_COPY","review_ref":"reviewed fixture"}]}
        return doc,dependency,site

    def test_exact_reviewed_icon_and_code_relationship_are_required(self):
        doc,dep,site=self.page()
        kept,excluded=partition_ui_resources(doc,[dep],site,"https://redis.io/docs/example")
        self.assertEqual(kept,[])
        self.assertEqual(excluded[0]["context"],"REDIS_COPY")
        doc.xpath("//path")[0].set("d","M1 1L20 20")
        self.assertEqual(partition_ui_resources(doc,[dep],site,"https://redis.io/docs/example")[0],[dep])

    def test_same_icon_without_code_relationship_stays_unprocessed(self):
        doc,dep,site=self.page()
        doc.xpath("//pre")[0].drop_tree()
        self.assertEqual(partition_ui_resources(doc,[dep],site,"https://redis.io/docs/example")[0],[dep])

    def test_same_icon_on_other_host_stays_unprocessed(self):
        doc,dep,site=self.page()
        self.assertEqual(partition_ui_resources(doc,[dep],site,"https://example.org/docs")[0],[dep])

    def test_unreviewed_background_and_missing_dom_stay_unprocessed(self):
        doc,dep,site=self.page()
        css={"kind":"css-background","node_path":dep["node_path"],"source_url":"url(important.svg)"}
        missing={**dep,"node_path":"/html/missing"}
        self.assertEqual(partition_ui_resources(doc,[css,missing],site,"https://redis.io")[0],[css,missing])

    def test_unknown_graphics_block_final_quality_without_manual_waiver(self):
        prior=[{"code":"OTHER","severity":"WARN"},{"code":"WEB_GRAPHICS_NOT_PROCESSED","severity":"WARN","resolution":{"old":True}}]
        quality=web_resource_quality(prior,{"unsupported_dependencies":[{"kind":"canvas"}]})
        self.assertEqual([q["code"] for q in quality],["OTHER","WEB_GRAPHICS_NOT_PROCESSED"])
        self.assertNotIn("resolution",quality[-1])
        self.assertEqual(quality[-1]["dependency_count"],1)
        self.assertEqual(web_resource_quality(prior,{"unsupported_dependencies":[],"resource_review_required":False}),prior[:1])
        self.assertEqual(web_resource_quality([],{"resource_review_required":True})[-1]["code"],"WEB_GRAPHICS_NOT_PROCESSED")

    def test_policy_rejects_unknown_context_and_unknown_keys(self):
        _,_,site=self.page()
        rule=site["verified_ui_resources"][0]
        for altered in ({**rule,"context":"ANY_SMALL_IMAGE"},{**rule,"ignored":True},{**rule,"content_sha256":"x"*64}):
            with self.assertRaises(ValueError):
                validate_rules([altered])

if __name__=="__main__":
    unittest.main()
