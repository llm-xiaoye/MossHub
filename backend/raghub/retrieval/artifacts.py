"""Read each bound parent/child/source file once within a retrieval request."""
from __future__ import annotations

import hashlib

from ..ingestion.archive import json_hash
from ..model_profiles import canonical, identity, text_sha256
from ..indexing.manifest import KEYS as GENERATION_KEYS, DOC_KEYS
from ..indexing.milvus_store import METADATA_FIELDS, collection_name
from ..indexing.storage import (CHILD_KEYS, FILE_KEYS, MANIFEST_KEYS, PARENT_KEYS,
                               SOURCE_KEYS, decode, digest_format, exact, regular)


class GenerationArtifacts:
    def __init__(self, settings, snapshot, profile):
        self.settings, self.snapshot, self.profile = settings, snapshot, profile
        self.reads, self.children, self.parents, self.sources, self.document_by_child = {}, {}, {}, {}, {}
        self.equivalence_contexts = {}
        generation = snapshot["generation"]
        target = regular(generation["manifest_path"], settings.home / "data/generations")
        manifest = self.read_json(target, generation["manifest_sha256"], 8 * 1024 * 1024)
        exact(manifest, GENERATION_KEYS)
        if (manifest["schema"] != "raghub.generation-manifest.v1"
                or manifest["kb_id"] != snapshot["kb_id"]
                or manifest["generation_id"] != snapshot["generation_id"]
                or manifest["data_instance_id"] != snapshot["data_instance_id"]
                or manifest["collection_name"] != generation["collection_name"]
                or manifest["collection_name"] != collection_name(snapshot["kb_id"], snapshot["generation_id"])
                or manifest["input_sha256"] != generation["input_sha256"]
                or json_hash(manifest["input"]) != manifest["input_sha256"]
                or manifest["input"]["profile"] != profile):
            raise ValueError("generation binding does not match this request")
        registry = {row["document_id"]: row for row in snapshot["documents"]}
        if type(manifest["documents"]) is not list or len(manifest["documents"]) != len(registry):
            raise ValueError("generation document set differs")
        seen = set()
        for document in manifest["documents"]:
            exact(document, DOC_KEYS)
            did = document["document_id"]
            if did in seen or did not in registry:
                raise ValueError("duplicate or unregistered generation document")
            seen.add(did)
            row = registry[did]
            for key in ("raw_version_id", "processing_version_id", "chunk_set_id",
                        "manifest_path", "manifest_sha256"):
                if document[key] != row[key]:
                    raise ValueError("generation document version/file differs")
            self.load_document(document)

    def read_json(self, path, expected_sha, maximum):
        path = regular(path, self.settings.home)
        if str(path) in self.reads:
            raise ValueError("a request must not reread its bound artifact")
        raw = path.read_bytes()
        if not 0 < len(raw) <= maximum or text_sha256_bytes(raw) != expected_sha:
            raise ValueError("bound JSON file changed or exceeds its budget")
        self.reads[str(path)] = {"sha256": expected_sha, "size_bytes": len(raw), "read_count": 1}
        return decode(raw)

    def read_jsonl(self, path, member):
        if str(path) in self.reads:
            raise ValueError("chunk file was already read in this request")
        digest, values, size = hashlib.sha256(), [], 0
        with path.open("rb") as stream:
            for _ in range(member["record_count"]):
                line = stream.readline(2 * 1024 * 1024 + 1)
                if not line or len(line) > 2 * 1024 * 1024 or not line.endswith(b"\n"):
                    raise ValueError("invalid or overlong JSONL record")
                size += len(line)
                if size > member["size_bytes"]:
                    raise ValueError("JSONL exceeds its bound")
                digest.update(line)
                values.append(decode(line))
            if stream.read(1) or size != member["size_bytes"] or digest.hexdigest() != member["sha256"]:
                raise ValueError("chunk member hash/count changed")
        self.reads[str(path)] = {**member, "path": str(path), "read_count": 1}
        return values

    def load_document(self, document):
        target = regular(document["manifest_path"], self.settings.home / "data/chunks")
        manifest = self.read_json(target, document["manifest_sha256"], 2 * 1024 * 1024)
        exact(manifest, MANIFEST_KEYS)
        if (manifest["schema"] != "raghub.chunk-manifest.v1"
                or manifest["chunk_set_id"] != document["chunk_set_id"]
                or manifest["processing_version_id"] != document["processing_version_id"]
                or manifest["raw_version_id"] != document["raw_version_id"]
                or manifest["embedding_space_id"] != self.profile["embedding_space_id"]
                or manifest["coverage"]["unmapped_fields"]):
            raise ValueError("chunk manifest version/coverage differs")
        if (type(manifest["files"]) is not list or [v["path"] for v in manifest["files"]]
                != ["parents.jsonl", "children.jsonl", "sources.jsonl"]):
            raise ValueError("chunk manifest member set differs")
        records = {}
        for member in manifest["files"]:
            exact(member, FILE_KEYS)
            path = regular(target.parent / member["path"], target.parent)
            if (type(member["record_count"]) is not int or member["record_count"] < 1
                    or type(member["size_bytes"]) is not int
                    or not 0 < member["size_bytes"] <= 128 * 1024 * 1024
                    or not digest_format(member["sha256"])):
                raise ValueError("chunk member bytes/count changed")
            records[member["path"]] = self.read_jsonl(path, member)
        # Source-unit mappings use the immutable supplement from this exact
        # processing version; only its needed, hash-bound JSON is loaded once.
        from ..parsing.contracts import Supplement
        bundle_path = regular(manifest["bundle_path"], self.settings.home / "data")
        bundle = self.read_json(bundle_path, manifest["bundle_sha256"], 2 * 1024 * 1024)
        exact(bundle, {"schema", "processing_version_id", "raw_version_id", "config_sha256", "status", "files", "counts"})
        if (bundle["schema"] != "raghub.parsing-bundle.v1" or bundle["status"] != "READY"
                or bundle["processing_version_id"] != document["processing_version_id"]
                or bundle["raw_version_id"] != document["raw_version_id"]):
            raise ValueError("source supplement version is not a published ready input")
        members = [item for item in bundle["files"] if item["path"] == "supplement.json"]
        if len(members) != 1:
            raise ValueError("source supplement member is missing or duplicated")
        member = members[0]
        exact(member, {"path", "sha256", "size_bytes"})
        supplement_path = regular(bundle_path.parent / member["path"], bundle_path.parent)
        supplement = Supplement.model_validate(self.read_json(supplement_path, member["sha256"], 128 * 1024 * 1024))
        if (self.reads[str(supplement_path)]["size_bytes"] != member["size_bytes"]
                or supplement.processing_version_id != document["processing_version_id"]
                or supplement.raw_version_id != document["raw_version_id"]
                or supplement.config_sha256 != bundle["config_sha256"]
                or len(supplement.fields) != bundle["counts"]["fields"]):
            raise ValueError("source supplement identity/count differs")
        if not hasattr(self, "fields"):
            self.fields = {}
        for field in supplement.fields:
            key = (supplement.processing_version_id, field.field_pointer)
            if key in self.fields:
                raise ValueError("duplicate processing field")
            self.fields[key] = field
        local_sources = {}
        for source in records["sources.jsonl"]:
            exact(source, SOURCE_KEYS)
            sid = source["source_record_id"]
            if sid in local_sources:
                raise ValueError("duplicate source record")
            local_sources[sid] = source
        local_parents = {}
        for parent in records["parents.jsonl"]:
            exact(parent, PARENT_KEYS)
            pid = parent["parent_id"]
            if (pid in local_parents or pid in self.parents
                    or parent["chunk_set_id"] != document["chunk_set_id"]
                    or parent["processing_version_id"] != document["processing_version_id"]
                    or parent["raw_version_id"] != document["raw_version_id"]
                    or type(parent["body"]) is not str or not parent["body"]
                    or not parent["text"].endswith(parent["body"])):
                raise ValueError("parent version/body identity differs")
            validate_source(local_sources[parent["source_record_id"]], parent["text"],
                            document["processing_version_id"])
            local_parents[pid] = parent
        by_parent = {pid: [] for pid in local_parents}
        for child in records["children.jsonl"]:
            exact(child, CHILD_KEYS)
            cid = child["chunk_id"]
            if (cid in self.children or child["parent_id"] not in local_parents
                    or child["chunk_set_id"] != document["chunk_set_id"]
                    or child["processing_version_id"] != document["processing_version_id"]
                    or child["raw_version_id"] != document["raw_version_id"]
                    or child["embedding_space_id"] != self.profile["embedding_space_id"]
                    or child["text_sha256"] != text_sha256(child["text"])
                    or not digest_format(child["equivalence_key"])):
                raise ValueError("child version/input identity differs")
            validate_source(local_sources[child["source_record_id"]], child["text"],
                            document["processing_version_id"])
            expected = identity("CONSERVATIVE_EQUIVALENCE", {
                "processing_version_id": child["processing_version_id"],
                "heading_scope": local_parents[child["parent_id"]]["heading_scope"],
                "content_kind": child["content_kind"], "text": child["text"],
            })
            if child["equivalence_key"] != expected:
                raise ValueError("stored conservative equivalence key changed")
            self.children[cid], self.document_by_child[cid] = child, document
            by_parent[child["parent_id"]].append(cid)
        for pid, ids in by_parent.items():
            if ids != local_parents[pid]["child_ids"]:
                raise ValueError("parent/child membership differs")
        equality = parsed_content_equivalence(supplement, local_parents.values())
        for parent_id in local_parents:
            self.equivalence_contexts[parent_id] = equality
        self.parents.update(local_parents)
        for sid, source in local_sources.items():
            if sid in self.sources and self.sources[sid] != source:
                raise ValueError("source identity collision")
            self.sources[sid] = source

    def check_hit(self, hit):
        if type(hit) is not dict or set(hit) != {"id", "distance", "entity"}:
            raise ValueError("unsupported SDK hit shape")
        entity, cid = hit["entity"], hit["id"]
        if type(cid) is not str or cid not in self.children:
            raise ValueError("hit does not name a child in the request generation")
        child, document = self.children[cid], self.document_by_child[cid]
        if document["document_id"] in self.snapshot["removal_snapshot"]["removed_document_ids"]:
            raise ValueError("removed document escaped the Milvus filter")
        if type(entity) is not dict or set(entity) != set(METADATA_FIELDS):
            raise ValueError("Milvus metadata keys differ")
        expected = {key: child[key] for key in METADATA_FIELDS
                    if key not in {"kb_id", "generation_id", "document_id"}}
        expected.update(kb_id=self.snapshot["kb_id"], generation_id=self.snapshot["generation_id"],
                        document_id=document["document_id"])
        if entity != expected:
            raise ValueError("Milvus/local metadata or source binding differs")
        return child


