"""Versioned identities for the chosen BGE models and complete input text."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


IDENTITY_KEYS = {
    "EMBEDDING_SPACE": {"model", "pooling", "dimension", "normalize", "query_instruction", "document_instruction", "tokenizer_add_special_tokens", "document_max_length"},
    "MODEL_EXECUTION_PROFILE": {"FlagEmbedding", "torch", "transformers", "device", "precision", "intra_op_threads", "inter_op_threads", "embedding_batch", "rerank_batch"},
    "RERANKER_PROFILE": {"model", "normalize", "query_max_length", "pair_max_length"},
    "VECTOR_CACHE": {"embedding_space_id", "execution_profile_id", "input_kind", "input_sha256"},
    "CHUNK_SET": {"processing_version_id", "bundle_sha256", "rules", "embedding_space_id"},
    "PARENT": {"chunk_set_id", "ordinal", "text_sha256", "parts"},
    "CHILD": {"chunk_set_id", "parent_id", "ordinal", "text_sha256", "parts"},
    "SOURCE_RECORD": {"processing_version_id", "text_sha256", "parts"},
    "CONSERVATIVE_EQUIVALENCE": {"processing_version_id", "heading_scope", "content_kind", "text"},
}


def identity(kind: str, value: dict) -> str:
    if kind not in IDENTITY_KEYS or type(value) is not dict or set(value) != IDENTITY_KEYS[kind]:
        raise ValueError("unknown identity kind or non-exact identity fields")
    return hashlib.sha256(canonical({"kind": kind, "schema_version": 1, "value": value})).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def profiles(settings) -> dict:
    lock = json.loads((settings.project / "configs/services/models.lock.json").read_bytes())
    if set(lock) != {"schema", "models"} or lock["schema"] != "raghub-models-v1":
        raise ValueError("unsupported model lock")
    embedding = lock["models"]["embedding"]
    reranker = lock["models"]["reranker"]
    space = {
        "model": embedding, "pooling": "cls", "dimension": 1024,
        "normalize": True, "query_instruction": "", "document_instruction": "",
        "tokenizer_add_special_tokens": True, "document_max_length": 1024,
    }
    execution = {
        "FlagEmbedding": importlib.metadata.version("FlagEmbedding"),
        "torch": importlib.metadata.version("torch"),
        "transformers": importlib.metadata.version("transformers"),
        "device": "cpu", "precision": "float32", "intra_op_threads": 4,
        "inter_op_threads": 1, "embedding_batch": 8, "rerank_batch": 4,
    }
    rank = {"model": reranker, "normalize": False, "query_max_length": 640, "pair_max_length": 1792}
    return {
        "embedding_space_id": identity("EMBEDDING_SPACE", space),
        "execution_profile_id": identity("MODEL_EXECUTION_PROFILE", execution),
        "reranker_profile_id": identity("RERANKER_PROFILE", rank),
        "embedding_space": space, "execution_profile": execution, "reranker_profile": rank,
    }


def load_tokenizer(settings):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(str(settings.model_embedding), local_files_only=True)


def token_count(tokenizer, text: str) -> int:
    if not isinstance(text, str):
        raise TypeError("tokenizer input must be text")
    return len(tokenizer.encode(text, add_special_tokens=True, truncation=False))


def vector_key(profile, input_kind: str, text: str) -> str:
    return identity("VECTOR_CACHE", {
        "embedding_space_id": profile["embedding_space_id"],
        "execution_profile_id": profile["execution_profile_id"],
        "input_kind": input_kind, "input_sha256": text_sha256(text),
    })
