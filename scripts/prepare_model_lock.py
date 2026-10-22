"""Record one completed, pinned local model snapshot; run only after download."""

from __future__ import annotations

import json
from pathlib import Path

from huggingface_hub import HfApi

from raghub.config import Settings
from raghub.model_assets import file_sha256, verify_local_models


REVISIONS = {
    "embedding": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181"),
    "reranker": ("BAAI/bge-reranker-v2-m3", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"),
}


def main() -> None:
    settings = Settings.load()
    target = settings.project / "configs/services/models.lock.json"
    if target.exists():
        raise RuntimeError("model lock already exists; review before making a new version")
    api = HfApi()
    entries: dict[str, dict] = {}
    for role, root in (
        ("embedding", settings.model_embedding),
        ("reranker", settings.model_reranker),
    ):
        repo_id, revision = REVISIONS[role]
        info = api.model_info(repo_id, revision=revision, files_metadata=True)
        if info.sha != revision:
            raise RuntimeError(f"remote model revision changed: {role}")
        remote = {item.rfilename: item for item in info.siblings}
        files = []
        for path in sorted(root.rglob("*")):
            if ".cache" in path.relative_to(root).parts or not path.is_file():
                continue
            if path.is_symlink() or path.resolve() != path:
                raise RuntimeError(f"model asset is aliased: {path}")
            relative = path.relative_to(root).as_posix()
            sibling = remote.get(relative)
            if sibling is None or sibling.size != path.stat().st_size:
                raise RuntimeError(f"model asset missing from pinned remote revision: {relative}")
            digest = file_sha256(path)
            if sibling.lfs and digest != sibling.lfs.sha256:
                raise RuntimeError(f"model LFS digest mismatch: {relative}")
            files.append({"path": relative, "size_bytes": sibling.size, "sha256": digest})
        if not files:
            raise RuntimeError(f"empty model snapshot: {role}")
        entries[role] = {"repo_id": repo_id, "revision": revision, "files": files}
    with target.open("x", encoding="utf-8") as stream:
        json.dump({"schema": "raghub-models-v1", "models": entries}, stream,
                  sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"lock": str(target), "revisions": verify_local_models(settings)}))


if __name__ == "__main__":
    main()
