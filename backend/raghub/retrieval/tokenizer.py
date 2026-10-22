"""Official DeepSeek offline token estimates; no remote model code or API call."""
from __future__ import annotations

import argparse
import hashlib
import json
from importlib.metadata import version
from pathlib import Path
import zipfile

from ..config import Settings
from ..ingestion.archive import hash_file, make_directory, write_closed
from ..model_profiles import canonical
from ..indexing.storage import decode, digest_format, exact, regular

SCHEMA = "raghub.deepseek-tokenizer.v1"
SOURCE_URL = "https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip"
RESOURCE_NAMES = ("tokenizer.json", "tokenizer_config.json")
LOCK_KEYS = {"schema", "source_url", "archive", "directory", "files", "dependencies",
             "counting", "cloud_usage_calibrated"}
COUNTING = {"add_special_tokens": True, "truncation": False,
            "whole_serialized_evidence": True, "budget": 8000}


def install(settings, archive_path, expected_sha):
    """Install the two data members from the already downloaded official archive."""
    if not digest_format(expected_sha):
        raise ValueError("archive SHA-256 must be 64 lowercase hex characters")
    archive = regular(archive_path, settings.home / "models")
    sha, size = hash_file(archive)
    if sha != expected_sha or not 0 < size <= 64 * 1024 * 1024:
        raise ValueError("official archive binding changed")
    directory = settings.home / "models/tokenizer/deepseek-v4-20261002"
    lock_path = settings.project / "configs/services/deepseek-tokenizer.lock.json"
    if directory.exists() or directory.is_symlink() or lock_path.exists() or lock_path.is_symlink():
        raise FileExistsError("tokenizer installation targets must be fresh")
    payloads = {}
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if [member.filename for member in members] != [
                "deepseek_v4_tokenizer/", "deepseek_v4_tokenizer/deepseek_tokenizer.py",
                "deepseek_v4_tokenizer/tokenizer_config.json", "deepseek_v4_tokenizer/tokenizer.json"]:
            raise ValueError("official package member set changed")
        for name in RESOURCE_NAMES:
            info = package.getinfo("deepseek_v4_tokenizer/" + name)
            if info.is_dir() or not 0 < info.file_size <= 32 * 1024 * 1024:
                raise ValueError("tokenizer data member size/type is invalid")
            payloads[name] = package.read(info)
            if len(payloads[name]) != info.file_size:
                raise ValueError("tokenizer data member is incomplete")
    config = decode(payloads["tokenizer_config.json"])
    if (config["tokenizer_class"] != "LlamaTokenizerFast"
            or config["add_bos_token"] is not False or config["add_eos_token"] is not False):
        raise ValueError("official tokenizer data contract changed")
    files = [{"path": name, "size_bytes": len(payloads[name]),
              "sha256": hashlib.sha256(payloads[name]).hexdigest()} for name in RESOURCE_NAMES]
    record = {"schema": SCHEMA, "source_url": SOURCE_URL,
              "archive": {"path": str(archive), "sha256": sha, "size_bytes": size},
              "directory": str(directory), "files": files,
              "dependencies": {name: version(name) for name in ("transformers", "tokenizers")},
              "counting": COUNTING, "cloud_usage_calibrated": False}
    lock_bytes = canonical(record) + b"\n"
    report = {"directory": str(directory), "lock_path": str(lock_path),
              "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(), "resources": files,
              "cloud_usage_calibrated": False}
    make_directory(directory, settings.home, exclusive=True)
    for name in RESOURCE_NAMES:
        write_closed(directory / name, payloads[name])
    write_closed(lock_path, lock_bytes)
    return report


class EvidenceTokenizer:
    def __init__(self, settings):
        lock_path = regular(settings.project / "configs/services/deepseek-tokenizer.lock.json",
                            settings.project)
        value = decode(lock_path.read_bytes())
        exact(value, LOCK_KEYS)
        if (value["schema"] != SCHEMA or value["source_url"] != SOURCE_URL
                or value["counting"] != COUNTING or value["cloud_usage_calibrated"] is not False):
            raise ValueError("unsupported evidence tokenizer contract")
        directory = Path(value["directory"])
        if (directory != settings.home / "models/tokenizer/deepseek-v4-20261002"
                or directory.resolve(strict=True) != directory or directory.is_symlink()):
            raise ValueError("tokenizer directory differs")
        if {p.name for p in directory.iterdir()} != set(RESOURCE_NAMES):
            raise ValueError("tokenizer data member set differs")
        if type(value["files"]) is not list or [v["path"] for v in value["files"]] != list(RESOURCE_NAMES):
            raise ValueError("tokenizer resource list differs")
        for member in value["files"]:
            exact(member, {"path", "size_bytes", "sha256"})
            if hash_file(regular(directory / member["path"], directory)) != (
                    member["sha256"], member["size_bytes"]):
                raise ValueError("tokenizer resource bytes changed")
        if value["dependencies"] != {name: version(name) for name in ("transformers", "tokenizers")}:
            raise ValueError("tokenizer dependencies changed")
        # Preserve the complete official JSON pipeline. Transformers v5 can
        # reconstruct a Llama pipeline from this legacy class name and lose CJK.
        from tokenizers import Tokenizer
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        probe = "中文证据 2018年：918 million；不包含硬件。"
        encoded = self.tokenizer.encode(probe, add_special_tokens=True)
        if not encoded.ids or self.tokenizer.decode(encoded.ids, skip_special_tokens=False) != probe:
            raise ValueError("official tokenizer pipeline did not preserve the multilingual probe")
        self.identity = {"lock_sha256": hash_file(lock_path)[0], **value,
                         "loader": "TOKENIZERS_JSON_PIPELINE_V1",
                         "implementation_sha256": hash_file(Path(__file__))[0]}

    def count(self, serialized_evidence):
        if type(serialized_evidence) is not str:
            raise TypeError("evidence must be the actual serialized string")
        ids = self.tokenizer.encode(serialized_evidence, add_special_tokens=True).ids
        if type(ids) is not list or any(type(item) is not int for item in ids):
            raise ValueError("tokenizer did not return one complete token sequence")
        if serialized_evidence and not ids:
            raise ValueError("nonempty evidence produced no tokens")
        return len(ids)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("archive_sha256")
    args = parser.parse_args()
    print(json.dumps(install(Settings.load(), args.archive, args.archive_sha256),
                     ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
