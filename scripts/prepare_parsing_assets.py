"""Prepare the selected parsing resources once; lock exact downloaded bytes."""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import shutil
from pathlib import Path

import onnxruntime as ort
import yaml
from huggingface_hub import HfApi, snapshot_download

from raghub.config import Settings
from raghub.ingestion.archive import hash_file, write_closed, json_bytes
from raghub.parsing_assets import LOCK_SCHEMA, parsing_models, model_file_hash

SELECTED_OCR = {
    "det": ("PP-OCRv6", "det", "PP-OCRv6_det_small.onnx"),
    "cls": ("PP-OCRv4", "cls", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
    "rec": ("PP-OCRv6", "rec", "PP-OCRv6_rec_small.onnx"),
}


def prepare(root: Path) -> dict:
    settings = Settings.load()
    lock_path = settings.project / "configs/parsing/models.lock.json"
    if lock_path.exists():
        raise RuntimeError("a parsing lock already exists; inspect it instead of overwriting")
    if not root.is_absolute() or root.parent.resolve() != root.parent:
        raise RuntimeError("model root must be canonical")
    root.mkdir(parents=True, exist_ok=False)
    api = HfApi(token=False)
    repositories = []
    for role, repo, revision in [
        ("layout", "docling-project/docling-layout-heron", "main"),
        ("tableformer", "docling-project/docling-models", "v2.3.0"),
    ]:
        info = api.model_info(repo, revision=revision, files_metadata=False)
        commit = info.sha
        if not commit or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise RuntimeError("model repository did not report an exact commit")
        members = [x.rfilename for x in info.siblings]
        if role == "layout":
            selected = [name for name in members if name.endswith((".json", ".bin", ".safetensors"))]
        else:
            selected = [name for name in members if name.startswith("model_artifacts/tableformer/")]
        if not selected:
            raise RuntimeError("selected model repository has no inference members")
        destination = root / repo.replace("/", "--")
        snapshot_download(repo_id=repo, revision=commit, token=False,
                          local_dir=destination, allow_patterns=selected)
        repositories.append({"role": role, "repo_id": repo, "requested_revision": revision,
                             "resolved_revision": commit, "relative_root": destination.name,
                             "selected_members": sorted(selected)})
    distribution = metadata.distribution("rapidocr")
    catalog_path = Path(distribution.locate_file("rapidocr/default_models.yaml"))
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))["onnxruntime"]
    records = {}
    for role, (version, task, filename) in SELECTED_OCR.items():
        choices = [entry for entry in catalog[version][task].values()
                   if entry["model_dir"].endswith("/" + filename)]
        if len(choices) != 1:
            raise RuntimeError("selected RapidOCR catalog entry is ambiguous")
        selected = choices[0]
        source = Path(distribution.locate_file("rapidocr/models/" + filename))
        destination = root / "ocr" / filename
        destination.parent.mkdir(exist_ok=True)
        if model_file_hash(source)[0] != selected["SHA256"]:
            raise RuntimeError("bundled OCR bytes do not match the fixed package catalog")
        with source.open("rb") as src, destination.open("xb") as target:
            shutil.copyfileobj(src, target)
        digest, size = model_file_hash(destination)
        records[role] = {"version": version, "model_type": "mobile" if role == "cls" else "small",
                         "path": destination.relative_to(root).as_posix(), "sha256": digest,
                         "size_bytes": size, "catalog_url": selected["model_dir"],
                         "catalog_sha256": hash_file(catalog_path)[0]}
    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads, session_options.inter_op_num_threads = 4, 1
    session = ort.InferenceSession(str(root / records["rec"]["path"]),
                                  sess_options=session_options, providers=["CPUExecutionProvider"])
    embedded = session.get_modelmeta().custom_metadata_map
    if not embedded:
        raise RuntimeError("selected recognizer has no embedded metadata")
    meta_path = root / "ocr/recognizer-metadata.json"
    write_closed(meta_path, json_bytes(embedded))
    records["rec"]["embedded_metadata"] = {"path": meta_path.relative_to(root).as_posix(),
                                         "sha256": hash_file(meta_path)[0],
                                         "keys": sorted(embedded)}
    files = []
    for path in sorted(root.rglob("*")):
        if ".cache" in path.parts or not path.is_file():
            continue
        if path.is_symlink():
            raise RuntimeError("downloaded inference resource is a symlink")
        digest, size = model_file_hash(path)
        files.append({"path": path.relative_to(root).as_posix(), "sha256": digest, "size_bytes": size})
    lock = {"schema": LOCK_SCHEMA, "root": str(root),
            "packages": {name: metadata.version(name) for name in
                         ("docling", "docling-core", "docling-ibm-models", "rapidocr",
                          "onnxruntime", "torch", "transformers")},
            "repositories": repositories, "ocr": records, "files": files}
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    write_closed(lock_path, json_bytes(lock))
    verified = parsing_models(settings)
    return {"lock_path": str(lock_path), "lock_sha256": verified["lock_sha256"],
            "root": str(root), "member_count": len(files),
            "total_inference_bytes": sum(x["size_bytes"] for x in files),
            "repositories": repositories, "ocr": records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
