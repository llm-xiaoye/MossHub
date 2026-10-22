"""Prove complete HTML inline blocks without merging adjacent DOM blocks."""
from __future__ import annotations

import hashlib
import re

POLICY = "HTML_INLINE_BLOCK_CLOSURE_V1"
BLOCK_TAGS = {"p", "li"}
NESTED_BLOCKS = {"p", "li", "ul", "ol", "dl", "div", "table", "pre", "blockquote",
                 "section", "article", "figure", "br", "img", "input", "script", "style"}


def normalize(value):
    return re.sub(r"\s+", " ", value).strip()


def block_source(node):
    """A p/li is one block; allow a li's sole complete p wrapper."""
    nested = [child for child in node.iter()
              if child is not node and isinstance(child.tag, str)
              and child.tag in NESTED_BLOCKS]
    text = normalize("".join(node.itertext()))
    single_paragraph = (node.tag == "li" and len(nested) == 1
                        and nested[0].tag == "p" and nested[0].getparent() is node
                        and normalize("".join(nested[0].itertext())) == text)
    eligible = node.tag in BLOCK_TAGS and (not nested or single_paragraph)
    return {"inline_block": eligible,
            "normalized_text": text if eligible else None}

def closure_proof(source, digest, items):
    """Match every emitted field to the whole original block, in order.

    Only original whitespace can occur between fields. Repeated equal fragments
    are resolved by the complete ordered sequence, never by first-match binding.
    """
    original = source.get("normalized_text")
    if not source.get("inline_block") or not original or not items:
        return None
    cursor, members = 0, []
    for pointer, value in items:
        value = normalize(value)
        if not value:
            return None
        begin = cursor
        while begin < len(original) and original[begin].isspace():
            begin += 1
        stop = begin + len(value)
        if original[begin:stop] != value:
            return None
        members.append({"field_pointer": pointer, "start": begin, "end": stop})
        cursor = stop
    if cursor != len(original):
        return None
    return {"policy": POLICY, "xpath": source["xpath"], "file_sha256": digest,
            "normalized_text_length": len(original),
            "normalized_text_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "members": members}
