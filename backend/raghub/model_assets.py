"""Verify the local model snapshot selected for this project."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import Settings


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_local_models(settings: Settings) -> dict[str, str]:
    lock_path = settings.project / "configs/services/models.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if set(lock) != {"schema", "models"} or lock["schema"] != "raghub-models-v1":
        raise RuntimeError("model lock has an unsupported schema")
    models = lock["models"]
    expected = {
        "embedding": (settings.model_embedding, "BAAI/bge-m3"),
        "reranker": (settings.model_reranker, "BAAI/bge-reranker-v2-m3"),
    }
    if set(models) != set(expected):
        raise RuntimeError("model lock does not name the required models")
    revisions: dict[str, str] = {}
    for role, (root, repo_id) in expected.items():
        entry = models[role]
        if (set(entry) != {"repo_id", "revision", "files"}
                or entry["repo_id"] != repo_id
                or not isinstance(entry["revision"], str)
                or len(entry["revision"]) != 40
                or any(c not in "0123456789abcdef" for c in entry["revision"])):
            raise RuntimeError(f"invalid model revision binding: {role}")
        if not root.is_dir() or root.is_symlink() or root.resolve() != root:
            raise RuntimeError(f"model directory is missing or aliased: {root}")
        listed: set[str] = set()
        for file_entry in entry["files"]:
            if (not isinstance(file_entry, dict)
                    or set(file_entry) != {"path", "size_bytes", "sha256"}):
                raise RuntimeError(f"invalid model file entry: {role}")
            relative = Path(file_entry["path"])
            if (relative.is_absolute() or ".." in relative.parts
                    or not relative.parts or relative.parts[0] == ".cache"):
                raise RuntimeError(f"unsafe model file path: {role}")
            name = relative.as_posix()
            if name in listed:
                raise RuntimeError(f"duplicate model file: {role}/{name}")
            listed.add(name)
            file_path = root / relative
            if (not file_path.is_file() or file_path.is_symlink()
                    or file_path.resolve() != file_path):
                raise RuntimeError(f"model file missing or aliased: {role}/{name}")
            if file_path.stat().st_size != file_entry["size_bytes"]:
                raise RuntimeError(f"model file size mismatch: {role}/{name}")
            if file_sha256(file_path) != file_entry["sha256"]:
                raise RuntimeError(f"model file digest mismatch: {role}/{name}")
        actual = {
            file.relative_to(root).as_posix()
            for file in root.rglob("*")
            if file.is_file() and ".cache" not in file.relative_to(root).parts
        }
        if listed != actual:
            raise RuntimeError(f"model file inventory mismatch: {role}")
        revisions[role] = entry["revision"]
    return revisions
