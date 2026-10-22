"""Close only whole HTML blocks proven by the archived-DOM parser."""
from __future__ import annotations

import re
from ..model_profiles import text_sha256

POLICY = "HTML_INLINE_BLOCK_CLOSURE_V1"


def _proof(locator):
    if (type(locator) is not dict or locator.get("kind") != "html"
            or locator.get("granularity") != "REGION"
            or locator.get("basis") != "DOCLING_BACKEND_COMPLETE_INLINE_BLOCK"):
        return None
    groups = locator.get("source_group")
    if type(groups) is not list or len(groups) != 1 or type(groups[0]) is not dict:
        return None
    value = groups[0]
    if (set(value) != {"policy", "xpath", "file_sha256", "normalized_text_length",
                       "normalized_text_sha256", "members"}
            or value["policy"] != POLICY or value["xpath"] != locator.get("xpath")
            or value["file_sha256"] != locator.get("file_sha256")
            or re.fullmatch(r"[0-9a-f]{64}", str(value["file_sha256"])) is None
            or type(value["members"]) is not list or not value["members"]):
        return None
    return value


def _complete_span(text, parts, index):
    first = parts[index]
    if first.get("constructed") is not False or first.get("purpose") != "BODY":
        return None
    locator = first.get("locator")
    proof = _proof(locator)
    if proof is None:
        return None
    cursor, position, normalized, pointers = first["start"], index, "", set()
    for member in proof["members"]:
        if type(member) is not dict or set(member) != {"field_pointer", "start", "end"}:
            return None
        if position >= len(parts):
            return None
        part = parts[position]
        if (part.get("constructed") is not False or part.get("purpose") != "BODY"
                or part.get("locator") != locator or part["start"] != cursor
                or part.get("field_pointer") != member["field_pointer"]
                or part.get("cleaned_start") != 0
                or part["cleaned_end"] != part["end"] - part["start"]
                or member["field_pointer"] in pointers):
            return None
        value = re.sub(r"\s+", " ", text[part["start"]:part["end"]]).strip()
        gap = member["start"] - len(normalized)
        if (not value or gap not in {0, 1}
                or member["end"] - member["start"] != len(value)):
            return None
        normalized += " " * gap + value
        pointers.add(member["field_pointer"])
        cursor = part["end"]
        position += 1
        if member is not proof["members"][-1]:
            if (position >= len(parts) or parts[position].get("constructed") is not True
                    or parts[position]["start"] != cursor
                    or text[cursor:parts[position]["end"]] != "\n"):
                return None
            cursor = parts[position]["end"]
            position += 1
    if (len(normalized) != proof["normalized_text_length"]
            or text_sha256(normalized) != proof["normalized_text_sha256"]):
        return None
    return first["start"], cursor


def complete_html_prose_ranges(text, source, ranges):
    if not ranges:
        return ranges
    start, end = ranges[0][0], ranges[-1][1]
    spans = []
    for index, part in enumerate(source["parts"]):
        span = _complete_span(text, source["parts"], index)
        if span is not None and start <= span[0] < span[1] <= end:
            spans.append(span)
    if not spans:
        return ranges
    spans = sorted(set(spans))
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError("overlapping complete HTML source blocks")
    edges = {start, end, *(item[0] for item in ranges), *(item[1] for item in ranges)}
    for left, right in spans:
        edges = {edge for edge in edges if not left < edge < right}
        edges.update((left, right))
    ordered = sorted(edges)
    original = {(left, right): kind for left, right, kind in ranges}
    return [(left, right, original.get((left, right), "text_span"))
            for left, right in zip(ordered, ordered[1:])]

