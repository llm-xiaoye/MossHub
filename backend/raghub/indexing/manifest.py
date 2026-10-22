"""Closed generation manifests bind SQLite, immutable local files and Milvus."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from ..db import open_db, system_state
from ..ingestion.archive import hash_file, json_hash, make_directory, write_closed
from ..model_profiles import canonical, load_tokenizer, profiles, text_sha256, vector_key
from .milvus_store import METADATA_FIELDS, collection_name, index_contract, DENSE_INDEX, SPARSE_INDEX
from .storage import decode, exact, regular, verify_chunk_set
from .vector_cache import load_vector

KEYS = {"schema", "generation_id", "kb_id", "data_instance_id", "task_id", "attempt_id",
        "input_sha256", "input", "collection_name", "documents", "readiness", "created_at"}
DOC_KEYS = {"document_id", "raw_version_id", "processing_version_id", "chunk_set_id",
            "manifest_path", "manifest_sha256", "chunk_reused", "vector_stats"}


def write_generation_manifest(settings, directory, value):
    exact(value, KEYS)
    if value["schema"] != "raghub.generation-manifest.v1":
        raise ValueError("unsupported generation manifest")
    raw = canonical(value) + b"\n"
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("generation manifest exceeds 8 MiB")
    target = directory / "generation-manifest.json"
    write_closed(target, raw)
    sha, _size = hash_file(target)
    verify_generation_manifest(settings, str(target), sha)
    return {"manifest_path": str(target), "manifest_sha256": sha}


def verify_generation_manifest(settings, path, sha):
    target = regular(path, settings.home / "data/generations")
    digest, size = hash_file(target)
    if digest != sha or not 0 < size <= 8 * 1024 * 1024:
        raise ValueError("generation manifest bytes changed")
    value = decode(target.read_bytes())
    exact(value, KEYS)
    if value["schema"] != "raghub.generation-manifest.v1" or json_hash(value["input"]) != value["input_sha256"]:
        raise ValueError("generation input identity differs")
    payload, kb_id, generation_id = value["input"], value["kb_id"], value["generation_id"]
    profile = profiles(settings)
    if payload["profile"] != profile or payload["kb_id"] != kb_id:
        raise ValueError("generation model or knowledge base differs")
    if value["collection_name"] != collection_name(kb_id, generation_id):
        raise ValueError("generation collection identity differs")
    tokenizer = load_tokenizer(settings)
    metadata = {}
    with closing(open_db(settings.sqlite)) as db:
        if system_state(db)["data_instance_id"] != value["data_instance_id"]:
            raise ValueError("generation data instance differs")
        generation = db.execute("SELECT * FROM generations WHERE generation_id=?", (generation_id,)).fetchone()
        if (generation is None or generation["task_id"] != value["task_id"] or generation["attempt_id"] != value["attempt_id"]
                or generation["input_sha256"] != value["input_sha256"] or generation["collection_name"] != value["collection_name"]
                or generation["embedding_space_id"] != profile["embedding_space_id"]
                or generation["execution_profile_id"] != profile["execution_profile_id"]):
            raise ValueError("SQLite generation binding differs")
        expected_docs = {doc["document_id"]: doc for doc in payload["documents"]}
        if (type(value["documents"]) is not list or len(value["documents"]) != len(expected_docs)
                or len({doc["document_id"] for doc in value["documents"]}) != len(value["documents"])):
            raise ValueError("generation document closure differs")
        registry_docs = {row["document_id"]: dict(row) for row in db.execute(
            "SELECT * FROM generation_documents WHERE generation_id=?", (generation_id,)).fetchall()}
        if set(registry_docs) != set(expected_docs):
            raise ValueError("SQLite generation document set differs")
        for doc in value["documents"]:
            exact(doc, DOC_KEYS)
            expected, registry = expected_docs[doc["document_id"]], registry_docs[doc["document_id"]]
            if type(doc["chunk_reused"]) is not bool:
                raise ValueError("chunk reuse flag type differs")
            for key in ("document_id", "raw_version_id", "processing_version_id"):
                if doc[key] != expected[key] or doc[key] != registry[key]:
                    raise ValueError("generation document version differs")
            chunks = verify_chunk_set(settings, doc["manifest_path"], doc["manifest_sha256"], tokenizer, profile)
            if (doc["chunk_set_id"] != chunks["chunk_set_id"] or doc["chunk_set_id"] != registry["chunk_set_id"]
                    or chunks["bundle_path"] != expected["bundle_path"] or chunks["bundle_sha256"] != expected["bundle_sha256"]):
                raise ValueError("generation chunk input differs")
            artifact = db.execute("SELECT * FROM chunk_sets WHERE chunk_set_id=?", (doc["chunk_set_id"],)).fetchone()
            if (artifact is None or artifact["manifest_path"] != doc["manifest_path"]
                    or artifact["manifest_sha256"] != doc["manifest_sha256"]
                    or artifact["parent_count"] != len(chunks["parents"]) or artifact["child_count"] != len(chunks["children"])):
                raise ValueError("SQLite chunk artifact differs")
            vector_rows = db.execute(
                "SELECT b.chunk_id,b.chunk_ordinal,c.* FROM chunk_vectors b JOIN vector_cache c USING(cache_key) "
                "WHERE b.chunk_set_id=? AND b.execution_profile_id=? ORDER BY b.chunk_ordinal",
                (doc["chunk_set_id"], profile["execution_profile_id"])).fetchall()
            if len(vector_rows) != len(chunks["children"]):
                raise ValueError("SQLite chunk vector count differs")
            for child, record in zip(chunks["children"], vector_rows, strict=True):
                if (record["chunk_id"] != child["chunk_id"] or record["chunk_ordinal"] != child["chunk_ordinal"]
                        or record["cache_key"] != vector_key(profile, child["input_kind"], child["text"])
                        or record["embedding_space_id"] != profile["embedding_space_id"]
                        or record["execution_profile_id"] != profile["execution_profile_id"]
                        or record["input_sha256"] != child["text_sha256"] or record["input_kind"] != child["input_kind"]):
                    raise ValueError("SQLite vector input binding differs")
                load_vector(settings, record)
                if child["chunk_id"] in metadata:
                    raise ValueError("duplicate child ID in generation")
                row = {key: child[key] for key in METADATA_FIELDS if key not in {"kb_id", "document_id", "generation_id"}}
                row.update(kb_id=kb_id, document_id=doc["document_id"], generation_id=generation_id)
                metadata[child["chunk_id"]] = row
    readiness = value["readiness"]
    if (readiness["collection_name"] != value["collection_name"] or readiness["row_count"] != len(metadata)
            or readiness["metadata_sha256"] != text_sha256(canonical([metadata[key] for key in sorted(metadata)]).decode("utf-8"))
            or readiness["load_state"] != "Loaded"):
        raise ValueError("Milvus readiness does not bind local metadata")
    if set(readiness["indexes"]) != {"dense_hnsw", "sparse_bm25"}:
        raise ValueError("Milvus readiness index set differs")
    for name, expected in (("dense_hnsw", DENSE_INDEX), ("sparse_bm25", SPARSE_INDEX)):
        observed = readiness["indexes"][name]
        index_contract(observed, expected)
        if observed["state"] != "Finished":
            raise ValueError("Milvus index is not Finished")
    if set(readiness["readability_probes"]) != {"dense", "sparse"}:
        raise ValueError("Milvus readability routes differ")
    for hits in readiness["readability_probes"].values():
        if len(hits) != 1 or not hits[0] or hits[0][0]["id"] not in metadata:
            raise ValueError("Milvus readability hit closure differs")
        hit = hits[0][0]
        if any(hit["entity"].get(key) != metadata[hit["id"]][key] for key in METADATA_FIELDS):
            raise ValueError("Milvus readability metadata differs")
    return value
