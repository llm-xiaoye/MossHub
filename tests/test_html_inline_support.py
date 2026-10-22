from copy import deepcopy
from hashlib import sha256

from lxml import html

from raghub.parsing.html_blocks import block_source, closure_proof
from raghub.retrieval.evidence import body_ranges


def bundle(original, pieces, xpath="/html/body/p"):
    node = html.fromstring("<p>" + original + "</p>")
    source = {"xpath": xpath, **block_source(node)}
    items = [("/texts/" + str(i) + "/text", value) for i, value in enumerate(pieces)]
    proof = closure_proof(source, "a" * 64, items)
    assert proof is not None
    locator = {"kind": "html", "file_sha256": "a" * 64, "xpath": xpath,
               "granularity": "REGION", "basis": "DOCLING_BACKEND_COMPLETE_INLINE_BLOCK",
               "source_group": [proof]}
    text, parts = "", []
    for i, (pointer, value) in enumerate(items):
        if i:
            parts.append({"start": len(text), "end": len(text)+1, "constructed": True})
            text += "\n"
        parts.append({"start": len(text), "end": len(text)+len(value), "constructed": False,
                      "purpose": "BODY", "field_pointer": pointer, "cleaned_start": 0,
                      "cleaned_end": len(value), "locator": deepcopy(locator)})
        text += value
    return {"text": text, "content_kind": "text"}, {"parts": parts}


def test_inline_code_condition_is_one_complete_support_unit():
    parent, source = bundle("If the exception matches <code>ValueError</code>, run the handler.",
                            ["If the exception matches", "ValueError", ", run the handler."])
    units = body_ranges(parent, source, 0, 0, len(parent["text"]))
    assert units == [(0, len(parent["text"]), "text_span")]
    assert len([part for part in source["parts"] if not part["constructed"]]) == 3


def test_missing_negation_or_intermediate_word_cannot_close():
    source = {"xpath": "/p", **block_source(html.fromstring("<p>If it does not match, propagate.</p>"))}
    assert closure_proof(source, "a"*64, [("/texts/0/text", "If it does match, propagate.")]) is None
    assert closure_proof(source, "a"*64, [("/texts/0/text", "propagate.")]) is None


def test_container_and_nested_blocks_are_not_inline_closure():
    for value in ("<ul><li>A</li><li>B</li></ul>", "<li>A<ul><li>B</li></ul></li>",
                  "<p>A<br>B</p>", "<li><p>A</p><p>B</p></li>",
                  "<li>Outside<p>Inside</p></li>"):
        assert block_source(html.fromstring(value))["inline_block"] is False


def test_same_ul_locator_without_complete_proof_stays_separate():
    parent, source = bundle("If <code>A</code>, do B.", ["If", "A", ", do B."])
    for part in source["parts"]:
        if not part["constructed"]:
            part["locator"] = {"kind": "html", "file_sha256": "a"*64, "xpath": "/ul",
                               "granularity": "REGION", "basis": "DOCLING_BACKEND_SOURCE_NODE"}
    assert len(body_ranges(parent, source, 0, 0, len(parent["text"]))) == 3


def test_incomplete_child_and_tampered_proof_do_not_close():
    parent, source = bundle("If <code>A</code>, do B.", ["If", "A", ", do B."])
    assert len(body_ranges(parent, source, 0, 0, source["parts"][2]["end"])) == 2
    for part in source["parts"]:
        if not part["constructed"]:
            part["locator"]["source_group"][0]["normalized_text_sha256"] = "b"*64
    assert len(body_ranges(parent, source, 0, 0, len(parent["text"]))) == 3


def test_backend_binds_distinct_list_items_to_original_li(tmp_path):
    from raghub.parsing.formats import native_document
    from raghub.parsing.source_backends import HTML_SOURCE_ATTR
    original = b"<html><body><ul><li><p>If it matches <code>ValueError</code>, handle it.</p></li><li>If it does not match, propagate.</li></ul></body></html>"
    tree = html.fromstring(original)
    nodes = {}
    for i, node in enumerate(tree.iter()):
        key = "source-" + str(i)
        nodes[key] = {"xpath": node.getroottree().getpath(node), "protected": False,
                      **block_source(node)}
        node.set(HTML_SOURCE_ATTR, key)
    path = tmp_path / "annotated.html"
    path.write_bytes(html.tostring(tree))
    parsed = native_document(path, "html", source_nodes=nodes, source_digest=sha256(original).hexdigest())
    locations = list(parsed.locations.values())
    closed = [loc for loc in locations if loc["basis"] == "DOCLING_BACKEND_COMPLETE_INLINE_BLOCK"]
    assert closed
    assert {loc["xpath"] for loc in closed} == {"/html/body/ul/li[1]", "/html/body/ul/li[2]"}
    for xpath in {loc["xpath"] for loc in closed}:
        members = [loc for loc in closed if loc["xpath"] == xpath]
        assert all(loc["source_group"] == members[0]["source_group"] for loc in members)
        assert len(members) == len(members[0]["source_group"][0]["members"])




def test_chunk_builder_preserves_complete_inline_members_and_rejects_changes():
    from types import SimpleNamespace
    from raghub.parsing.contracts import HTMLLocation
    from raghub.indexing.chunks import complete_html_fields, ChunkingUnsupported
    parent, source = bundle("If <code>ValueError</code>, handle it.",
                            ["If", "ValueError", ", handle it."])
    fields = {part["field_pointer"]: SimpleNamespace(
        cleaned_text=parent["text"][part["start"]:part["end"]],
        locator=HTMLLocation.model_validate(part["locator"]))
        for part in source["parts"] if not part["constructed"]}
    assert complete_html_fields(fields, "/texts/0/text") == list(fields)
    fields["/texts/1/text"].cleaned_text = "TypeError"
    try:
        complete_html_fields(fields, "/texts/0/text")
    except ChunkingUnsupported:
        pass
    else:
        raise AssertionError("changed inline source was accepted")

def load_tests(loader, tests, pattern):
    import tempfile
    import unittest
    from pathlib import Path

    def backend_case():
        with tempfile.TemporaryDirectory(prefix="rag-html-") as root:
            test_backend_binds_distinct_list_items_to_original_li(Path(root))

    names = [test_inline_code_condition_is_one_complete_support_unit,
             test_missing_negation_or_intermediate_word_cannot_close,
             test_container_and_nested_blocks_are_not_inline_closure,
             test_same_ul_locator_without_complete_proof_stays_separate,
             test_incomplete_child_and_tampered_proof_do_not_close,
             test_chunk_builder_preserves_complete_inline_members_and_rejects_changes, backend_case]
    return unittest.TestSuite(unittest.FunctionTestCase(case) for case in names)
