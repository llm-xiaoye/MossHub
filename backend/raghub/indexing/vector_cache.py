"""T40: normalized document vectors are adopted only after their NPY bytes verify."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from ..db import new_id, now, transaction
from ..ingestion.archive import hash_file, make_directory
from ..model_profiles import canonical, identity, text_sha256, vector_key
from .storage import regular


def vector_values(value):
    array = np.asarray(value, dtype=np.float32)
    if array.shape != (1024,) or not np.isfinite(array).all():
        raise ValueError("dense vector must contain exactly 1024 finite float32 values")
    norm = float(np.linalg.norm(array.astype(np.float64)))
    if not np.isfinite(norm) or norm == 0 or abs(norm - 1.0) > 1e-4:
        raise ValueError("dense vector is not normalized within tolerance")
    return array, norm


def register_profiles(db, profile):
    for key, kind, config_key in (
            ("embedding_space_id", "EMBEDDING_SPACE", "embedding_space"),
            ("execution_profile_id", "MODEL_EXECUTION_PROFILE", "execution_profile"),
            ("reranker_profile_id", "RERANKER_PROFILE", "reranker_profile")):
        config = profile[config_key]
        if profile[key] != identity(kind, config):
            raise ValueError("model profile identity mismatch")
        raw = canonical(config).decode("utf-8")
        prior = db.execute("SELECT kind,config_json FROM model_profiles WHERE profile_id=?", (profile[key],)).fetchone()
        if prior:
            if prior["kind"] != kind or prior["config_json"] != raw:
                raise ValueError("model profile bytes changed under the same identity")
        else:
            db.execute("INSERT INTO model_profiles(profile_id,kind,config_json,created_at) VALUES (?,?,?,?)",
                       (profile[key], kind, raw, now()))


def load_vector(settings, record):
    path = regular(record["vector_path"], settings.home / "data/vectors")
    if hash_file(path) != (record["vector_sha256"], record["size_bytes"]):
        raise ValueError("persisted vector bytes changed")
    with path.open("rb") as stream:
        array = np.load(stream, allow_pickle=False)
        if stream.read(1):
            raise ValueError("trailing bytes after NPY vector")
    if array.dtype != np.dtype(np.float32):
        raise ValueError("persisted vector dtype changed")
    array, norm = vector_values(array)
    if abs(norm - record["norm"]) > 1e-8 or record["dimension"] != 1024:
        raise ValueError("persisted vector metadata changed")
    return array


def cached_vector(db, settings, profile, child):
    key = vector_key(profile, child["input_kind"], child["text"])
    row = db.execute("SELECT * FROM vector_cache WHERE cache_key=?", (key,)).fetchone()
    if row is None:
        return key, None
    if (row["embedding_space_id"] != profile["embedding_space_id"]
            or row["execution_profile_id"] != profile["execution_profile_id"]
            or row["input_kind"] != child["input_kind"] or row["input_sha256"] != child["text_sha256"]):
        raise ValueError("vector-cache key metadata mismatch")
    return key, {**dict(row), "vector": load_vector(settings, row)}


def persist_vector(db, settings, profile, child, values, fence):
    key = vector_key(profile, child["input_kind"], child["text"])
    array, norm = vector_values(values)
    directory = settings.home / "data/vectors" / profile["embedding_space_id"] / profile["execution_profile_id"] / key
    make_directory(directory, settings.home, exclusive=False)
    target = directory / (new_id() + ".npy")
    with target.open("xb") as stream:
        np.save(stream, array, allow_pickle=False)
        stream.flush()
        os.fsync(stream.fileno())
    target.chmod(0o444)
    sha, size = hash_file(target)
    record = {"cache_key": key, "embedding_space_id": profile["embedding_space_id"],
              "execution_profile_id": profile["execution_profile_id"], "input_kind": child["input_kind"],
              "input_sha256": text_sha256(child["text"]), "dimension": 1024,
              "vector_path": str(target), "vector_sha256": sha, "size_bytes": size,
              "norm": norm, "created_at": now()}
    load_vector(settings, record)
    with transaction(db):
        fence()
        prior = db.execute("SELECT * FROM vector_cache WHERE cache_key=?", (key,)).fetchone()
        if prior:
            # A complete orphan is retained; an already adopted exact key wins.
            load_vector(settings, prior)
            record = dict(prior)
        else:
            db.execute("INSERT INTO vector_cache(cache_key,embedding_space_id,execution_profile_id,input_kind,input_sha256,"
                       "dimension,vector_path,vector_sha256,size_bytes,norm,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       tuple(record[name] for name in ("cache_key", "embedding_space_id", "execution_profile_id", "input_kind",
                                                       "input_sha256", "dimension", "vector_path", "vector_sha256",
                                                       "size_bytes", "norm", "created_at")))
    return {**record, "vector": load_vector(settings, record)}


def document_vectors(db, settings, profile, children, model_client, owner_id, fence, progress):
    vectors, misses, reused = {}, [], 0
    for child in children:
        fence()
        _key, found = cached_vector(db, settings, profile, child)
        if found is None:
            misses.append(child)
        else:
            vectors[child["chunk_id"]] = found
            reused += 1
    pending = {}
    for child in misses:
        pending.setdefault(vector_key(profile, child["input_kind"], child["text"]), []).append(child)
    representatives = [group[0] for group in pending.values()]
    operations = []
    batch_size = 4
    for kind in ("document_standard", "document_extended"):
        # Equal-length inputs retain their original order; IDs, cache keys and
        # the final per-child mapping remain independent of compute ordering.
        group = sorted((child for child in representatives if child["input_kind"] == kind),
                       key=lambda child: child["token_count"])
        for first in range(0, len(group), batch_size):
            batch = group[first:first + batch_size]
            fence()
            operation_id = new_id()
            progress("EMBEDDING", {"operation_id": operation_id, "owner_id": owner_id, "input_kind": kind,
                                    "item_ids": [child["chunk_id"] for child in batch], "budget_ms": 30000})
            items = [{"item_id": child["chunk_id"], "text": child["text"], "input_kind": kind} for child in batch]
            response = model_client.embed(items, owner_id=owner_id, operation_id=operation_id, fence=fence)
            operations.append(response["operation"])
            for child, item in zip(batch, response["items"], strict=True):
                if item["item_id"] != child["chunk_id"]:
                    raise ValueError("model vector order changed")
                record = persist_vector(db, settings, profile, child, item["vector"], fence)
                key = vector_key(profile, child["input_kind"], child["text"])
                for equivalent_input in pending[key]:
                    vectors[equivalent_input["chunk_id"]] = record
    if set(vectors) != {child["chunk_id"] for child in children}:
        raise ValueError("document vector set is incomplete")
    return vectors, {"cache_hit_child_count": reused, "computed_input_count": len(representatives),
                     "computed_child_count": len(misses), "within_request_duplicate_child_count": len(misses) - len(representatives),
                     "operations": operations}
