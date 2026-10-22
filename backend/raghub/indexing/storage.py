"""Closed parent/child/source artifacts; all source ranges retain their real basis."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..ingestion.archive import hash_file, inside, make_directory, write_closed
from ..model_profiles import canonical, identity, text_sha256, token_count
from ..parsing.bundle import verify_bundle
from ..parsing.contracts import Supplement

MANIFEST_KEYS = {"schema", "chunk_set_id", "processing_version_id", "raw_version_id",
                 "embedding_space_id", "rules", "bundle_path", "bundle_sha256", "files", "coverage"}
PARENT_KEYS = {"parent_id", "chunk_set_id", "processing_version_id", "raw_version_id",
               "content_kind", "heading_scope", "body", "text", "token_count", "source_record_id", "child_ids"}
CHILD_KEYS = {"chunk_id", "parent_id", "chunk_set_id", "processing_version_id", "raw_version_id",
              "embedding_space_id", "source_record_id", "content_kind", "source_format", "text",
              "text_sha256", "token_count", "token_limit", "input_kind", "chunk_ordinal", "equivalence_key"}
SOURCE_KEYS = {"source_record_id", "text_sha256", "parts"}
FILE_KEYS = {"path", "sha256", "size_bytes", "record_count"}
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024


def exact(value, keys):
    if type(value) is not dict or set(value) != keys:
        raise ValueError("non-exact artifact fields")


def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def decode(raw):
    return json.loads(raw, object_pairs_hook=no_duplicates,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def digest_format(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def regular(path, root):
    path = Path(path)
    inside(path, root)
    if not path.is_file() or path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError("artifact must be a canonical regular file")
    return path


def write_records(path, records):
    count, size = 0, 0
    with Path(path).open("xb") as stream:
        for record in records:
            raw = canonical(record) + b"\n"
            if len(raw) > MAX_LINE_BYTES or size + len(raw) > MAX_FILE_BYTES:
                raise ValueError("chunk artifact exceeds its declared byte budget")
            stream.write(raw)
            size += len(raw)
            count += 1
        import os
        stream.flush()
        os.fsync(stream.fileno())
    Path(path).chmod(0o444)
    sha, actual_size = hash_file(Path(path))
    if actual_size != size:
        raise ValueError("written artifact size changed")
    return {"path": Path(path).name, "sha256": sha, "size_bytes": size, "record_count": count}


def read_records(path, record_count):
    values = []
    with Path(path).open("rb") as stream:
        for _ in range(record_count):
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line or len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                raise ValueError("invalid or overlong JSONL record")
            values.append(decode(line))
        if stream.read(1):
            raise ValueError("extra JSONL records")
    return values


def save_chunk_set(settings, kb_id, document_id, attempt_id, value):
    exact(value, {"schema", "chunk_set_id", "bundle_path", "bundle_sha256", "processing_version_id",
                  "raw_version_id", "embedding_space_id", "rules", "parents", "children", "sources", "coverage"})
    if value["schema"] != "raghub.chunk-set.v1":
        raise ValueError("unsupported chunk set")
    directory = (settings.home / "data/chunks" / kb_id / document_id /
                 value["processing_version_id"] / value["chunk_set_id"] / attempt_id)
    make_directory(directory, settings.home, exclusive=True)
    files = [write_records(directory / (role + ".jsonl"), value[role])
             for role in ("parents", "children", "sources")]
    metadata = {key: value[key] for key in MANIFEST_KEYS - {"schema", "files"}}
    metadata.update(schema="raghub.chunk-manifest.v1", files=files)
    target = directory / "manifest.json"
    write_closed(target, canonical(metadata) + b"\n")
    sha, _size = hash_file(target)
    return {"manifest_path": str(target), "manifest_sha256": sha,
            "parent_count": len(value["parents"]), "child_count": len(value["children"])}


def verify_source(source, text, fields, processing_id):
    exact(source, SOURCE_KEYS)
    if source["text_sha256"] != text_sha256(text):
        raise ValueError("source record names a different full text")
    covered = 0
    for piece in source["parts"]:
        if type(piece) is not dict or type(piece.get("constructed")) is not bool:
            raise ValueError("invalid source piece")
        if type(piece.get("start")) is not int or type(piece.get("end")) is not int:
            raise ValueError("source interval type")
        start, end = piece["start"], piece["end"]
        if start != covered or not 0 <= start <= end <= len(text):
            raise ValueError("source pieces do not cover the actual text in order")
        covered = end
        if piece["constructed"]:
            exact(piece, {"start", "end", "constructed"})
            continue
        exact(piece, {"start", "end", "constructed", "field_pointer", "cleaned_start", "cleaned_end",
                      "purpose", "locator", "mapping_indices", "mapping_precision"})
        field = fields.get(piece["field_pointer"])
        if field is None:
            raise ValueError("unbound source field")
        first, stop = piece["cleaned_start"], piece["cleaned_end"]
        if type(first) is not int or type(stop) is not int or not 0 <= first <= stop <= len(field.cleaned_text):
            raise ValueError("source field range")
        if text[start:end] != field.cleaned_text[first:stop]:
            raise ValueError("source range does not reproduce exact cleaned text")
        if piece["locator"] != field.locator.model_dump(mode="json"):
            raise ValueError("source location differs from the frozen supplement")
        indices = [i for i, trace in enumerate(field.mappings)
                   if trace.output_start < stop and trace.output_end > first]
        if piece["mapping_indices"] != indices or piece["mapping_precision"] != sorted(
                {field.mappings[i].precision for i in indices}):
            raise ValueError("source interval traces changed")
    if covered != len(text):
        raise ValueError("source coverage incomplete")
    if identity("SOURCE_RECORD", {"processing_version_id": processing_id,
                                 "text_sha256": source["text_sha256"], "parts": source["parts"]}) != source["source_record_id"]:
        raise ValueError("source identity mismatch")


def verify_chunk_rules(rules):
    """Read historical rule versions without assigning them current semantics."""
    if type(rules) is not dict or "schema_version" not in rules:
        raise ValueError("chunk rules require an explicit version")
    rule_version = rules["schema_version"]
    if type(rule_version) is not int or rule_version not in {1, 2, 3, 4, 5}:
        raise ValueError("unsupported chunk rules version")
    keys = {"schema_version", "hierarchical_merge_list_items", "child_tokens",
            "overlap_tokens", "parent_tokens", "indivisible_tokens", "table_view",
            "text_utf8_max", "source_sha256", "docling_core_version"}
    if rule_version in {3, 4, 5}:
        keys.add("list_children")
    if rule_version in {4, 5}:
        keys.add("table_intro_context")
    if rule_version == 5:
        keys.add("html_inline_blocks")
    exact(rules, keys)
    if (rule_version in {3, 4, 5}
            and rules["list_children"] != "SOURCE_LIST_ITEM_BOUNDARY_PROSE_GROUP_V1"):
        raise ValueError("unsupported list-child boundary policy")
    if (rule_version in {4, 5}
            and rules["table_intro_context"] != "MD_ADJACENT_EXPLICIT_UNIT_INTRO_V2"):
        raise ValueError("unsupported table-intro context policy")
    if (rule_version == 5
            and rules["html_inline_blocks"] != "COMPLETE_DOM_BLOCK_BEFORE_INLINE_CODE_SPLIT_V1"):
        raise ValueError("unsupported HTML inline-block policy")
    return rule_version


def verify_chunk_set(settings, manifest_path, expected_sha, tokenizer, profile):
    target = regular(manifest_path, settings.home)
    observed_sha, size = hash_file(target)
    if observed_sha != expected_sha or size > 2 * 1024 * 1024:
        raise ValueError("chunk manifest changed or exceeds its budget")
    manifest = decode(target.read_bytes())
    exact(manifest, MANIFEST_KEYS)
    if manifest["schema"] != "raghub.chunk-manifest.v1" or manifest["embedding_space_id"] != profile["embedding_space_id"]:
        raise ValueError("chunk/model space contract changed")
    if not digest_format(manifest["chunk_set_id"]) or identity("CHUNK_SET", {
            "processing_version_id": manifest["processing_version_id"],
            "bundle_sha256": manifest["bundle_sha256"], "rules": manifest["rules"],
            "embedding_space_id": manifest["embedding_space_id"]}) != manifest["chunk_set_id"]:
        raise ValueError("chunk set identity mismatch")
    bundle = verify_bundle(Path(manifest["bundle_path"]))
    if bundle["status"] != "READY" or bundle["manifest_sha256"] != manifest["bundle_sha256"]:
        raise ValueError("chunk input is no longer a verified READY bundle")
    supplement = Supplement.model_validate_json((Path(bundle["manifest_path"]).parent / "supplement.json").read_bytes())
    if (supplement.processing_version_id != manifest["processing_version_id"]
            or supplement.raw_version_id != manifest["raw_version_id"]):
        raise ValueError("chunk input version mismatch")
    fields = {field.field_pointer: field for field in supplement.fields}
    if type(manifest["files"]) is not list or [item.get("path") for item in manifest["files"]] != [
            "parents.jsonl", "children.jsonl", "sources.jsonl"]:
        raise ValueError("chunk manifest does not contain the exact three members")
    actual_members = {p.name for p in target.parent.iterdir()}
    if actual_members != {"manifest.json", "parents.jsonl", "children.jsonl", "sources.jsonl"}:
        raise ValueError("chunk directory membership changed")
    records = {}
    for file in manifest["files"]:
        exact(file, FILE_KEYS)
        if type(file["size_bytes"]) is not int or not 0 < file["size_bytes"] <= MAX_FILE_BYTES:
            raise ValueError("chunk file byte bound")
        if type(file["record_count"]) is not int or file["record_count"] <= 0:
            raise ValueError("chunk record count")
        path = regular(target.parent / file["path"], settings.home)
        if hash_file(path) != (file["sha256"], file["size_bytes"]):
            raise ValueError("chunk member bytes changed")
        records[file["path"].removesuffix(".jsonl")] = read_records(path, file["record_count"])
    sources = {}
    for source in records["sources"]:
        exact(source, SOURCE_KEYS)
        if source["source_record_id"] in sources:
            raise ValueError("duplicate source identity")
        sources[source["source_record_id"]] = source
    parents = {}
    consumed_sources = set()
    for ordinal, parent in enumerate(records["parents"]):
        exact(parent, PARENT_KEYS)
        if parent["parent_id"] in parents or parent["chunk_set_id"] != manifest["chunk_set_id"]:
            raise ValueError("duplicate or misbound parent")
        if parent["processing_version_id"] != manifest["processing_version_id"] or parent["raw_version_id"] != manifest["raw_version_id"]:
            raise ValueError("parent version mismatch")
        source = sources[parent["source_record_id"]]
        verify_source(source, parent["text"], fields, manifest["processing_version_id"])
        consumed_sources.add(parent["source_record_id"])
        actual_count = token_count(tokenizer, parent["text"])
        if parent["token_count"] != actual_count or actual_count > 1536:
            raise ValueError("parent exceeds complete-input token budget")
        if identity("PARENT", {"chunk_set_id": parent["chunk_set_id"], "ordinal": ordinal,
                               "text_sha256": text_sha256(parent["text"]), "parts": source["parts"]}) != parent["parent_id"]:
            raise ValueError("parent identity mismatch")
        parents[parent["parent_id"]] = parent
    child_ids, by_parent = set(), {key: [] for key in parents}
    for ordinal, child in enumerate(records["children"]):
        exact(child, CHILD_KEYS)
        if child["chunk_id"] in child_ids or child["parent_id"] not in parents:
            raise ValueError("duplicate child or missing parent")
        if (child["chunk_set_id"] != manifest["chunk_set_id"] or child["chunk_ordinal"] != ordinal
                or child["processing_version_id"] != manifest["processing_version_id"]
                or child["raw_version_id"] != manifest["raw_version_id"]
                or child["embedding_space_id"] != manifest["embedding_space_id"]):
            raise ValueError("child version/ordinal/space mismatch")
        source = sources[child["source_record_id"]]
        verify_source(source, child["text"], fields, manifest["processing_version_id"])
        consumed_sources.add(child["source_record_id"])
        count = token_count(tokenizer, child["text"])
        if (child["token_limit"] not in {512, 1024} or child["token_count"] != count
                or count > child["token_limit"] or len(child["text"].encode("utf-8")) > 65535):
            raise ValueError("child complete-input token/byte budget")
        expected_kind = "document_extended" if child["token_limit"] == 1024 else "document_standard"
        if child["input_kind"] != expected_kind or child["text_sha256"] != text_sha256(child["text"]):
            raise ValueError("child encoding input changed")
        if identity("CHILD", {"chunk_set_id": child["chunk_set_id"], "parent_id": child["parent_id"],
                              "ordinal": ordinal, "text_sha256": child["text_sha256"],
                              "parts": source["parts"]}) != child["chunk_id"]:
            raise ValueError("child identity mismatch")
        parent_parts = sources[parents[child["parent_id"]]["source_record_id"]]["parts"]
        for piece in source["parts"]:
            if not piece["constructed"] and not any(
                    not owner["constructed"] and owner["field_pointer"] == piece["field_pointer"]
                    and owner["cleaned_start"] <= piece["cleaned_start"] <= piece["cleaned_end"] <= owner["cleaned_end"]
                    for owner in parent_parts):
                raise ValueError("child source range escapes its exact parent")
        by_parent[child["parent_id"]].append(child["chunk_id"])
        child_ids.add(child["chunk_id"])
    if consumed_sources != set(sources):
        raise ValueError("unused source records")
    for parent_id, ids in by_parent.items():
        if not ids or parents[parent_id]["child_ids"] != ids:
            raise ValueError("parent-child relation is incomplete")
    rule_version = verify_chunk_rules(manifest["rules"])
    exact(manifest["coverage"], {"nonempty_source_fields", "consumed_fields", "title_only_fields", "unmapped_fields"})
    from docling_core.types.doc import DoclingDocument, TitleItem, SectionHeaderItem
    native = DoclingDocument.model_validate_json((Path(bundle["manifest_path"]).parent / "cleaned.native.json").read_bytes())
    if rule_version == 1:
        native_items = native.iterate_items()
    else:
        from .chunks import source_items
        native_items = source_items(native)
    title_fields = {item.self_ref.replace("#", "") + "/text" for item, _level in native_items
                    if isinstance(item, (TitleItem, SectionHeaderItem))}
    ranges = {}
    for source in sources.values():
        for piece in source["parts"]:
            if not piece["constructed"]:
                ranges.setdefault(piece["field_pointer"], []).append((piece["cleaned_start"], piece["cleaned_end"]))
    if (manifest["coverage"]["unmapped_fields"]
            or manifest["coverage"]["nonempty_source_fields"] != sum(bool(f.cleaned_text) for f in fields.values())
            or manifest["coverage"]["consumed_fields"] != sorted(ranges)
            or manifest["coverage"]["title_only_fields"] != sorted(title_fields - set(ranges))):
        raise ValueError("source coverage metadata differs from actual source ranges")
    for pointer, field in fields.items():
        if not field.cleaned_text or pointer in title_fields - set(ranges):
            continue
        covered = 0
        for first, stop in sorted(ranges.get(pointer, [])):
            if first > covered:
                raise ValueError("uncovered cleaned source interval")
            covered = max(covered, stop)
        if covered != len(field.cleaned_text):
            raise ValueError("source field coverage incomplete")
    return {**manifest, **records, "manifest_path": str(target), "manifest_sha256": observed_sha}
