"""Locked local Docling/RapidOCR resources; normal conversion never downloads."""
from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .model_assets import file_sha256
from .ingestion.archive import hash_file, inside, json_hash

LOCK_SCHEMA = "raghub.parsing-models.v1"
PARSER_ADAPTER = "raghub-format-adapters-v8"
PARSER_SOURCES = ("formats.py","source_backends.py","html_blocks.py","markdown_html.py","bundle.py","cleanup.py","contracts.py","alignment.py","cache.py","store.py","worker.py")


def parser_identity(settings: Settings, lock: dict | None = None) -> dict:
    """One authoritative implementation identity for parsing and file reuse."""
    return {
        "schema_version":1, "adapter":PARSER_ADAPTER,
        "model_lock_sha256":lock["lock_sha256"] if lock is not None else hash_file(settings.project/"configs/parsing/models.lock.json")[0],
        "adapter_sources":{name:hash_file(settings.project/"backend/raghub/parsing"/name)[0] for name in PARSER_SOURCES},
        "supporting_sources":{name:hash_file(settings.project/"backend/raghub"/name)[0] for name in ("docx_source.py","svg_image.py")},
        "assets_source_sha256":hash_file(settings.project/"backend/raghub/parsing_assets.py")[0],
        "dependency_lock_sha256":hash_file(settings.project/"uv.lock")[0],
    }


def model_file_hash(path: Path) -> tuple[str, int]:
    return file_sha256(path), path.stat().st_size


def parsing_models(settings: Settings) -> dict:
    path = settings.project / "configs/parsing/models.lock.json"
    lock = json.loads(path.read_text(encoding="utf-8"))
    if set(lock) != {"schema", "root", "packages", "repositories", "ocr", "files"} or lock["schema"] != LOCK_SCHEMA:
        raise RuntimeError("unsupported parsing model lock")
    root = Path(lock["root"])
    if not root.is_absolute() or root.resolve() != root or root.is_symlink() or not root.is_dir():
        raise RuntimeError("parsing model root is missing or aliased")
    listed = set()
    for record in lock["files"]:
        if set(record) != {"path", "sha256", "size_bytes"}:
            raise RuntimeError("invalid parsing model member")
        member = inside(root / record["path"], root)
        if member.is_symlink() or not member.is_file() or record["path"] in listed:
            raise RuntimeError("parsing model member missing, aliased or duplicated")
        if model_file_hash(member) != (record["sha256"], record["size_bytes"]):
            raise RuntimeError("parsing model member changed: " + record["path"])
        listed.add(record["path"])
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".cache" not in p.parts}
    if actual != listed:
        raise RuntimeError("parsing model inference inventory changed")
    import importlib.metadata
    for name, version in lock["packages"].items():
        if importlib.metadata.version(name) != version:
            raise RuntimeError("parsing dependency changed: " + name)
    return {**lock, "lock_sha256": hash_file(path)[0], "config_sha256": json_hash(lock)}


def rapid_params(lock: dict) -> dict:
    from rapidocr.utils.typings import EngineType, ModelType, OCRVersion

    root = Path(lock["root"])
    weights = lock["ocr"]
    return {
        "Det.model_path": str(root / weights["det"]["path"]),
        "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": ModelType.SMALL,
        "Cls.model_path": str(root / weights["cls"]["path"]),
        "Cls.ocr_version": OCRVersion.PPOCRV4, "Cls.model_type": ModelType.MOBILE,
        "Rec.model_path": str(root / weights["rec"]["path"]),
        "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": ModelType.SMALL,
        "Det.engine_type": EngineType.ONNXRUNTIME, "Cls.engine_type": EngineType.ONNXRUNTIME, "Rec.engine_type": EngineType.ONNXRUNTIME,
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_cuda": False,
        "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.use_coreml": False,
        "Global.log_level": "warning",
    }
