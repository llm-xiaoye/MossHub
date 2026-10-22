"""Budget the complete serialized evidence and bind every selectable source unit."""
from __future__ import annotations

import re

from ..model_profiles import canonical, text_sha256
from .support_scope import SUPPORT_SCOPE
from .packet import bound_section_titles, evidence_packet


def parent_requires_complete_context(parent):
    """Share the same non-table full-parent contract with seed selection."""
    return type(parent.get("content_kind")) is str and parent["content_kind"] != "table"


def required_parent_coverage(artifacts, item):
    """Prove a candidate window before reusing its required parent seed slot.

    Missing capabilities do not prove coverage. A malformed available mapping
    fails through child_window; it is never silently treated as a duplicate.
    """
    parent = artifacts.parents[item["parent_id"]]
    if not parent_requires_complete_context(parent):
        return None
    children, sources = getattr(artifacts, "children", None), getattr(artifacts, "sources", None)
    if type(children) is not dict or type(sources) is not dict:
        return None
    child = children.get(item["chunk_id"])
    if type(child) is not dict:
        return None
    parent_source = sources.get(parent.get("source_record_id"))
    child_source = sources.get(child.get("source_record_id"))
    if type(parent_source) is not dict or type(child_source) is not dict:
        return None
    left, right = child_window(parent, parent_source, child, child_source)
    body_start = len(parent["text"]) - len(parent["body"])
    if not body_start <= left < right <= len(parent["text"]):
        raise ValueError("candidate is outside the required complete parent body")
    return {"candidate_window": [left, right],
            "required_parent_body_interval": [body_start, len(parent["text"])]}


def body_ranges(parent, source, body_start, start, end):
    text = parent["text"]
    if parent["content_kind"] == "table":
        rows = [part["start"] for part in source["parts"] if part["constructed"]
                and part["start"] >= body_start
                and re.fullmatch(r"第[1-9][0-9]*行：", text[part["start"]:part["end"]])]
        if not rows:
            body_parts=[part for part in source["parts"] if part["end"]>body_start]
            original=[part for part in body_parts if not part["constructed"]]
            header_only=(any(part["constructed"] and part["start"]==body_start
                and text[part["start"]:part["end"]]=="表格原文（无数据行）：" for part in body_parts)
                and original and all(part["purpose"] in {"HEADER","TABLE_LEADING_CONTEXT"} for part in original))
            if header_only:
                if start!=body_start or end!=len(text):
                    raise ValueError("header-only table evidence must retain its complete source context")
                return [(start,end,"text_span")]
        if not rows or rows[0] != body_start:
            raise ValueError("table body has no bound complete-row boundaries")
        stops = rows[1:] + [len(text)]
        content_stops = []
        for stop in stops:
            # Joined complete rows omit the following constructed delimiter in
            # a child. An original field newline is never treated as a delimiter.
            separators = [part for part in source["parts"] if part["constructed"]
                          and part["end"] == stop and text[part["start"]:stop] == "\n"]
            if len(separators) > 1:
                raise ValueError("table row delimiter is not uniquely source-bound")
            content_stops.append(separators[0]["start"] if separators else stop)
        if start not in rows or end not in stops + content_stops:
            raise ValueError("child table evidence would cut a complete source row")
        return [(left, min(right, end), "table_row")
                for left, right, content_stop in zip(rows, stops, content_stops, strict=True)
                if start <= left and content_stop <= end]
    if parent["content_kind"] in {"code", "formula"}:
        return [(start, end, parent["content_kind"])]
    # Locate protected tokens in the full parent, including tokens that cross
    # a child edge. Ambiguous URL punctuation stays in the original text span.
    protected = [(match.start(), match.end()) for match in re.finditer(
        r"(?:https?://|www\.)[^\s<>\"“”‘’。！？]+"
        r"|[A-Z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?\.[A-Z]{2,}"
        r"|\b[vV]?\d+(?:\.\d+)+\b"
        r"|\b(?:[A-Z]\.){2,}"
        r"|\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|etc|vs|No|Fig)\.",
        text, re.IGNORECASE)]
    # Preserve every source character and its half-open interval. A suffix at
    # a chunk edge or after a protected abbreviation remains a text_span.
    value, ranges, cursor = text[start:end], [], start
    for match in re.finditer(r"[。！？!?]+[”’\"）)]*|\.(?=\s|$)|\n+", value):
        if not match.group().startswith("\n") and any(
                left <= start + match.start() < right for left, right in protected):
            continue
        stop = start + match.end()
        if stop > cursor:
            ranges.append((cursor, stop, "sentence" if not match.group().startswith("\n") else "text_span"))
        cursor = stop
    if cursor < end:
        ranges.append((cursor, end, "text_span"))
    from .html_scope import complete_html_prose_ranges
    ranges = complete_html_prose_ranges(text, source, ranges)
    return complete_markdown_prose_ranges(text, source, ranges)


