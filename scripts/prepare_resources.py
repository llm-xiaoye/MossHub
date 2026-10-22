"""Download pinned local resources for a fresh install; never call a cloud model."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import urllib.request
import zipfile

from raghub.config import Settings
from raghub.ingestion.archive import json_bytes
from raghub.model_assets import verify_local_models
from raghub.parsing_assets import parsing_models
from raghub.retrieval.tokenizer import EvidenceTokenizer


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_new(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)


def check(path, record):
    digest = hashlib.sha256()
    total = 0
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Resource is missing or aliased: " + str(path))
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(block)
            digest.update(block)
    if total != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
        raise RuntimeError("Resource size/SHA-256 mismatch: " + str(path))


def member(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeError("Unsafe resource member")
    return root / path


def resolve(value, home):
    if isinstance(value, str):
        return value.replace("@RAG_HOME@", str(home))
    if isinstance(value, list):
        return [resolve(item, home) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, home) for key, item in value.items()}
    return value


def download_url(url, target, record):
    if not url.startswith("https://"):
        raise RuntimeError("HTTPS is required for resource downloads")
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, target.open("xb") as output:
        total = 0
        for block in iter(lambda: response.read(1024 * 1024), b""):
            total += len(block)
            if total > record["size_bytes"]:
                raise RuntimeError("Resource response exceeds pinned size")
            output.write(block)
    check(target, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true",
                        help="Explicitly download resources into fresh targets")
    args = parser.parse_args()
    if not args.download:
        parser.error("--download is required; no resources were downloaded")

    settings = Settings.load()
    if settings.project.resolve() != Path(__file__).resolve().parents[1]:
        raise RuntimeError("Repository must be located at RAG_HOME/project")
    templates = settings.project / "configs/templates"
    parsing = resolve(read(templates / "parsing-models.lock.json"), settings.home)
    tokenizer = resolve(read(templates / "deepseek-tokenizer.lock.json"), settings.home)
    models = read(settings.project / "configs/services/models.lock.json")
    # Package versions are part of the existing runtime resource contracts.
    for name, expected in {**parsing["packages"], **tokenizer["dependencies"]}.items():
        if importlib.metadata.version(name) != expected:
            raise RuntimeError("Install uv.lock before resources; package differs: " + name)

    parsing_lock = settings.project / "configs/parsing/models.lock.json"
    tokenizer_lock = settings.project / "configs/services/deepseek-tokenizer.lock.json"
    parsing_root = Path(parsing["root"])
    tokenizer_root = Path(tokenizer["directory"])
    archive = Path(tokenizer["archive"]["path"])
    targets = [settings.model_embedding, settings.model_reranker, parsing_root,
               tokenizer_root, archive, parsing_lock, tokenizer_lock]
    if any(os.path.lexists(path) for path in targets):
        raise RuntimeError("A target already exists; inspect partial/previous installation. Nothing overwritten.")
    for path in targets:
        path.relative_to(settings.home)
    from huggingface_hub import hf_hub_download

    for role, root in (("embedding", settings.model_embedding),
                       ("reranker", settings.model_reranker)):
        root.mkdir(parents=True, exist_ok=False)
        record = models["models"][role]
        for item in record["files"]:
            hf_hub_download(repo_id=record["repo_id"], revision=record["revision"],
                            filename=item["path"], local_dir=root, token=False)
            check(member(root, item["path"]), item)
    verify_local_models(settings)

    parsing_root.mkdir(parents=True, exist_ok=False)
    records = {item["path"]: item for item in parsing["files"]}
    for repository in parsing["repositories"]:
        root = member(parsing_root, repository["relative_root"])
        root.mkdir(parents=True, exist_ok=False)
        for name in repository["selected_members"]:
            hf_hub_download(repo_id=repository["repo_id"],
                            revision=repository["resolved_revision"],
                            filename=name, local_dir=root, token=False)
            relative = repository["relative_root"] + "/" + name
            check(member(parsing_root, relative), records[relative])

    for role in ("det", "cls", "rec"):
        item = parsing["ocr"][role]
        download_url(item["catalog_url"], member(parsing_root, item["path"]), item)
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(member(parsing_root, parsing["ocr"]["rec"]["path"])),
                                  sess_options=options, providers=["CPUExecutionProvider"])
    metadata = session.get_modelmeta().custom_metadata_map
    metadata_path = member(parsing_root, parsing["ocr"]["rec"]["embedded_metadata"]["path"])
    write_new(metadata_path, json_bytes(metadata))
    for item in parsing["files"]:
        check(member(parsing_root, item["path"]), item)
    write_new(parsing_lock, json_bytes(parsing))
    parsing_models(settings)

    download_url(tokenizer["source_url"], archive, tokenizer["archive"])
    tokenizer_root.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as package:
        # Extract only the pinned JSON data, never remote Python code.
        for item in tokenizer["files"]:
            name = "deepseek_v4_tokenizer/" + item["path"]
            info = package.getinfo(name)
            if info.is_dir() or info.file_size != item["size_bytes"]:
                raise RuntimeError("Tokenizer archive member changed")
            target = member(tokenizer_root, item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            check(target, item)
    write_new(tokenizer_lock, json_bytes(tokenizer))
    EvidenceTokenizer(settings)
    print(json.dumps({"resources": "VERIFIED", "models": sorted(models["models"]),
                      "parsing_members": len(parsing["files"]),
                      "tokenizer_members": len(tokenizer["files"]),
                      "provider_calls": 0}))


if __name__ == "__main__":
    main()
