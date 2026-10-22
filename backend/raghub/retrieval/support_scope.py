"""Reconstruct citations only from the exact evidence sent to generation."""
from __future__ import annotations
from ..model_profiles import text_sha256

SUPPORT_SCOPE = "GROUP_BOUND_CONTEXT_PLUS_UNIT_BODY_V2"
COMPOSITE_FIELDS = {"support_scope", "support_parts", "support_text_sha256", "parent_text_length"}
SOURCE_IDENTITY = ("kb_id", "generation_id", "document_id", "raw_version_id",
                   "processing_version_id", "source_record_id")

def require(record, fields):
    if not isinstance(record, dict) or not set(fields).issubset(record):
        raise ValueError("incomplete citation contract")

def interval(value):
    if (type(value) is not list or len(value) != 2
            or any(type(x) is not int for x in value)
            or value[0] < 0 or value[1] < value[0]):
        raise ValueError("invalid support interval")
    return value

def support_texts(groups, whitelist, *, kb_id, generation_id):
    if type(groups) is not list or type(whitelist) is not dict:
        raise ValueError("invalid active evidence container")
    texts, evidence_ids = {}, set()
    for group in groups:
        require(group, ("evidence_id", "document_id", "raw_version_id",
                        "processing_version_id", "context_text", "support_units"))
        evidence_id = group["evidence_id"]
        context = group["context_text"]
        if (not isinstance(evidence_id, str) or not evidence_id or evidence_id in evidence_ids
                or type(context) is not str or type(group["support_units"]) is not list):
            raise ValueError("invalid or duplicate evidence group")
        evidence_ids.add(evidence_id)
        group_composite = "support_scope" in group
        if group_composite and group["support_scope"] != SUPPORT_SCOPE:
            raise ValueError("unknown support scope")
        for sent in group["support_units"]:
            require(sent, ("support_unit_id", "text"))
            unit_id, body = sent["support_unit_id"], sent["text"]
            if not isinstance(unit_id, str) or unit_id in texts or type(body) is not str or not body.strip():
                raise ValueError("invalid or duplicate support unit")
            if unit_id not in whitelist:
                raise ValueError("sent support missing from active whitelist")
            unit = whitelist[unit_id]
            require(unit, ("evidence_id", "support_unit_id", "parent_id", "parent_text_interval",
                           "text_sha256", "sources", *SOURCE_IDENTITY))
            if (unit["support_unit_id"] != unit_id or unit["evidence_id"] != evidence_id
                    or unit["kb_id"] != kb_id or unit["generation_id"] != generation_id
                    or any(unit[k] != group[k] for k in
                           ("document_id", "raw_version_id", "processing_version_id"))):
                raise ValueError("support identity crosses the sent snapshot")
            left, right = interval(unit["parent_text_interval"])
            if (left < len(context) or right-left != len(body)
                    or unit["text_sha256"] != text_sha256(body)):
                raise ValueError("sent body differs from the bound support range")
            if type(unit["sources"]) is not list or not unit["sources"]:
                raise ValueError("support has no original source")
            composite = bool(COMPOSITE_FIELDS.intersection(unit))
            if composite != group_composite:
                raise ValueError("partial composite citation contract")
            if composite:
                require(unit, COMPOSITE_FIELDS)
                if (unit["support_scope"] != SUPPORT_SCOPE or type(unit["parent_text_length"]) is not int
                        or right > unit["parent_text_length"]):
                    raise ValueError("invalid composite parent or scope")
                parts = []
                if context:
                    parts.append({"role":"CONTEXT", "parent_text_interval":[0,len(context)],
                                  "text_sha256":text_sha256(context)})
                parts.append({"role":"BODY", "parent_text_interval":[left,right],
                              "text_sha256":text_sha256(body)})
                support = context + body
                if (unit["support_parts"] != parts
                        or unit["support_text_sha256"] != text_sha256(support)):
                    raise ValueError("context or composite support differs from sent text")
            else:
                support = body
            body_found = False
            for source in unit["sources"]:
                require(source, SOURCE_IDENTITY + ("purpose",))
                if any(source[k] != unit[k] for k in SOURCE_IDENTITY):
                    raise ValueError("original source crosses the support snapshot")
                if composite:
                    require(source, ("support_role", "parent_text_interval"))
                    role = source["support_role"]
                    a, b = interval(source["parent_text_interval"])
                    bounds = [0,len(context)] if role == "CONTEXT" and context else [left,right] if role == "BODY" else None
                    if bounds is None or a < bounds[0] or b > bounds[1]:
                        raise ValueError("source position does not belong to its support role")
                elif "support_role" in source:
                    raise ValueError("legacy source has partial composite metadata")
                else:
                    role = "BODY"
                if role == "BODY" and source["purpose"] in {"BODY", "MERGE_CONTEXT"}:
                    body_found = True
            if not body_found:
                raise ValueError("context alone cannot support a fact")
            texts[unit_id] = support
    if set(texts) != set(whitelist):
        raise ValueError("active whitelist contains an unsent support unit")
    return texts

