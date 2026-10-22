"""Deterministic weighted fusion, raw-logit ordering and conservative seed selection."""
from __future__ import annotations

import math


def fuse(artifacts, routes, query_count):
    expected = {(f"Q{i+1}", route) for i in range(query_count) for route in ("dense", "bm25")}
    if len(routes) != len(expected) or {(r["query_id"], r["route"]) for r in routes} != expected:
        raise ValueError("required recall route set is incomplete")
    if any(route["status"] != "SUCCEEDED" for route in routes):
        raise ValueError("a required recall route failed; partial routes cannot be adopted")
    weight = 0.5 / query_count
    candidates = {}
    for route in routes:
        ordered = sorted(route["hits"], key=lambda item: (-item["distance"], item["id"]))
        trace = []
        for rank, hit in enumerate(ordered, 1):
            child = artifacts.check_hit(hit)
            score = hit["distance"]
            if route["route"] == "dense":
                if not math.isfinite(score) or not -1 <= score <= 1:
                    raise ValueError("COSINE score is outside [-1,1]")
                normalized = (score+1)/2
            else:
                if not math.isfinite(score) or score < 0:
                    raise ValueError("BM25 score must be nonnegative and finite")
                normalized = 2/math.pi * math.atan(score)
            contribution = weight*normalized
            detail = {"query_id": route["query_id"], "route": route["route"], "route_rank": rank,
                      "raw_score": score, "normalized_score": normalized, "weight": weight,
                      "weighted_contribution": contribution}
            trace.append({"chunk_id": child["chunk_id"], **detail})
            cid = child["chunk_id"]
            if cid not in candidates:
                document = artifacts.document_by_child[cid]
                candidates[cid] = {**child, "document_id": document["document_id"], "fusion_score": 0.0,
                                   "best_route_rank": rank, "route_hits": []}
            item = candidates[cid]
            item["fusion_score"] += contribution
            item["best_route_rank"] = min(item["best_route_rank"], rank)
            item["route_hits"].append(detail)
        route["fusion_contributions"] = trace
    ordered = sorted(candidates.values(), key=lambda item: (-item["fusion_score"],
                     item["best_route_rank"], item["chunk_id"]))
    for rank, item in enumerate(ordered, 1):
        item["fusion_rank"] = rank
    return ordered