def text_sha256_bytes(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def validate_source(source, text, processing_id):
    exact(source, SOURCE_KEYS)
    if source["text_sha256"] != text_sha256(text):
        raise ValueError("source record text changed")
    covered = 0
    for part in source["parts"]:
        if (type(part) is not dict or type(part.get("constructed")) is not bool
                or type(part.get("start")) is not int or type(part.get("end")) is not int
                or part["start"] != covered or not 0 <= part["start"] <= part["end"] <= len(text)):
            raise ValueError("source intervals do not cover exact text")
        covered = part["end"]
        if not part["constructed"]:
            if (part["cleaned_end"] - part["cleaned_start"] != part["end"] - part["start"]
                    or part["cleaned_start"] < 0 or type(part["locator"]) is not dict):
                raise ValueError("source coordinate basis differs")
    if covered != len(text) or identity("SOURCE_RECORD", {
            "processing_version_id": processing_id, "text_sha256": source["text_sha256"],
            "parts": source["parts"]}) != source["source_record_id"]:
        raise ValueError("source closure identity differs")

EQUIVALENCE_POLICY = "EXACT_PARSED_CONTENT_CONTEXT_V4"


def equivalence_text(value):
    """Normalize line endings only in the comparison view, never in source bytes."""
    return value.replace("\r\n", "\n")


def parsed_content_equivalence(supplement, parents):
    """Bind all parsed content and applicability, keeping acquisition IDs as provenance."""
    details = dict(supplement.format_details)
    observation = details.get("source_observation")
    if type(observation) is dict:
        observation = dict(observation)
        # Exact acquisition identities do not change the complete content below.
        # Dates, revisions, directory context and unknown applicability stay bound.
        for key in ("acquisition_attempt_manifest", "filename", "raw_version_id",
                    "relative_path", "source_root_id", "unchanged"):
            observation.pop(key, None)
        provenance = observation.get("provenance")
        if type(provenance) is dict:
            provenance = dict(provenance)
            provenance.pop("origin_local_path", None)
            observation["provenance"] = provenance
        details["source_observation"] = observation
    serialized = canonical({
        "policy": EQUIVALENCE_POLICY,
        "adapter": supplement.adapter, "config_sha256": supplement.config_sha256,
        "model_lock_sha256": supplement.model_lock_sha256,
        "scope": supplement.scope, "format_details": details,
        "quality": supplement.quality, "images": supplement.images,
        # Complete fields retain headings, table units, footnotes and conditions.
        "fields": [{"field_pointer": field.field_pointer,
                    "original_text": equivalence_text(field.original_text),
                    "cleaned_text": equivalence_text(field.cleaned_text)} for field in supplement.fields],
        "parents": [{key: equivalence_text(parent[key]) if key in ("text", "body") else parent[key]
                     for key in ("text", "body", "content_kind", "heading_scope")}
                    for parent in parents],
    }).decode("utf-8")
    return {"sha256": text_sha256(serialized), "serialized": serialized}
