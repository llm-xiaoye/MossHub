"""One actual evidence envelope for retrieval budgeting and generation."""
from __future__ import annotations


def bound_section_titles(artifacts, context_refs):
    """Copy only actual bound HEADING fields, without inferring author roles."""
    titles = []
    for ref in context_refs:
        if ref["purpose"] != "HEADING":
            continue
        field = artifacts.fields[(ref["processing_version_id"], ref["field_pointer"])]
        left, right = ref["cleaned_interval"]
        title = field.cleaned_text[left:right]
        if title and title not in titles:
            titles.append(title)
    return titles


def evidence_packet(*, request_id, kb_id, generation_id, removal_revision, groups):
    """Count primary document identities from only the groups actually sent.

    Counts do not include unsent candidates, alias sources or knowledge-base
    totals, and neither headings nor this inventory prove whole-document coverage.
    """
    documents = {group["document_id"] for group in groups}
    versions = {(group["document_id"], group["raw_version_id"],
                 group["processing_version_id"]) for group in groups}
    return {"schema": "raghub.evidence.v1", "request_id": request_id,
            "kb_id": kb_id, "generation_id": generation_id,
            "removal_revision": removal_revision,
            "source_scope": {"kind": "RETRIEVED_FRAGMENTS",
                             "document_count": len(documents),
                             "document_version_count": len(versions),
                             "whole_document_coverage": "NOT_VERIFIED"},
            "groups": groups}
