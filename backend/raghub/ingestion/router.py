"""Loopback ingestion API; every business write keeps the supplied instance."""
from __future__ import annotations
import hashlib
import os
import shutil
from contextlib import closing
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse

from ..config import Settings
from ..db import open_db, system_state, new_id
from ..mode import request_mode
from .archive import make_directory
from .contracts import (BatchRequest, DirectoryPreviewRequest, PreviewConfirmation, URLRequest,
                         RecoveryRequest, ModeRequest, KnowledgeBaseCreate, RootRebind, RenameAssociation,
                         FILE_LIMIT, MULTIPART_ALLOWANCE, MIB)
from .store import BusinessError, IngestionStore
from .web import load_policy, acquisition_config

router = APIRouter(prefix="/api")
settings = Settings.load()

def call_store(method, *args):
    with closing(open_db(settings.sqlite)) as db:
        return getattr(IngestionStore(db, settings), method)(*args)

def disk_gate(expected_bytes):
    free = shutil.disk_usage(settings.home).free
    needed = 4 * expected_bytes + 500 * MIB
    if free < needed:
        raise BusinessError("DISK_SPACE_LOW", "磁盘空间不足，停止新接收；已接收现场保留。", 507)
    return {"free_bytes_observed": free, "required_bytes": needed, "reserve_bytes": 500 * MIB}

@router.get("/state")
def state():
    with closing(open_db(settings.sqlite)) as db:
        value = dict(system_state(db))
        value["intake_open"] = value["mode"] == "IMPORT" and value["verification_status"] == "VERIFIED" and bool(value["dispatch_open"])
        active = db.execute("SELECT workflow_id,kb_id,state FROM import_workflows WHERE data_instance_id=? "
                            "AND state NOT IN ('COMPLETE','PARTIAL_COMPLETE','CANCELLED') LIMIT 1",
                            (value["data_instance_id"],)).fetchone()
        value["import_workflow"] = dict(active) if active else None
        value["processing_pointer_semantics"] = "PREPARATION_ONLY_NOT_QUERY_PUBLICATION"
        value["publication_count_semantics"] = "RESERVATION_ATTEMPT_BUDGET_NOT_PHYSICAL_SEND_COUNT"
        return value

@router.post("/knowledge-bases")
def create_kb(body: KnowledgeBaseCreate, instance: str = Header(alias="X-RAG-Data-Instance")):
    return call_store("create_kb", instance, body.name)

@router.post("/import/directory/preview")
def directory_preview(body: DirectoryPreviewRequest, instance: str = Header(alias="X-RAG-Data-Instance")):
    return call_store("preview", instance, body)

@router.get("/import/directory/previews/{preview_id}")
def preview_status(preview_id: UUID):
    return call_store("preview_status", str(preview_id))

@router.post("/import/directory/previews/{preview_id}/confirm")
def confirm_preview(preview_id: UUID, body: PreviewConfirmation, instance: str = Header(alias="X-RAG-Data-Instance")):
    return call_store("confirm_preview", instance, str(preview_id), body.preview_sha256)

@router.post("/import/batches")
def create_batch(body: BatchRequest, instance: str = Header(alias="X-RAG-Data-Instance"), request_key: str = Header(alias="X-Request-ID")):
    disk_gate(sum(item.size_bytes for item in body.items))
    return call_store("create_batch", instance, request_key, body)

@router.get("/import/batches/by-request/{request_key}")
def request_status(request_key: UUID):
    return call_store("request_status", str(request_key))

@router.get("/import/batches/{batch_id}")
def batch_status(batch_id: UUID):
    return call_store("batch_status", str(batch_id))