def markdown_inline_source_closure(text, source, start, end):
    """Recognize validated inline fragments from one original MD line.

    Punctuation-only exact-bounded fields keep their original REGION locator;
    their weaker basis is never applied to words or upgraded in references.
    """
    parts = [part for part in source["parts"]
             if part["end"] > start and part["start"] < end]
    if (not parts or parts[0].get("constructed") is not False
            or parts[0]["start"] != start):
        return False
    common = parts[0].get("locator")
    if (type(common) is not dict or common.get("kind") != "md"
            or common.get("granularity") not in {"EXACT", "REGION"}
            or common.get("basis") != "T12_EXACT_UNIQUE"
            or type(common.get("line_start")) is not int
            or type(common.get("line_end")) is not int
            or not 0 < common["line_start"] == common["line_end"]
            or type(common.get("source_start")) is not int
            or type(common.get("source_end")) is not int
            or not 0 <= common["source_start"] < common["source_end"]
            or type(common.get("file_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", common["file_sha256"]) is None):
        return False
    cursor = start
    for part in parts:
        stop = min(end, part["end"])
        if part["start"] != cursor or stop <= cursor:
            return False
        if part.get("constructed") is True:
            if stop != part["end"] or text[cursor:stop] != "\n":
                return False
        elif part.get("constructed") is False:
            locator = part.get("locator")
            if part.get("purpose") != "BODY" or type(locator) is not dict:
                return False
            field_text = text[part["start"]:part["end"]]
            if "\n" in field_text or "\r" in field_text:
                return False
            if locator != common:
                punctuation = text[part["start"]:part["end"]]
                normalized = dict(locator)
                normalized["basis"] = common["basis"]
                if (locator.get("basis") != "T12_EXACT_UNIQUE_BOUNDED"
                        or normalized != common
                        or re.fullmatch(r"[。！？!?]+[”’\"）)]*|\.", punctuation) is None):
                    return False
        else:
            return False
        cursor = stop
    return cursor == end


def complete_markdown_prose_ranges(text, source, ranges):
    """Keep available prose from one proven original MD block together.

    A single-line EXACT or REGION locator and source bounds must agree for every
    original field. Sentence punctuation within that block is not a new
    applicability boundary. Real newlines, other source regions, gaps and
    child edges never import unavailable text. References retain their basis.
    """
    result = []
    for current in ranges:
        previous = result[-1] if result else None
        if (previous is not None and previous[1] == current[0]
                and text[previous[0]:previous[1]].strip()
                and markdown_inline_source_closure(text, source, previous[0], current[1])):
            result[-1] = (previous[0], current[1], "text_span")
        else:
            result.append(current)
    return result

def source_references(artifacts, source, text, start, end, document):
    refs = []
    for part in source["parts"]:
        if part["constructed"]:
            continue
        left, right = max(start, part["start"]), min(end, part["end"])
        empty = part["start"] == part["end"] and start <= part["start"] < end
        if left >= right and not empty:
            continue
        field = artifacts.fields[(document["processing_version_id"], part["field_pointer"])]
        clean_start = part["cleaned_start"] + left-part["start"]
        clean_end = part["cleaned_start"] + right-part["start"]
        if (field.cleaned_text[clean_start:clean_end] != text[left:right]
                or part["locator"] != field.locator.model_dump(mode="json")):
            raise ValueError("support unit does not match its actual cleaned field/location")
        traces, raw_intervals = [], []
        for mapping_index, mapping in enumerate(field.mappings):
            begin, stop = max(clean_start, mapping.output_start), min(clean_end, mapping.output_end)
            if begin >= stop:
                continue
            unchanged = (
                mapping.input_intervals == [[mapping.input_start, mapping.input_end]]
                and mapping.input_end-mapping.input_start == mapping.output_end-mapping.output_start
                and field.original_text[mapping.input_start:mapping.input_end]
                == field.cleaned_text[mapping.output_start:mapping.output_end])
            intervals = ([[mapping.input_start + begin-mapping.output_start,
                           mapping.input_start + stop-mapping.output_start]]
                         if unchanged else mapping.input_intervals)
            traces.append({"mapping_index": mapping_index, "cleaned_intersection": [begin, stop],
                           "raw_intervals": intervals, "operation": mapping.operation,
                           "precision": "EXACT_UNCHANGED_INTERVAL" if unchanged else mapping.precision})
            raw_intervals.extend(intervals)
        if clean_start < clean_end and not traces:
            raise ValueError("nonempty support unit has no source interval mapping")
        refs.append({"kb_id": artifacts.snapshot["kb_id"], "generation_id": artifacts.snapshot["generation_id"],
                     "document_id": document["document_id"], "raw_version_id": document["raw_version_id"],
                     "processing_version_id": document["processing_version_id"],
                     "source_record_id": source["source_record_id"], "field_pointer": part["field_pointer"],
                     "purpose": part["purpose"], "cleaned_interval": [clean_start, clean_end],
                     "parent_text_interval": [left, right],
                     "coordinate_units": "FIELD_PYTHON_UNICODE_CODE_POINT",
                     "interval_semantics": "ZERO_BASED_HALF_OPEN",
                     "locator": part["locator"], "raw_intervals": raw_intervals, "mappings": traces,
                     "empty_source_field": empty and not field.cleaned_text})
    return refs


def proven_empty_table_row(artifacts, item):
    """Skip only table placeholders whose referenced source cells are truly empty."""
    if item["content_kind"] != "table":
        return None
    children, sources, fields = (getattr(artifacts, name, None)
                                  for name in ("children", "sources", "fields"))
    if not all(isinstance(value, dict) for value in (children, sources, fields)):
        return None
    child = children[item["chunk_id"]]
    source = sources[child["source_record_id"]]
    real = [part for part in source["parts"] if not part["constructed"]]
    if not real or any(part["end"] > part["start"] for part in real):
        return None
    if any(part["start"] != part["end"] or part["cleaned_start"] != 0
           or part["cleaned_end"] != 0 for part in real):
        return None
    refs = source_references(artifacts, source, child["text"], 0, len(child["text"]), item)
    if len(refs) != len(real) or not all(ref["empty_source_field"] for ref in refs):
        return None
    return {"policy": "EMPTY_TABLE_ROW_SOURCE_VERIFIED_V1",
            "source_record_id": child["source_record_id"], "empty_cell_count": len(refs)}


def child_window(parent, parent_source, child, child_source):
    prefix = parent["text"][:-len(parent["body"])]
    if not child["text"].startswith(prefix):
        raise ValueError("seed necessary context differs from its parent")
    begin = len(prefix)
    if len(child["text"]) <= begin:
        raise ValueError("seed child has no body")
    real = [p for p in child_source["parts"] if not p["constructed"] and p["start"] >= begin and p["end"] > p["start"]]
    if not real:
        raise ValueError("seed child has no bound source body")
    first, offsets = real[0], set()
    for part in parent_source["parts"]:
        if (not part["constructed"] and part["field_pointer"] == first["field_pointer"]
                and part["purpose"] == first["purpose"]
                and part["cleaned_start"] <= first["cleaned_start"] < part["cleaned_end"]):
            offsets.add(part["start"] + first["cleaned_start"]-part["cleaned_start"]-(first["start"]-begin))
    verified = []
    for offset in offsets:
        stop = offset + len(child["text"])-begin
        if not begin <= offset < stop <= len(parent["text"]) or parent["text"][offset:stop] != child["text"][begin:]:
            continue
        matches = True
        for piece in real:
            at = offset + piece["start"]-begin
            candidates = [p for p in parent_source["parts"] if not p["constructed"]
                          and p["field_pointer"] == piece["field_pointer"] and p["purpose"] == piece["purpose"]
                          and p["locator"] == piece["locator"]
                          and p["start"] <= at and at+piece["end"]-piece["start"] <= p["end"]
                          and p["cleaned_start"]+at-p["start"] == piece["cleaned_start"]]
            if len(candidates) != 1:
                matches = False
                break
        if matches:
            verified.append((offset, stop))
    if len(verified) != 1:
        raise ValueError("seed child cannot be mapped uniquely to reliable parent/source intervals")
    return verified[0]


def merge_windows(windows):
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


class EvidenceAssembler:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def assemble(self, request_id, artifacts, seeds, groups):
        decisions, mappings = [], {}
        seed_by_id = {v["chunk_id"]: v for v in seeds}

        def serialize(values):
            return canonical(evidence_packet(
                request_id=request_id, kb_id=artifacts.snapshot["kb_id"],
                generation_id=artifacts.snapshot["generation_id"],
                removal_revision=artifacts.snapshot["removal_snapshot"]["revision"],
                groups=values)).decode("utf-8")

        def prepare(ordinal, group):
            parent = artifacts.parents[group["parent_id"]]
            source = artifacts.sources[parent["source_record_id"]]
            first_seed = seed_by_id[group["selected_seed_ids"][0]]
            document = artifacts.document_by_child[first_seed["chunk_id"]]
            if document["document_id"] in artifacts.snapshot["removal_snapshot"]["removed_document_ids"]:
                raise ValueError("removed document reached final evidence")
            body_start = len(parent["text"])-len(parent["body"])
            evidence_id = f"R{request_id}-E{ordinal:03d}"
            context = parent["text"][:body_start]
            context_refs = source_references(artifacts, source, parent["text"], 0, body_start, document)
            candidate_mappings = {}
            # Opaque IDs are stable across expansion candidates. Source offsets
            # remain separate bindings; the model must never invent an interval.
            unit_ids = {}

            def candidate(windows, mode, child_ids):
                units = []
                for start, end in merge_windows(windows):
                    for left, right, kind in body_ranges(parent, source, body_start, start, end):
                        text = parent["text"][left:right]
                        if not text.strip():
                            continue
                        refs = source_references(artifacts, source, parent["text"], left, right, document)
                        if not any(ref["purpose"] in {"BODY", "MERGE_CONTEXT"} for ref in refs):
                            continue  # Titles/headers alone never receive a support ID.
                        parts = []
                        if context:
                            parts.append({"role":"CONTEXT", "parent_text_interval":[0,body_start],
                                          "text_sha256":text_sha256(context)})
                        parts.append({"role":"BODY", "parent_text_interval":[left,right],
                                      "text_sha256":text_sha256(text)})
                        combined_refs = ([{**ref, "support_role":"CONTEXT"} for ref in context_refs]
                                         + [{**ref, "support_role":"BODY"} for ref in refs])
                        unit_key = (left, right)
                        if unit_key not in unit_ids:
                            unit_ids[unit_key] = f"{evidence_id}-U{len(unit_ids) + 1:03d}"
                        unit_id = unit_ids[unit_key]
                        item = {"support_unit_id": unit_id, "kind": kind, "text": text}
                        units.append(item)
                        candidate_mappings[unit_id] = {
                            "evidence_id": evidence_id, "support_unit_id": unit_id,
                            "parent_id": parent["parent_id"], "parent_text_interval": [left, right],
                            "text_sha256": text_sha256(text), "source_record_id": parent["source_record_id"],
                            "document_id": document["document_id"], "raw_version_id": document["raw_version_id"],
                            "processing_version_id": document["processing_version_id"],
                            "kb_id": artifacts.snapshot["kb_id"], "generation_id": artifacts.snapshot["generation_id"],
                            "support_scope": SUPPORT_SCOPE, "support_parts":parts,
                            "parent_text_length":len(parent["text"]),
                            "support_text_sha256":text_sha256(context + text),
                            "sources": combined_refs}
                return {"evidence_id": evidence_id, "document_id": document["document_id"],
                        "raw_version_id": document["raw_version_id"], "processing_version_id": document["processing_version_id"],
                        "content_kind": parent["content_kind"], "context_text": context,
                        "section_titles": bound_section_titles(artifacts, context_refs),
                        "support_scope": SUPPORT_SCOPE,
                        "support_units": units}, {"mode": mode, "parent_id": parent["parent_id"],
                        "selected_child_ids": child_ids, "windows": merge_windows(windows),
                        "all_hit_child_ids": group["all_hit_child_ids"], "context_sources": context_refs}


            full, metadata = candidate([(body_start, len(parent["text"]))],
                                       "FULL_PARENT", group["selected_seed_ids"])
            windows, seed_candidates = {}, {}
            for child_id in group["selected_seed_ids"]:
                child = artifacts.children[child_id]
                windows[child_id] = child_window(
                    parent, source, child, artifacts.sources[child["source_record_id"]])
                # Arbitrary prose/code has no reliable standalone applicability
                # delimiter. Keep its entire bound parent as necessary context.
                required = ([(body_start, len(parent["text"]))] if parent_requires_complete_context(parent)
                            else [windows[child_id]])
                mode = "REQUIRED_PARENT_CONTEXT" if parent_requires_complete_context(parent) else "SEED_CHILDREN"
                seed_candidates[child_id] = candidate(required, mode, [child_id])
            # Verify the actual support spans and their field provenance, not
            # merely nonempty full-parent and seed candidates.
            full_ranges = merge_windows([candidate_mappings[unit["support_unit_id"]]["parent_text_interval"]
                                         for unit in full["support_units"]])
            full_refs = [ref for unit in full["support_units"]
                         for ref in candidate_mappings[unit["support_unit_id"]]["sources"]]
            source_keys = ("kb_id", "generation_id", "document_id", "raw_version_id",
                           "processing_version_id", "source_record_id", "field_pointer", "purpose", "locator")
            for seed_value, _ in seed_candidates.values():
                for unit in seed_value["support_units"]:
                    binding = candidate_mappings[unit["support_unit_id"]]
                    start, end = binding["parent_text_interval"]
                    if not any(left <= start and end <= right for left, right in full_ranges):
                        raise ValueError("full parent does not cover a valid seed support span")
                    for seed_ref in binding["sources"]:
                        matching = [ref["cleaned_interval"] for ref in full_refs
                                    if ref["support_role"] == seed_ref["support_role"]
                                    and all(ref[key] == seed_ref[key] for key in source_keys)]
                        left, right = seed_ref["cleaned_interval"]
                        if not any(begin <= left and right <= stop for begin, stop in merge_windows(matching)):
                            raise ValueError("full parent does not cover a valid seed source interval")
            return {"ordinal": ordinal, "group": group, "evidence_id": evidence_id,
                    "candidate": candidate, "mappings": candidate_mappings, "windows": windows,
                    "seed_candidates": seed_candidates,
                    "has_seed_evidence": any(value[0]["support_units"] for value in seed_candidates.values()),
                    "full": full, "full_metadata": metadata, "selected": None, "metadata": None,
                    "trials": [], "full_parent_token_cost": self.tokenizer.count(serialize([full]))}

        states = [prepare(ordinal, group) for ordinal, group in enumerate(groups, 1)]

        def ordered_values(replacement=None):
            # Trial allocation may visit short parents first; serialization always
            # preserves parent relevance order and each candidate's source order.
            replacement = replacement or {}
            return [value for state in states
                    if (value := replacement.get(state["ordinal"], state["selected"])) is not None]

        def try_candidate(state, value, metadata, stage, child_id=None):
            trial = serialize(ordered_values({state["ordinal"]: value}))
            count = self.tokenizer.count(trial)
            if not state["has_seed_evidence"]:
                rejection_reason = "NO_VALID_SEED_SUPPORT_UNITS"
            elif not value["support_units"]:
                rejection_reason = "NO_CANDIDATE_SUPPORT_UNITS"
            elif count > 8000:
                rejection_reason = "BUDGET_EXCEEDED"
            elif stage == "COMPLETE_PARENT" and count > 4000:
                rejection_reason = "OPTIONAL_SOFT_TARGET_EXCEEDED"
            else:
                rejection_reason = None
            accepted = rejection_reason is None
            observation = {"stage": stage, "mode": metadata["mode"],
                           "token_count": count, "adopted": accepted,
                           "rejection_reason": rejection_reason}
            if child_id is not None:
                observation["child_id"] = child_id
            state["trials"].append(observation)
            if accepted:
                state["selected"], state["metadata"] = value, metadata
            return accepted

        # First reserve one fitting complete hit and its existing necessary context
        # for each selected parent, visiting parents and seeds in relevance order.
        for state in states:
            for child_id in state["group"]["selected_seed_ids"]:
                value, metadata = state["seed_candidates"][child_id]
                if try_candidate(state, value, metadata, "RESERVE_SEED", child_id):
                    break

        # Only remaining selected hit seeds may expand a fallback group. A full
        # parent already covers its body, so its seeds are never duplicated.
        for state in states:
            if state["metadata"] is not None and state["metadata"]["mode"] in {"FULL_PARENT", "REQUIRED_PARENT_CONTEXT"}:
                continue
            for child_id in state["group"]["selected_seed_ids"]:
                child_ids = [] if state["metadata"] is None else state["metadata"]["selected_child_ids"]
                if child_id in child_ids:
                    continue
                seed_value, seed_metadata = state["seed_candidates"][child_id]
                if not seed_value["support_units"]:
                    try_candidate(state, seed_value, seed_metadata, "EXPAND_SEEDS", child_id)
                    continue
                expanded_ids = child_ids + [child_id]
                value, metadata = state["candidate"](
                    [state["windows"][key] for key in expanded_ids], "SEED_CHILDREN", expanded_ids)
                try_candidate(state, value, metadata, "EXPAND_SEEDS", child_id)

        # Mandatory hits/conditions may use the hard budget. Only optional parent
        # completion stops at the soft target; no support unit is text-truncated.
        necessary_tokens = self.tokenizer.count(serialize(ordered_values()))
        for state in sorted(states, key=lambda item: (item["full_parent_token_cost"], item["ordinal"])):
            if state["metadata"] is not None and state["metadata"]["mode"] in {"FULL_PARENT", "REQUIRED_PARENT_CONTEXT"}:
                continue
            try_candidate(state, state["full"], state["full_metadata"], "COMPLETE_PARENT")

        selected = ordered_values()
        for state in states:
            adopted = state["selected"]
            if adopted is not None:
                for unit in adopted["support_units"]:
                    mappings[unit["support_unit_id"]] = state["mappings"][unit["support_unit_id"]]
                decisions.append({"evidence_id": state["evidence_id"], "decision": "INCLUDED",
                                  **state["metadata"], "trials": state["trials"],
                                  "full_parent_token_cost": state["full_parent_token_cost"]})
            else:
                if not state["has_seed_evidence"]:
                    exclusion_reason = "NO_VALID_SEED_SUPPORT_UNITS"
                else:
                    valid_seed_ids = {key for key, (value, _) in state["seed_candidates"].items()
                                      if value["support_units"]}
                    seed_trials = [trial for trial in state["trials"]
                                   if trial["stage"] == "RESERVE_SEED" and trial["child_id"] in valid_seed_ids]
                    if not seed_trials or any(trial["rejection_reason"] != "BUDGET_EXCEEDED" for trial in seed_trials):
                        raise ValueError("excluded valid seeds do not have actual budget rejection evidence")
                    exclusion_reason = "BUDGET_EXCEEDED"
                decisions.append({"evidence_id": state["evidence_id"], "parent_id": state["group"]["parent_id"],
                                  "decision": "BUDGET_EXCLUDED" if exclusion_reason == "BUDGET_EXCEEDED" else "NO_SUPPORT_UNITS",
                                  "exclusion_reason": exclusion_reason, "trials": state["trials"],
                                  "full_parent_token_cost": state["full_parent_token_cost"]})
        serialized = serialize(selected)
        final_tokens = self.tokenizer.count(serialized)
        if final_tokens > 8000 or len(mappings) != sum(len(g["support_units"]) for g in selected):
            raise ValueError("frozen evidence budget/whitelist differs")
        return {"serialized_evidence": serialized, "serialized_sha256": text_sha256(serialized),
                "evidence_token_count": final_tokens, "evidence_budget": 8000,
                "assembly_policy": "HITS_THEN_REQUIRED_CONTEXT_THEN_OPTIONAL_PARENT_V2",
                "optional_soft_target": 4000, "necessary_evidence_tokens": necessary_tokens,
                "required_context_policy": "TABLE_BOUND_PREFIX_AND_COMPLETE_ROWS;OTHER_COMPLETE_PARENT",
                "support_unit_policy": "MD_BOUND_SINGLE_LINE_SOURCE_BLOCK_V4",
                "tokenizer_identity": self.tokenizer.identity, "groups": selected,
                "support_unit_whitelist": mappings, "assembly_decisions": decisions,
                "seed_count": len(seeds), "unique_parent_count": len(groups),
                "included_group_count": len(selected),
                "source_document_count": len({g["document_id"] for g in selected}),
                "no_evidence": not selected, "answer_generated": False}
