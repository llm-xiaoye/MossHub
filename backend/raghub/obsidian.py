"""Read-only fixed-vault browsing and explicit selected-file intake."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import stat
import time
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Header, Query
from pydantic import Field, UUID4

from .config import Settings
from .contracts import StrictRecord
from .db import new_id, open_db
from .ingestion.archive import json_bytes, make_directory
from .ingestion.contracts import (
    BATCH_LIMIT, FILE_LIMIT, IMAGE_SUFFIXES, SUFFIXES, DirectoryPreviewRequest, FileItem, relative_path,
)
from .ingestion.store import BusinessError, IngestionStore, canonical_root, require_instance, require_kb

VAULT_ROOT = (Settings.load().home / "obsidian")
ENTRY_LIMIT = 20_000
SELECTION_LIMIT = 2_000
EXCLUDED = {".git", ".obsidian"}


class WorkflowRequest(StrictRecord):
    kb_id: UUID4
    preview_id: UUID4
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: bool


class WorkflowAction(StrictRecord):
    action: str = Field(pattern=r"^(pause|resume|finish|use-ready)$")
    ready_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SelectedFile(StrictRecord):
    relative_path: str = Field(min_length=1, max_length=4096)
    signature: str = Field(min_length=1, max_length=300)


class SelectionRequest(StrictRecord):
    paths: list[str] = Field(default_factory=list, max_length=SELECTION_LIMIT)
    directories: list[str] = Field(default_factory=list, max_length=SELECTION_LIMIT)


class CheckRequest(StrictRecord):
    entries: list[SelectedFile] = Field(max_length=10000)


class PreviewRequest(CheckRequest):
    kb_id: UUID4
    entries: list[SelectedFile] = Field(max_length=SELECTION_LIMIT)
    document_id: UUID4 | None = None
    expected_raw_version_id: UUID4 | None = None
    registered_source_id: UUID4 | None = None


class DirectoryUpdateRequest(StrictRecord):
    kb_id: UUID4
    document_id: UUID4
    absolute_root: str = Field(min_length=1, max_length=8192)
    file: FileItem


def asset_member(asset: dict) -> str:
    return "resources/" + asset["sha256"] + PurePosixPath(asset["relative_path"]).suffix.lower()


class Vault:
    def __init__(self, settings: Settings, root: Path = VAULT_ROOT):
        self.settings, self.root = settings, root
        self._scanner = None

    def scanner(self):
        if self._scanner is None:
            spec = importlib.util.spec_from_file_location("raghub_obsidian_directory", self.settings.project / "scripts/directory.py")
            if spec is None or spec.loader is None:
                raise RuntimeError("directory adapter is unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._scanner = module
        return self._scanner

    @staticmethod
    def path(value: str, *, directory: bool = False) -> str:
        if directory and value == "":
            return value
        try:
            result = relative_path(value)
        except ValueError as exc:
            raise BusinessError("OBSIDIAN_PATH_INVALID", "只允许知识库内的相对路径。", 422) from exc
        if any(part in EXCLUDED for part in result.split("/")):
            raise BusinessError("OBSIDIAN_PATH_EXCLUDED", ".git 与 .obsidian 不参与资料导入。", 422)
        return result

    @contextmanager
    def root_fd(self):
        fd = None
        try:
            if self.root.is_symlink() or self.root.resolve(strict=True) != self.root:
                raise ValueError("vault root is aliased")
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open("/", flags)
            for part in self.root.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
        except (OSError, ValueError) as exc:
            if fd is not None:
                os.close(fd)
            raise BusinessError("OBSIDIAN_ROOT_UNAVAILABLE", "Obsidian 知识库目录不可读；保留上次视图，请检查目录权限。", 422) from exc
        try:
            before = os.fstat(fd)
            yield fd
            current = os.stat(self.root, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise BusinessError("OBSIDIAN_ROOT_CHANGED", "知识库根目录在读取期间被替换，请重新查看。")
        finally:
            os.close(fd)

    @contextmanager
    def directory(self, root_fd: int, path: str):
        self.path(path, directory=True)
        fd = os.dup(root_fd)
        try:
            for part in path.split("/") if path else []:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError as exc:
                    raise BusinessError("OBSIDIAN_DIRECTORY_MISSING", "子目录已消失；保留原选择，继续刷新其他目录。", 404) from exc
                except NotADirectoryError as exc:
                    raise BusinessError("OBSIDIAN_DIRECTORY_INVALID", "该路径已不再是目录；保留原选择，需要重新核对。", 409) from exc
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def signature(value) -> str:
        return ":".join(str(item) for item in (
            value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        ))

    def entry(self, path: str, value) -> dict:
        name = PurePosixPath(path).name
        kind = "DIRECTORY" if stat.S_ISDIR(value.st_mode) else "REGULAR" if stat.S_ISREG(value.st_mode) else "SYMLINK" if stat.S_ISLNK(value.st_mode) else "OTHER"
        reason = None
        if kind == "SYMLINK":
            reason = "SYMLINK"
        elif kind == "DIRECTORY" and name in EXCLUDED:
            reason = "EXCLUDED_DIRECTORY"
        elif kind not in {"DIRECTORY", "REGULAR"}:
            reason = "NON_REGULAR"
        elif kind == "REGULAR":
            if name == ".DS_Store":
                reason = "SYSTEM_FILE"
            elif name.startswith(("~$", ".~")) or name.endswith("~") or name.lower().endswith((".tmp", ".temp", ".swp", ".swo")):
                reason = "TEMPORARY_FILE"
            elif PurePosixPath(name).suffix.lower() not in SUFFIXES:
                reason = "UNSUPPORTED_EXTENSION"
            elif value.st_size > FILE_LIMIT:
                reason = "FILE_TOO_LARGE"
        return {"relative_path": path, "filename": name, "kind": kind,
                "eligible": reason is None, "skip_reason": reason,
                "size_bytes": value.st_size, "signature": self.signature(value)}

    def rows(self, fd: int, prefix: str) -> list[dict]:
        before = os.fstat(fd)
        result = []
        with os.scandir(fd) as iterator:
            for child in iterator:
                if len(result) >= ENTRY_LIMIT:
                    raise BusinessError("OBSIDIAN_VIEW_LIMIT", "当前目录超过 20,000 项，请缩小目录范围。", 413)
                path = prefix + "/" + child.name if prefix else child.name
                result.append(self.entry(path, child.stat(follow_symlinks=False)))
        after = os.fstat(fd)
        if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
            raise BusinessError("OBSIDIAN_DIRECTORY_CHANGED", "目录读取期间发生变化，保留上次视图。")
        return sorted(result, key=lambda row: (row["kind"] != "DIRECTORY", row["relative_path"]))

    def walk(self, root_fd: int, prefix: str = "") -> list[dict]:
        result = []
        deadline = time.monotonic() + 5
        def visit(path):
            if time.monotonic() > deadline:
                raise BusinessError("OBSIDIAN_VIEW_LIMIT", "递归读取超过 5 秒，请选择更小的目录。", 413)
            with self.directory(root_fd, path) as current:
                rows = self.rows(current, path)
            result.extend(rows)
            if len(result) > ENTRY_LIMIT:
                raise BusinessError("OBSIDIAN_VIEW_LIMIT", "递归范围超过 20,000 项，请选择更小的目录。", 413)
            for row in rows:
                if row["kind"] == "DIRECTORY" and row["eligible"]:
                    visit(row["relative_path"])
        visit(prefix)
        return result

    def metadata(self, fd: int, path: str) -> dict:
        path = self.path(path)
        parts = path.split("/")
        with self.directory(fd, "/".join(parts[:-1])) as parent:
            return self.entry(path, os.stat(parts[-1], dir_fd=parent, follow_symlinks=False))

    def browse(self, path: str, search: str, offset: int, limit: int):
        self.path(path, directory=True)
        with self.root_fd() as fd:
            if search:
                rows = [row for row in self.walk(fd, path) if search.casefold() in row["relative_path"].casefold()]
            else:
                with self.directory(fd, path) as current:
                    rows = self.rows(current, path)
        return {"root_label": "Obsidian 知识库", "relative_directory": path,
                "items": rows[offset:offset + limit], "total": len(rows),
                "offset": offset, "limit": limit, "complete": True,
                "supported_suffixes": sorted(SUFFIXES), "read_only": True}

    def selection(self, body: SelectionRequest):
        result, skipped = {}, []
        with self.root_fd() as fd:
            for path in body.paths:
                row = self.metadata(fd, path)
                if row["kind"] != "REGULAR" or not row["eligible"]:
                    raise BusinessError("OBSIDIAN_FILE_UNSUPPORTED", path + " 不可导入：" + str(row["skip_reason"]), 422)
                result[path] = row
            for directory in body.directories:
                self.path(directory, directory=True)
                for row in self.walk(fd, directory):
                    if row["kind"] == "REGULAR" and row["eligible"]:
                        result[row["relative_path"]] = row
                    elif row["skip_reason"]:
                        skipped.append(row)
            if len(result) > SELECTION_LIMIT:
                raise BusinessError("OBSIDIAN_SELECTION_LIMIT", "单次选择最多 2,000 份资料，请缩小范围。", 413)
        return {"entries": [result[key] for key in sorted(result)], "skipped": skipped,
                "scope": "FROZEN_SELECTION", "new_files_auto_selected": False}

    def check(self, entries: list[SelectedFile]):
        result = []
        with self.root_fd() as fd:
            for expected in entries:
                self.path(expected.relative_path)
                try:
                    row = self.metadata(fd, expected.relative_path)
                except (FileNotFoundError, NotADirectoryError):
                    result.append({"relative_path": expected.relative_path, "status": "MISSING", "current_signature": None})
                    continue
                except BusinessError as exc:
                    if exc.code not in {"OBSIDIAN_DIRECTORY_MISSING", "OBSIDIAN_DIRECTORY_INVALID"}:
                        raise
                    result.append({"relative_path": expected.relative_path, "status": "MISSING", "current_signature": None})
                    continue
                result.append({"relative_path": expected.relative_path, "current_signature": row["signature"],
                               "status": "UNCHANGED" if row["eligible"] and row["kind"] == "REGULAR" and row["signature"] == expected.signature else "CHANGED"})
        return {"items": result}

    def markdown(self, path: str, offset: int, limit: int):
        path = self.path(path)
        if PurePosixPath(path).suffix.lower() != ".md":
            raise BusinessError("OBSIDIAN_PREVIEW_FORMAT", "本视图只读预览 Markdown；其他格式可选择后导入。", 422)
        helper = self.scanner()
        with self.root_fd() as fd:
            try:
                with helper.open_file(fd, path) as (stream, before, parent, leaf):
                    content = stream.read(FILE_LIMIT + 1)
                    helper.unchanged(stream, before, parent, leaf, path)
            except FileNotFoundError as exc:
                raise BusinessError("OBSIDIAN_PREVIEW_MISSING", "当前笔记已消失；请重新选择文件或重试。", 404) from exc
            except NotADirectoryError as exc:
                raise BusinessError("OBSIDIAN_PREVIEW_INVALID", "当前笔记的上级路径已不再是目录。", 409) from exc
        if len(content) > FILE_LIMIT:
            raise BusinessError("OBSIDIAN_FILE_TOO_LARGE", "单文件超过 50 MiB。", 413)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise BusinessError("OBSIDIAN_ENCODING", "Markdown 不是可读取的 UTF-8 文本。", 422) from exc
        return {"relative_path": path, "text": text[offset:offset + limit],
                "offset": offset, "end": min(offset + limit, len(text)), "total_characters": len(text),
                "signature": self.signature(before), "read_only": True}

    def _require_update_target(self, db, body):
        if not body.document_id:
            raise BusinessError("UPDATE_DOCUMENT_NOT_AVAILABLE", "更新目标缺失，请重新读取资料状态。", 422)
        if len(body.entries) != 1:
            raise BusinessError("UPDATE_SINGLE_FILE_REQUIRED", "更新一份资料时只能处理登记的这一份笔记。", 422)
        row = db.execute("SELECT document_id,current_raw_version_id FROM documents "
                         "WHERE document_id=? AND kb_id=? AND removed_at IS NULL",
                         (str(body.document_id), str(body.kb_id))).fetchone()
        if row is None:
            raise BusinessError("UPDATE_DOCUMENT_NOT_AVAILABLE", "更新目标不存在、已移除或不属于当前知识库。", 404)
        if body.expected_raw_version_id is not None and row["current_raw_version_id"] != str(body.expected_raw_version_id):
            raise BusinessError("OBSIDIAN_SOURCE_BINDING_CHANGED", "资料版本在检查期间改变，请重新读取当前状态。", 409)
        if body.registered_source_id is not None:
            from .obsidian_state import ObsidianState
            sources = ObsidianState(self)._rows(db, str(body.kb_id), document_id=str(body.document_id), limit=2)
            if (len(sources) != 1 or sources[0]["source_id"] != str(body.registered_source_id)
                    or sources[0]["relative_path"] != body.entries[0].relative_path):
                raise BusinessError("OBSIDIAN_SOURCE_BINDING_CHANGED", "登记的 Obsidian 笔记发生变化，未改为其他文件。", 409)

    def preview(self, instance: str, body: PreviewRequest, expected_file: FileItem | None = None):
        if not body.entries or len({item.relative_path for item in body.entries}) != len(body.entries):
            raise BusinessError("OBSIDIAN_SELECTION_INVALID", "请选择不重复的实际文件。", 422)
        if body.document_id or body.expected_raw_version_id or body.registered_source_id:
            with closing(open_db(self.settings.sqlite)) as db:
                require_instance(db, instance, importing=False)
                self._require_update_target(db, body)
        helper, files, pending, assets, dependencies = self.scanner(), [], [], [], {}
        with self.root_fd() as fd:
            for expected in body.entries:
                row = self.metadata(fd, expected.relative_path)
                if not row["eligible"] or row["kind"] != "REGULAR" or row["signature"] != expected.signature:
                    raise BusinessError("OBSIDIAN_SELECTION_CHANGED", expected.relative_path + " 已改变，请重新核对所选文件。")
                item, refs = helper._item(fd, str(self.root), expected.relative_path)
                if self.metadata(fd, expected.relative_path)["signature"] != expected.signature:
                    raise BusinessError("OBSIDIAN_SELECTION_CHANGED", expected.relative_path + " 读取期间改变。")
                if expected_file is not None and any(item[key] != getattr(expected_file, key)
                                                      for key in ("filename", "relative_path", "size_bytes", "sha256")):
                    raise BusinessError("DIRECTORY_UPDATE_FILE_CHANGED", "所选目录文件与本机选择的字节不一致，请重新选择并核对。", 409)
                if body.document_id:
                    item["document_id"] = str(body.document_id)
                files.append(item)
                pending.extend((expected.relative_path, syntax, reference) for syntax, reference in refs)
            images = {row["relative_path"] for row in self.walk(fd)
                      if row["kind"] == "REGULAR" and row["eligible"] and PurePosixPath(row["filename"]).suffix.lower() in IMAGE_SUFFIXES} if pending else set()
            relations = []
            for note, syntax, reference in pending:
                relation = helper._relation(note, syntax, reference, images)
                if relation is None:
                    continue
                relations.append(relation)
                if relation["status"] in {"MISSING", "AMBIGUOUS", "OUTSIDE_ROOT"}:
                    raise BusinessError("OBSIDIAN_IMAGE_UNRESOLVED", note + " 的本地图片无法确定：" + reference + "（" + relation["status"] + "）", 422)
                if relation["status"] == "RESOLVED":
                    path = relation["resolved_relative_path"]
                    row = self.metadata(fd, path)
                    if path not in dependencies:
                        item, _refs = helper._item(fd, str(self.root), path)
                        if self.metadata(fd, path)["signature"] != row["signature"]:
                            raise BusinessError("OBSIDIAN_SELECTION_CHANGED", "关联图片在读取期间改变。")
                        dependencies[path] = {**row, "sha256": item["sha256"]}
                    dep = dependencies[path]
                    asset = {"markdown_relative_path": note, "relative_path": path,
                             "size_bytes": dep["size_bytes"], "sha256": dep["sha256"]}
                    if asset not in assets:
                        assets.append(asset)
            weights = {item["relative_path"]: item["size_bytes"] + sum(asset["size_bytes"] for asset in assets if asset["markdown_relative_path"] == item["relative_path"]) for item in files}
            if any(size > BATCH_LIMIT for size in weights.values()):
                raise BusinessError("OBSIDIAN_ITEM_CLOSURE_LIMIT", "单份资料连同图片超过 500 MiB，不能拆开其图片依赖。", 413)
            request = DirectoryPreviewRequest.model_validate({
                "kb_id": str(body.kb_id), "absolute_root": str(self.root), "scope": "SELECTION",
                "files": files, "skipped": [], "image_relations": relations,
                "local_asset_snapshots": assets,
            })
            if len(json_bytes(request.model_dump(mode="json"))) > 4 * 1024 * 1024:
                raise BusinessError("OBSIDIAN_MANIFEST_LIMIT", "确认清单超过 4 MiB，请减少选择。", 413)
            with closing(open_db(self.settings.sqlite)) as db:
                if body.document_id or body.expected_raw_version_id or body.registered_source_id:
                    self._require_update_target(db, body)
                result = IngestionStore(db, self.settings).preview(instance, request, importing=False)
        return {**result, "item_bytes": weights, "dependency_entries": list(dependencies.values()),
                "asset_copy_bytes": sum(asset["size_bytes"] for asset in assets),
                "remote_images_fetched": False}

    def copy_file(self, fd, expected: dict, destination: Path):
        helper, digest, count = self.scanner(), hashlib.sha256(), 0
        with helper.open_file(fd, expected["relative_path"]) as (source, before, parent, leaf):
            with destination.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    count += len(chunk)
                    if count > FILE_LIMIT:
                        raise BusinessError("OBSIDIAN_FILE_TOO_LARGE", "读取中的文件超过 50 MiB。", 413)
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            helper.unchanged(source, before, parent, leaf, expected["relative_path"])
        if (digest.hexdigest(), count) != (expected["sha256"], expected["size_bytes"]):
            raise BusinessError("OBSIDIAN_CONFIRMED_BYTES_CHANGED", expected["relative_path"] + " 已不等于确认字节；保留失败记录，请重新预览。", 422)
        destination.chmod(0o444)
        return digest.hexdigest(), count

    def receive(self, instance: str, batch_id: str, item_id: str):
        with closing(open_db(self.settings.sqlite)) as db:
            store = IngestionStore(db, self.settings)
            require_instance(db, instance)
            batch = store.batch_status(batch_id)
            if batch["data_instance_id"] != instance or not batch["preview_id"]:
                raise BusinessError("OBSIDIAN_BATCH_INVALID", "批次不属于当前实例或不是选择导入。")
            item = next((item for item in batch["items"] if item["item_id"] == item_id), None)
            if item is None:
                raise BusinessError("ITEM_NOT_AVAILABLE", "条目不属于该批次。", 404)
            preview = store.read_preview(batch["preview_id"])
            if preview["manifest"].get("scope") != "SELECTION" or preview["absolute_path"] != str(self.root):
                raise BusinessError("OBSIDIAN_BATCH_INVALID", "该批次未绑定固定 Obsidian 根。")
            if item["state"] == "ACCEPTED":
                return {"item_id": item_id, "task_id": item["task_id"], "already_accepted": True}
            assets = [asset for asset in preview["manifest"].get("local_asset_snapshots", [])
                      if asset["markdown_relative_path"] == item["metadata"]["relative_path"]]
            needed = item["metadata"]["size_bytes"] + sum(asset["size_bytes"] for asset in assets)
            if shutil.disk_usage(self.settings.home).free < 4 * needed + BATCH_LIMIT:
                raise BusinessError("DISK_BUDGET_EXCEEDED", "本地暂存与归档空间不足。", 413)
            upload_id = new_id()
            store.reserve_upload(instance, item_id, upload_id)
            directory = self.settings.home / "tmp/uploads" / upload_id
            try:
                make_directory(directory, self.settings.home, exclusive=True)
                with self.root_fd() as fd:
                    digest, size = self.copy_file(fd, item["metadata"], directory / "input.bin")
                    if assets:
                        make_directory(directory / "resources", self.settings.home, exclusive=True)
                    copied = set()
                    for asset in assets:
                        member = asset_member(asset)
                        if member not in copied:
                            self.copy_file(fd, asset, directory / member)
                            copied.add(member)
                return store.accept_upload(instance, item_id, upload_id, directory / "input.bin", digest, size)
            except BaseException as exc:
                store.upload_failed(item_id, upload_id, {
                    "code": getattr(exc, "code", "OBSIDIAN_RECEIVE_FAILED"), "message": str(exc),
                    "temporary_directory": str(directory), "stage": "LOCAL_SNAPSHOT_BEFORE_QUEUE",
                })
                raise


def create_router(settings: Settings, root: Path = VAULT_ROOT) -> APIRouter:
    from .obsidian_state import ObsidianState
    from .import_workflow import ImportWorkflow
    router = APIRouter(tags=["Obsidian"])
    vault = Vault(settings, root)
    registered = ObsidianState(vault)
    workflow = ImportWorkflow(settings, vault)
    router.workflow_engine = workflow
    router.registered_sources = registered

    @router.post("/obsidian/workflows")
    def workflow_create(body: WorkflowRequest, instance: str = Header(alias="X-RAG-Data-Instance"),
                        key: str = Header(alias="X-Request-ID")):
        if body.confirmed is not True:
            raise BusinessError("WORKFLOW_CONFIRMATION_REQUIRED", "请先核对所选笔记和图片。", 422)
        return workflow.create(instance, key, str(body.kb_id), str(body.preview_id), body.preview_sha256)

    @router.get("/obsidian/workflows")
    def workflow_latest(kb_id: UUID4, request_key: UUID4 | None = None,
                        instance: str = Header(alias="X-RAG-Data-Instance")):
        return workflow.latest(instance, str(kb_id), str(request_key) if request_key else None)

    @router.get("/obsidian/workflows/{workflow_id}")
    def workflow_read(workflow_id: UUID4, offset: int = Query(default=0, ge=0),
                      instance: str = Header(alias="X-RAG-Data-Instance")):
        return workflow.read(instance, str(workflow_id), offset)

    @router.post("/obsidian/workflows/{workflow_id}/actions")
    def workflow_action(workflow_id: UUID4, body: WorkflowAction,
                        instance: str = Header(alias="X-RAG-Data-Instance")):
        return workflow.action(instance, str(workflow_id), body.action, body.ready_sha256)

    def readable(instance):
        with closing(open_db(settings.sqlite)) as db:
            require_instance(db, instance, importing=False)

    @router.get("/obsidian/changes")
    def changes(kb_id: UUID4, instance: str = Header(alias="X-RAG-Data-Instance"),
                cursor: UUID4 | None = None, limit: int = Query(default=25, ge=1, le=50)):
        return registered.changes(instance, str(kb_id), str(cursor) if cursor else "", limit)

    @router.get("/documents/{document_id}/obsidian-target")
    def obsidian_target(document_id: UUID4, instance: str = Header(alias="X-RAG-Data-Instance"),
                        raw_version_id: UUID4 | None = None, fresh: bool = False):
        return registered.target(instance, str(document_id), str(raw_version_id) if raw_version_id else None, fresh=fresh)

    @router.get("/obsidian/tree")
    def tree(instance: str = Header(alias="X-RAG-Data-Instance"), path: str = "",
             search: str = Query(default="", max_length=200), offset: int = Query(default=0, ge=0),
             limit: int = Query(default=100, ge=1, le=200)):
        readable(instance)
        return vault.browse(path, search, offset, limit)

    @router.post("/obsidian/selection")
    def selection(body: SelectionRequest, instance: str = Header(alias="X-RAG-Data-Instance")):
        readable(instance)
        return vault.selection(body)

    @router.post("/obsidian/check")
    def check(body: CheckRequest, instance: str = Header(alias="X-RAG-Data-Instance")):
        readable(instance)
        return vault.check(body.entries)

    @router.get("/obsidian/markdown")
    def markdown(path: str, instance: str = Header(alias="X-RAG-Data-Instance"),
                 offset: int = Query(default=0, ge=0), limit: int = Query(default=8000, ge=100, le=16000)):
        readable(instance)
        return vault.markdown(path, offset, limit)

    @router.post("/obsidian/preview")
    def preview(body: PreviewRequest, instance: str = Header(alias="X-RAG-Data-Instance")):
        with closing(open_db(settings.sqlite)) as db:
            require_instance(db, instance, importing=False)
            require_kb(db, body.kb_id)
        return vault.preview(instance, body)

    @router.post("/obsidian/batches/{batch_id}/items/{item_id}/receive")
    def receive(batch_id: UUID4, item_id: UUID4, instance: str = Header(alias="X-RAG-Data-Instance")):
        return vault.receive(instance, str(batch_id), str(item_id))

    return router

def create_directory_update_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/directory-update", tags=["Directory update"])

    @router.post("/preview")
    def preview(body: DirectoryUpdateRequest, instance: str = Header(alias="X-RAG-Data-Instance")):
        with closing(open_db(settings.sqlite)) as db:
            require_instance(db, instance)
            require_kb(db, body.kb_id)
        if body.file.relative_path is None or (body.file.document_id is not None and body.file.document_id != body.document_id):
            raise BusinessError("DIRECTORY_UPDATE_TARGET_INVALID", "请选择目录中的一个文件并保留当前更新目标。", 422)
        vault = Vault(settings, Path(canonical_root(body.absolute_root)))
        with vault.root_fd() as fd:
            row = vault.metadata(fd, body.file.relative_path)
        request = PreviewRequest(kb_id=body.kb_id, document_id=body.document_id,
                                 entries=[SelectedFile(relative_path=body.file.relative_path, signature=row["signature"])])
        return vault.preview(instance, request, expected_file=body.file)

    @router.post("/batches/{batch_id}/items/{item_id}/receive")
    def receive(batch_id: UUID4, item_id: UUID4, instance: str = Header(alias="X-RAG-Data-Instance")):
        with closing(open_db(settings.sqlite)) as db:
            require_instance(db, instance)
            store = IngestionStore(db, settings)
            batch = store.batch_status(str(batch_id))
            if batch["data_instance_id"] != instance or not batch["preview_id"] or len(batch["items"]) != 1:
                raise BusinessError("DIRECTORY_UPDATE_BATCH_INVALID", "更新批次必须属于当前实例且只包含一个文件。", 409)
            preview = store.read_preview(batch["preview_id"])
            files = preview["manifest"]["files"]
            if preview["manifest"]["scope"] != "SELECTION" or len(files) != 1 or not files[0]["document_id"]:
                raise BusinessError("DIRECTORY_UPDATE_BATCH_INVALID", "该预览未绑定单份资料更新。", 409)
            root = Path(preview["absolute_path"])
        return Vault(settings, root).receive(instance, str(batch_id), str(item_id))

    return router