@router.post("/import/batches/{batch_id}/items/{item_id}/file")
async def upload(batch_id: UUID, item_id: UUID, request: Request, instance: str = Header(alias="X-RAG-Data-Instance")):
    batch = call_store("batch_status", str(batch_id))
    if not any(item["item_id"] == str(item_id) for item in batch["items"]):
        raise BusinessError("BATCH_ITEM_MISMATCH", "条目不属于该批次。", 404)
    upload_id = new_id()
    reserved = call_store("reserve_upload", instance, str(item_id), upload_id)
    if reserved["already_accepted"]:
        task = call_store("task_status", reserved["task_id"])
        return {"task_id": task["task_id"], "attempt_id": task["current_attempt_id"], "already_accepted": True}
    metadata = reserved["metadata"]
    directory = settings.home / "tmp/uploads" / upload_id
    try:
        disk_gate(metadata["size_bytes"])
        make_directory(directory, settings.home, exclusive=True)
        async with request.form(max_files=1, max_fields=0, max_part_size=MULTIPART_ALLOWANCE) as form:
            parts = form.multi_items()
            if len(parts) != 1 or parts[0][0] != "file" or not isinstance(parts[0][1], UploadFile):
                raise BusinessError("UPLOAD_PARTS_INVALID", "每个请求须且仅须一个 file。", 422)
            incoming = parts[0][1]
            if incoming.filename != metadata["filename"]:
                raise BusinessError("UPLOAD_FILENAME_MISMATCH", "文件名与确认清单不一致。", 422)
            target = directory / "input.bin"
            digest, size = hashlib.sha256(), 0
            with target.open("xb") as handle:
                while chunk := await incoming.read(256 * 1024):
                    size += len(chunk)
                    if size > FILE_LIMIT or size > metadata["size_bytes"]:
                        raise BusinessError("UPLOAD_BYTE_LIMIT", "实际文件超过受理大小。", 413)
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o444)
            return call_store("accept_upload", instance, str(item_id), upload_id, target, digest.hexdigest(), size)
    except BaseException as exc:
        call_store("upload_failed", str(item_id), upload_id, {"code": getattr(exc, "code", "UPLOAD_FAILED"),
                   "error_type": type(exc).__name__, "message": str(exc), "temporary_directory": str(directory)})
        raise

@router.post("/import/urls")
def submit_url(body: URLRequest, instance: str = Header(alias="X-RAG-Data-Instance"), request_key: str = Header(alias="X-Request-ID")):
    disk_gate(110 * MIB)
    policy = load_policy(settings.project / "configs/parsing/sites.json")
    return call_store("submit_url", instance, request_key, body, acquisition_config(policy, body.profile))

@router.get("/tasks/{task_id}")
def task_status(task_id: UUID):
    return call_store("task_status", str(task_id))

@router.post("/tasks/{task_id}/cancel")
def cancel(task_id: UUID, instance: str = Header(alias="X-RAG-Data-Instance")):
    return call_store("cancel", instance, str(task_id))

@router.post("/tasks/{task_id}/recover")
def recover(task_id: UUID, body: RecoveryRequest, instance: str = Header(alias="X-RAG-Data-Instance")):
    return call_store("recover", instance, str(task_id), str(body.expected_attempt_id))

@router.post("/mode")
def mode(body: ModeRequest, instance: str = Header(alias="X-RAG-Data-Instance")):
    return request_mode(settings, instance, body)

@router.post("/import/source-roots/{root_id}/rebind")
def rebind(root_id: UUID, body: RootRebind, instance: str = Header(alias="X-RAG-Data-Instance")):
    return call_store("rebind_root", instance, str(root_id), body)

@router.post("/import/directory/associate-rename")
def associate(body: RenameAssociation, instance: str = Header(alias="X-RAG-Data-Instance")):
    return call_store("associate_rename", instance, body)

class IntakeBodyLimit:
    """Count ASGI body chunks before the multipart parser writes its spool."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            return await self.app(scope, receive, send)
        limit = FILE_LIMIT + MULTIPART_ALLOWANCE
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"code": "CONTENT_LENGTH_INVALID"}, status_code=400)(scope, receive, send)
        if declared < 0 or declared > limit:
            return await JSONResponse({"code": "REQUEST_BODY_LIMIT"}, status_code=413)(scope, receive, send)
        received = 0
        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="request body limit exceeded")
            return message
        return await self.app(scope, bounded_receive, send)