def rerank_candidates(candidates):
    """Keep both recall routes represented before the fixed forty-item rerank."""
    routes={}
    for item in candidates:
        for hit in item.get("route_hits",[]):
            key=(hit["query_id"],hit["route"])
            routes.setdefault(key,[]).append((hit["route_rank"],item["fusion_rank"],item["chunk_id"]))
    quota=min(10,20//len(routes)) if routes else 0
    protected=set()
    for hits in routes.values():
        protected.update(cid for _rank,_fusion,cid in sorted(hits)[:quota])
    selected=set(protected)
    for item in candidates:
        if len(selected)>=40:break
        selected.add(item["chunk_id"])
    return [item for item in candidates if item["chunk_id"] in selected]

def order_reranked(candidates, batches):
    selected = rerank_candidates(candidates)
    scores = {}
    for batch in batches:
        for item in batch["items"]:
            if item["item_id"] in scores:
                raise ValueError("duplicate reranker output")
            scores[item["item_id"]] = item["score"]
    if set(scores) != {item["chunk_id"] for item in selected}:
        raise ValueError("reranker did not return all selected candidates")
    ordered = sorted(({**item, "reranker_raw_logit": scores[item["chunk_id"]]} for item in selected),
                     key=lambda item: (-item["reranker_raw_logit"], item["fusion_rank"], item["chunk_id"]))
    for rank, item in enumerate(ordered, 1):
        item["rerank_rank"] = rank
    return ordered


RERANK_TEXT_POLICY = "BOUND_HEADING_AFTER_BODY_V2"


def rerank_passage(text, source_parts):
    """Move a proven first-line heading after the body for reranking only.

    Evidence/source bytes are unmodified. Ranking and seed membership may change.
    Source parts are taken from the already verified retrieval artifacts.
    Multiline or CRLF heading layouts are conservatively left unchanged.
    """
    if not isinstance(text, str):
        raise ValueError("rerank passage must be text")
    if not text.startswith("标题：") or "\n" not in text:
        return text
    heading, body = text.split("\n", 1)
    if not body or heading.endswith("\r") or not source_parts:
        return text
    heading_end = len(heading)
    prefix_parts = [part for part in source_parts if part["start"] < heading_end]
    bound = [part for part in prefix_parts if not part["constructed"]]
    if not bound or any(part["purpose"] != "HEADING" or part["end"] > heading_end
                        for part in bound):
        return text
    return body + "\n" + heading


def rerank_batches(candidates):
    """Use stored tokenizer lengths as a padding proxy; keep the selected ID set."""
    selected = rerank_candidates(candidates)
    if len({item["chunk_id"] for item in selected}) != len(selected):
        raise ValueError("duplicate rerank candidate")
    ordered = sorted(selected, key=lambda item: (item["token_count"], item["fusion_rank"], item["chunk_id"]))
    return [ordered[start:start+4] for start in range(0, len(ordered), 4)]


SEED_SELECTION_POLICY = {
    "strategy": "FUSION_PROTECTED_QUERY_SECTION_THEN_RERANK_FILL",
    "section_focus_limit": 2,
    "fusion_protected_limit": 4,
    "seed_limit": 8,
    "candidate_scope": "RERANKED_VALID_CANDIDATES",
    "output_order": "ORIGINAL_RERANK_ORDER",
    "required_parent_seed_reuse": "ONE_SEED_PER_PROVEN_REQUIRED_COMPLETE_PARENT_V1",
}


def query_section_match(artifacts, item, query):
    """Use a bound leaf heading as a lexical coverage hint, never a truth score."""
    import re
    import unicodedata
    if not isinstance(query, str) or not query.strip():
        return None
    parent = artifacts.parents[item["parent_id"]]
    scope = parent.get("heading_scope")
    fields = getattr(artifacts, "fields", {})
    if not isinstance(scope, list) or len(scope) < 2:
        return None
    source = getattr(artifacts, "sources", {}).get(parent.get("source_record_id"))
    if not isinstance(source, dict) or not isinstance(source.get("parts"), list):
        return None
    headings = [part for part in source["parts"] if part.get("constructed") is False
                and part.get("purpose") == "HEADING"]
    if len(headings) < 2:
        return None
    leaf = headings[-1]
    pointer = leaf.get("field_pointer")
    field = fields.get((item["processing_version_id"], pointer))
    left, right = leaf.get("cleaned_start"), leaf.get("cleaned_end")
    if (field is None or not isinstance(field.cleaned_text, str)
            or type(left) is not int or type(right) is not int
            or not 0 <= left < right <= len(field.cleaned_text)):
        return None
    title = field.cleaned_text[left:right].strip()
    heading = unicodedata.normalize("NFKC", title).casefold()
    question = unicodedata.normalize("NFKC", query).casefold()
    if not heading:
        return None
    generic_headings = {"核心要点", "模型补充", "模型判断", "补充知识", "自测", "我的补充"}
    if heading in generic_headings and heading not in question:
        return None
    ignored = {"什么", "哪些", "如何", "怎么", "是否", "几个", "几步", "几部",
               "部分", "内容", "介绍", "相关", "说明", "模型", "笔记", "资料",
               "总结", "要点", "正文", "补充", "判断"}
    matches = set()
    query_words = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", question))
    matches.update(word for word in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", heading)
                   if word in query_words)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", heading):
        for width in range(min(6, len(run)), 1, -1):
            found = {run[left:left+width] for left in range(len(run)-width+1)
                     if run[left:left+width] not in ignored and run[left:left+width] in question}
            if found:
                matches.update(found)
                break
    if not matches:
        return None
    return {"policy": "BOUND_LEAF_HEADING_LITERAL_OVERLAP_V1", "heading": title,
            "field_pointer": pointer, "processing_version_id": item["processing_version_id"],
            "matched_terms": sorted(matches), "score": max(len(term) for term in matches),
            "semantic_relevance_proven": False}


def select_seeds(artifacts, ranked, query=""):
    from .artifacts import EQUIVALENCE_POLICY, equivalence_text
    from .evidence import proven_empty_table_row, required_parent_coverage
    seeds, decisions_by_id = [], {}
    rerank_order = sorted(ranked, key=lambda item: (item["rerank_rank"], item["chunk_id"]))

    def consider(item, origin):
        cid = item["chunk_id"]
        if cid in decisions_by_id:
            return  # Preserve a selected origin or an already-proven alias.
        parent = artifacts.parents[item["parent_id"]]
        ranks = {"fusion_rank": item["fusion_rank"], "rerank_rank": item["rerank_rank"]}
        empty_row = proven_empty_table_row(artifacts, item)
        if empty_row is not None:
            decisions_by_id[cid] = {"chunk_id": cid, "decision": "PROVEN_EMPTY_TABLE_ROW_SKIPPED",
                                    "considered_by": origin, "proof": empty_row,
                                    "sent_as_support_unit": False, **ranks}
            return
        for selected in seeds:
            identities = ("document_id", "raw_version_id", "processing_version_id")
            if (item["parent_id"] == selected["parent_id"] and all(
                    type(item.get(key)) is str and item[key] == selected.get(key) for key in identities)):
                coverage = required_parent_coverage(artifacts, item)
                if coverage is not None:
                    decisions_by_id[cid] = {
                        "chunk_id": cid, "decision": "REQUIRED_PARENT_ALREADY_COVERED",
                        "covered_by_seed": selected["chunk_id"], "parent_id": item["parent_id"],
                        "considered_by": origin, "coverage": coverage,
                        "coverage_scope": "PLANNED_REQUIRED_PARENT;FINAL_ASSEMBLY_MAY_EXCLUDE_FOR_BUDGET",
                        "content_equivalence_claim": False, **ranks}
                    return
        equivalent = None
        for selected in seeds:
            other = artifacts.parents[selected["parent_id"]]
            contexts = getattr(artifacts, "equivalence_contexts", {})
            context, other_context = contexts.get(item["parent_id"]), contexts.get(selected["parent_id"])
            same_processing = (item["processing_version_id"] == selected["processing_version_id"]
                               and item["equivalence_key"] == selected["equivalence_key"])
            complete_context_equal = (context is not None and other_context is not None
                                      and context["sha256"] == other_context["sha256"]
                                      and context["serialized"] == other_context["serialized"])
            if ((same_processing or complete_context_equal)
                    and equivalence_text(item["text"]) == equivalence_text(selected["text"])
                    and item["content_kind"] == selected["content_kind"]
                    and equivalence_text(parent["text"]) == equivalence_text(other["text"])
                    and parent["heading_scope"] == other["heading_scope"]):
                equivalent = selected["chunk_id"]
                break
        if equivalent is not None:
            decisions_by_id[cid] = {"chunk_id": cid, "decision": "PROVEN_EQUIVALENT_SKIPPED",
                                    "equivalent_to": equivalent, "equivalence_key": item["equivalence_key"],
                                    "considered_by": origin, "equivalence_policy": EQUIVALENCE_POLICY,
                                    "source": {key: item[key] for key in ("document_id", "raw_version_id", "processing_version_id", "parent_id")},
                                    "sent_as_support_unit": False, **ranks}
        elif len(seeds) < SEED_SELECTION_POLICY["seed_limit"]:
            seeds.append({**item, "selection_origin": origin})
            decisions_by_id[cid] = {"chunk_id": cid, "decision": "SELECTED_SEED",
                                    "selection_origin": origin, **ranks}
        else:
            decisions_by_id[cid] = {"chunk_id": cid, "decision": "SEED_LIMIT",
                                    "considered_by": origin, **ranks}

    for item in sorted(rerank_order, key=lambda value: (value["fusion_rank"],
                                                       value["rerank_rank"], value["chunk_id"])):
        if len(seeds) >= SEED_SELECTION_POLICY["fusion_protected_limit"]:
            break
        consider(item, "FUSION_PROTECTED")
    focused = []
    for item in rerank_order:
        match = query_section_match(artifacts, item, query)
        if match is not None:
            focused.append((item, match))
    focused.sort(key=lambda pair: (-pair[1]["score"], pair[0]["fusion_rank"],
                                  pair[0]["rerank_rank"], pair[0]["chunk_id"]))
    focused_count = 0
    for item, match in focused:
        if focused_count >= SEED_SELECTION_POLICY["section_focus_limit"] or len(seeds) >= SEED_SELECTION_POLICY["seed_limit"]:
            break
        count = len(seeds)
        consider(item, "QUERY_SECTION_HEADING")
        if len(seeds) > count:
            decisions_by_id[item["chunk_id"]]["heading_match"] = match
            focused_count += 1
    for item in rerank_order:
        consider(item, "RERANK_FILL")

    # Protection chooses membership; the original model order still governs
    # parent grouping and T79 evidence reservation/completion.
    seeds.sort(key=lambda item: (item["rerank_rank"], item["chunk_id"]))
    decisions, observed_ids = [], set()
    for item in rerank_order:
        cid = item["chunk_id"]
        if cid in observed_ids:
            decisions.append({"chunk_id": cid, "decision": "DUPLICATE_CHUNK_SKIPPED",
                              "duplicate_of_chunk_id": cid})
        else:
            observed_ids.add(cid)
            decisions.append(decisions_by_id[cid])
    groups = {}
    for seed in seeds:
        pid = seed["parent_id"]
        if pid not in groups:
            groups[pid] = {"parent_id": pid, "selected_seed_ids": [], "best_child_logit": seed["reranker_raw_logit"],
                           "first_rerank_rank": seed["rerank_rank"], "all_hit_child_ids": []}
        groups[pid]["selected_seed_ids"].append(seed["chunk_id"])
    for item in rerank_order:
        if item["parent_id"] in groups:
            hits = groups[item["parent_id"]]["all_hit_child_ids"]
            if item["chunk_id"] not in hits:
                hits.append(item["chunk_id"])
    ordered = sorted(groups.values(), key=lambda item: (-item["best_child_logit"],
                     item["first_rerank_rank"], item["parent_id"]))
    return seeds, ordered, decisions
