"""The existing solo document worker builds and conditionally publishes indexes."""
from __future__ import annotations

import traceback
from pathlib import Path

from ..db import now, transaction
from ..ingestion.archive import json_bytes, make_directory, write_closed
from ..ingestion.store import event
from ..model_profiles import load_tokenizer
from .chunks import plan_chunks
from .manifest import write_generation_manifest
from .milvus_store import CollectionBuild, rows_for
from .model_client import ModelClient
from .storage import save_chunk_set, verify_chunk_set
from .store import IndexStore
from .vector_cache import document_vectors


def build_claimed(db, settings, identity_tuple, payload):
    store = IndexStore(db, settings)
    generation_id, name = store.start_candidate(identity_tuple, payload)
    directory = settings.home / "data/generations" / payload["kb_id"] / generation_id / identity_tuple[2]
    client, collection = None, None

    def fence():
        store.current_owner(*identity_tuple)

    def journal(kind, details):
        with transaction(db):
            store.completion_owner(identity_tuple, required=True)
            event(db, identity_tuple[1], identity_tuple[2], kind, details)

    try:
        make_directory(directory, settings.home, exclusive=True)
        with transaction(db):
            fence()
            db.execute("UPDATE attempts SET directory_path=? WHERE attempt_id=?", (str(directory), identity_tuple[2]))
        write_closed(directory / "input.json", json_bytes(payload))
        tokenizer, profile = load_tokenizer(settings), payload["profile"]
        client = ModelClient(settings, profile, journal)
        documents, rows, probe = [], [], None
        for doc in payload["documents"]:
            fence()
            store.progress(identity_tuple, "CHUNKING", {"document_id": doc["document_id"], "generation_id": generation_id})
            chunks = store.reusable_chunks(doc, tokenizer, profile)
            reused = chunks is not None
            if chunks is None:
                planned = plan_chunks(doc["bundle_path"], tokenizer, profile)
                fence()
                saved = save_chunk_set(settings, payload["kb_id"], doc["document_id"], identity_tuple[2], planned)
                chunks = verify_chunk_set(settings, saved["manifest_path"], saved["manifest_sha256"], tokenizer, profile)
            store.adopt_chunks(identity_tuple, generation_id, doc, chunks, reused=reused)
            vectors, stats = document_vectors(db, settings, profile, chunks["children"], client, identity_tuple[2],
                                              fence, lambda stage, details: store.progress(identity_tuple, stage, details))
            store.adopt_vectors(identity_tuple, chunks, vectors, profile, stats)
            doc_rows = rows_for(payload["kb_id"], generation_id, doc["document_id"], chunks["children"], vectors)
            rows.extend(doc_rows)
            if probe is None and doc_rows:
                probe = doc_rows[0]
            documents.append({"document_id": doc["document_id"], "raw_version_id": doc["raw_version_id"],
                              "processing_version_id": doc["processing_version_id"], "chunk_set_id": chunks["chunk_set_id"],
                              "manifest_path": chunks["manifest_path"], "manifest_sha256": chunks["manifest_sha256"],
                              "chunk_reused": reused,
                              "vector_stats": {**{key: value for key, value in stats.items() if key != "operations"},
                                               "operation_ids": [op["operation_id"] for op in stats["operations"]]}})
        fence()
        store.progress(identity_tuple, "MILVUS_WRITING", {"generation_id": generation_id, "row_count": len(rows)})
        collection = CollectionBuild(settings, name, fence, journal)
        collection.create()
        collection.upsert(rows)
        store.transition(identity_tuple, generation_id, "BUILDING", "CHECKING")
        store.progress(identity_tuple, "MILVUS_CHECKING", {"generation_id": generation_id})
        readiness = collection.complete(probe)
        fence()
        value = {"schema": "raghub.generation-manifest.v1", "generation_id": generation_id,
                 "kb_id": payload["kb_id"], "data_instance_id": identity_tuple[0],
                 "task_id": identity_tuple[1], "attempt_id": identity_tuple[2],
                 "input_sha256": store.state(generation_id)["input_sha256"], "input": payload,
                 "collection_name": name, "documents": documents, "readiness": readiness, "created_at": now()}
        manifest = write_generation_manifest(settings, directory, value)
        result = store.publish(identity_tuple, generation_id, payload, manifest)
        return {"adopted": True, "generation_id": generation_id, "manifest": manifest}
    except Exception as error:
        details = {"code": getattr(error, "code", "GENERATION_FAILED"), "error_type": type(error).__name__,
                   "message": str(error), "traceback": traceback.format_exc(), "generation_id": generation_id,
                   "collection_name": name, "automatic_collection_cleanup": False, "automatic_retry": False}
        store.fail_candidate(identity_tuple, generation_id, details)
        store.finish(identity_tuple, error=details)
        return {"adopted": False, "error": details}
    finally:
        if collection is not None:
            collection.close()
        if client is not None:
            client.close()
