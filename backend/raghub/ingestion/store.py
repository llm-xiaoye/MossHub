"""Business transactions and current-owner fences for acquisition."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import psutil

from .archive import inside, json_hash, uuid_text, verify_archive
from .contracts import BatchRequest, DirectoryPreviewRequest, URLRequest, url_source_key
from ..config import Settings
from ..dispatch_gate import send_gate
from ..db import IdentityStore, new_id, now, system_state, transaction


class BusinessError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def event(db, task_id, attempt_id, kind, details):
    db.execute(
        "INSERT INTO task_events(task_id,attempt_id,event_type,details_json,created_at) VALUES (?,?,?,?,?)",
        (task_id, attempt_id, kind, json.dumps(details, ensure_ascii=False, sort_keys=True), now()),
    )


def require_instance(db, supplied: str, *, importing: bool = True):
    state = system_state(db)
    if supplied != state["data_instance_id"]:
        raise BusinessError("DATA_INSTANCE_MISMATCH", "数据实例已改变，请核对原提交记录后确认新工作。")
    if state["verification_status"] != "VERIFIED" or not state["dispatch_open"]:
        raise BusinessError("RECOVERY_CLOSED", "恢复尚未核验完成，普通业务写入关闭。")
    if importing and state["mode"] != "IMPORT":
        raise BusinessError("IMPORT_REQUIRED", "请切换到导入模式并等待已有工作收尾。")

    if importing and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_deletions'").fetchone():
        if db.execute("SELECT 1 FROM document_deletions WHERE state='CLEANING' LIMIT 1").fetchone():
            raise BusinessError("DELETION_CLEANUP_ACTIVE", "正在清理已删除资料的本机副本，请稍后再导入。")
    return state


def require_kb(db, kb_id):
    row = db.execute("SELECT * FROM knowledge_bases WHERE kb_id=?", (str(kb_id),)).fetchone()
    if row is None:
        raise BusinessError("KNOWLEDGE_BASE_NOT_FOUND", "知识库不存在。", 404)
    return row


def require_document(db, kb_id, document_id):
    row = db.execute("SELECT * FROM documents WHERE kb_id=? AND document_id=?", (kb_id, document_id)).fetchone()
    if row is None or row["removed_at"]:
        raise BusinessError("DOCUMENT_NOT_AVAILABLE", "目标文档不存在或已移除。", 404)
    return row


def canonical_root(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise BusinessError("SOURCE_ROOT_INVALID", "来源目录须为实际存在且无符号链接别名的绝对目录。", 422)
    return str(path)


class IngestionStore:
    def __init__(self, db: sqlite3.Connection, settings: Settings):
        self.db, self.settings = db, settings

    def create_kb(self, instance: str, name: str) -> dict:
        with transaction(self.db):
            require_instance(self.db, instance)
            kb_id = new_id()
            self.db.execute("INSERT INTO knowledge_bases(kb_id,name,created_at) VALUES (?,?,?)",
                            (kb_id, name, now()))
            return {"kb_id": kb_id, "name": name}

    def preview(self, instance: str, request: DirectoryPreviewRequest, *, importing: bool = True) -> dict:
        payload = request.model_dump(mode="json")
        root_path = canonical_root(request.absolute_root)
        with transaction(self.db):
            require_instance(self.db, instance, importing=importing)
            kb_id = str(request.kb_id)
            require_kb(self.db, kb_id)
            if request.source_root_id:
                root = self.db.execute("SELECT * FROM source_roots WHERE source_root_id=? AND kb_id=?",
                                       (str(request.source_root_id), kb_id)).fetchone()
                if root is None or root["absolute_path"] != root_path:
                    raise BusinessError("SOURCE_ROOT_REBIND_REQUIRED", "目录位置改变，请显式重新绑定原来源目录。")
            else:
                root = self.db.execute("SELECT * FROM source_roots WHERE kb_id=? AND absolute_path=?",
                                       (kb_id, root_path)).fetchone()
                if root is None:
                    root_id = new_id()
                    self.db.execute("INSERT INTO source_roots(source_root_id,kb_id,absolute_path,created_at) VALUES (?,?,?,?)",
                                    (root_id, kb_id, root_path, now()))
                    root = self.db.execute("SELECT * FROM source_roots WHERE source_root_id=?", (root_id,)).fetchone()
            known = self.db.execute(
                "SELECT s.relative_path,s.document_id,r.sha256 FROM document_sources s "
                "LEFT JOIN documents d ON d.document_id=s.document_id "
                "LEFT JOIN raw_versions r ON r.raw_version_id=d.current_raw_version_id "
                "WHERE s.source_root_id=?", (root["source_root_id"],),
            ).fetchall()
            by_path = {row["relative_path"]: dict(row) for row in known}
            observed = {item.relative_path for item in request.files}
            payload["source_root_id"] = root["source_root_id"]
            digest = json_hash(payload)
            preview_id = new_id()
            self.db.execute(
                "INSERT INTO import_previews(preview_id,data_instance_id,kb_id,source_root_id,root_binding_version,"
                "manifest_sha256,manifest_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (preview_id, instance, kb_id, root["source_root_id"], root["binding_version"],
                 digest, json.dumps(payload, ensure_ascii=False, sort_keys=True), now()),
            )
            return {
                "preview_id": preview_id, "preview_sha256": digest, "source_root_id": root["source_root_id"],
                "manifest": payload, "matches": [by_path[path] for path in sorted(observed & by_path.keys())],
                "missing": [] if request.scope == "SELECTION" else [by_path[path] for path in sorted(by_path.keys() - observed)],
                "missing_policy": "REPORT_ONLY_NO_AUTOMATIC_REMOVAL",
                "file_count": len(request.files), "size_bytes": sum(item.size_bytes for item in request.files),
                "confirmation_required": True,
            }

    def read_preview(self, preview_id: str) -> dict:
        row = self.db.execute(
            "SELECT p.*,r.absolute_path,r.binding_version FROM import_previews p "
            "JOIN source_roots r ON r.source_root_id=p.source_root_id WHERE p.preview_id=?", (preview_id,),
        ).fetchone()
        if row is None:
            raise BusinessError("PREVIEW_NOT_FOUND", "预览不存在。", 404)
        payload = json.loads(row["manifest_json"])
        if json_hash(payload) != row["manifest_sha256"]:
            raise BusinessError("PREVIEW_INVALID", "预览记录已损坏。")
        return {**dict(row), "manifest": payload}

    def confirm_preview(self, instance: str, preview_id: str, digest: str) -> dict:
        with transaction(self.db):
            require_instance(self.db, instance)
            row = self.read_preview(preview_id)
            if row["data_instance_id"] != instance or row["manifest_sha256"] != digest:
                raise BusinessError("PREVIEW_MISMATCH", "预览与本次确认不一致，请重新查看。")
            if row["root_binding_version"] != row["binding_version"]:
                raise BusinessError("SOURCE_ROOT_CHANGED", "来源目录绑定已改变，请重新预览。")
            self.db.execute("UPDATE import_previews SET confirmed_at=COALESCE(confirmed_at,?) WHERE preview_id=?",
                            (now(), preview_id))
            return {"preview_id": preview_id, "confirmed": True}

    def create_batch(self, instance: str, request_key: str, request: BatchRequest) -> dict:
        uuid_text(request_key)
        payload = request.model_dump(mode="json")
        digest, kb_id = json_hash(payload), str(request.kb_id)
        with transaction(self.db):
            require_instance(self.db, instance)
            require_kb(self.db, kb_id)
            old = self.db.execute("SELECT * FROM import_batches WHERE data_instance_id=? AND request_key=?",
                                  (instance, request_key)).fetchone()
            if old:
                if old["request_sha256"] != digest:
                    raise BusinessError("IDEMPOTENCY_CONFLICT", "同一提交键关联了不同内容。")
                return self.batch_status(old["batch_id"])
            if request.preview_id:
                preview = self.read_preview(str(request.preview_id))
                if (preview["data_instance_id"] != instance or preview["kb_id"] != kb_id
                        or preview["manifest_sha256"] != request.preview_sha256 or not preview["confirmed_at"]
                        or preview["root_binding_version"] != preview["binding_version"]):
                    raise BusinessError("PREVIEW_NOT_CONFIRMED", "目录预览尚未确认或已失效。")
                allowed = {item["relative_path"]: item for item in preview["manifest"]["files"]}
                for item in payload["items"]:
                    if allowed.get(item["relative_path"]) != item:
                        raise BusinessError("PREVIEW_FILE_CHANGED", "批次文件与已确认清单不一致。")
                from .contracts import BATCH_LIMIT
                selected_paths = {item["relative_path"] for item in payload["items"]}
                asset_bytes = sum(asset["size_bytes"] for asset in preview["manifest"].get("local_asset_snapshots", [])
                                  if asset["markdown_relative_path"] in selected_paths)
                if sum(item.size_bytes for item in request.items) + asset_bytes > BATCH_LIMIT:
                    raise BusinessError("BATCH_BYTES_EXCEEDED", "批次连同各份资料的图片副本超过 500 MiB。", 413)
                existing = self.db.execute("SELECT i.metadata_json FROM import_items i JOIN import_batches b "
                                           "ON b.batch_id=i.batch_id WHERE b.preview_id=?", (str(request.preview_id),)).fetchall()
                reserved_paths = {json.loads(row[0])["relative_path"] for row in existing}
                if any(item["relative_path"] in reserved_paths for item in payload["items"]):
                    raise BusinessError("PREVIEW_PATH_ALREADY_RESERVED", "这些文件已有批次记录，请先核对原批次，不重复预留。")
            batch_id = new_id()
            self.db.execute(
                "INSERT INTO import_batches(batch_id,data_instance_id,kb_id,request_key,request_sha256,"
                "preview_id,expected_bytes,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, instance, kb_id, request_key, digest, str(request.preview_id) if request.preview_id else None,
                 sum(item.size_bytes for item in request.items) + (asset_bytes if request.preview_id else 0), now()),
            )
            for ordinal, item in enumerate(payload["items"]):
                self.db.execute(
                    "INSERT INTO import_items(item_id,batch_id,ordinal,metadata_json,state) VALUES (?,?,?,?,'AWAITING_UPLOAD')",
                    (new_id(), batch_id, ordinal, json.dumps(item, ensure_ascii=False, sort_keys=True)),
                )
            return self.batch_status(batch_id)

    def batch_status(self, batch_id: str) -> dict:
        batch = self.db.execute("SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise BusinessError("BATCH_NOT_FOUND", "导入批次不存在。", 404)
        items = self.db.execute("SELECT * FROM import_items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        return {
            **dict(batch), "items": [
                {**dict(row), "metadata": json.loads(row["metadata_json"]),
                 "error": json.loads(row["error_json"]) if row["error_json"] else None}
                for row in items
            ],
        }

    def reserve_upload(self, instance: str, item_id: str, upload_id: str) -> dict:
        with transaction(self.db):
            require_instance(self.db, instance)
            row = self.db.execute(
                "SELECT i.*,b.data_instance_id,b.kb_id,b.preview_id FROM import_items i "
                "JOIN import_batches b ON b.batch_id=i.batch_id WHERE i.item_id=?", (item_id,),
            ).fetchone()
            if row is None or row["data_instance_id"] != instance:
                raise BusinessError("ITEM_NOT_AVAILABLE", "上传条目不存在或属于其他实例。", 404)
            if row["state"] == "ACCEPTED":
                return {**dict(row), "metadata": json.loads(row["metadata_json"]), "already_accepted": True}
            if row["state"] == "RECEIVING":
                raise BusinessError("UPLOAD_IN_PROGRESS", "该文件正在接收；读取原状态，不重复上传。")
            if row["state"] == "FAILED":
                raise BusinessError("UPLOAD_FAILED", "原上传已失败；请以新批次重新提交，保留旧现场。")
            if row["preview_id"]:
                preview = self.read_preview(row["preview_id"])
                if not preview["confirmed_at"] or preview["root_binding_version"] != preview["binding_version"]:
                    raise BusinessError("PREVIEW_INVALID", "目录预览已失效，请重新预览确认。")
            self.db.execute("UPDATE import_items SET state='RECEIVING',upload_id=? WHERE item_id=?",
                            (upload_id, item_id))
            return {**dict(row), "metadata": json.loads(row["metadata_json"]), "already_accepted": False}

    def upload_failed(self, item_id: str, upload_id: str, error: dict) -> None:
        with transaction(self.db):
            self.db.execute(
                "UPDATE import_items SET state='FAILED',error_json=? "
                "WHERE item_id=? AND upload_id=? AND state='RECEIVING'",
                (json.dumps(error, ensure_ascii=False, sort_keys=True), item_id, upload_id),
            )

    def source_for(self, kb_id: str, kind: str, metadata: dict, *, root_id=None, source_key=None) -> tuple[str, str]:
        supplied = metadata.get("document_id")
        source = None
        if kind == "DIRECTORY":
            source = self.db.execute("SELECT * FROM document_sources WHERE kb_id=? AND source_root_id=? AND relative_path=?",
                                     (kb_id, root_id, metadata["relative_path"])).fetchone()
        elif kind == "URL":
            source = self.db.execute("SELECT * FROM document_sources WHERE kb_id=? AND source_key=?",
                                     (kb_id, source_key)).fetchone()
        if source and supplied and supplied != source["document_id"]:
            raise BusinessError("SOURCE_DOCUMENT_CONFLICT", "来源已有文档身份，不能覆盖其他文档。")
        document_id = source["document_id"] if source else supplied
        if document_id:
            document = require_document(self.db, kb_id, document_id)
            if document["active_prepare_task_id"]:
                active = self.db.execute("SELECT status FROM tasks WHERE task_id=?", (document["active_prepare_task_id"],)).fetchone()
                if active and active["status"] in {"PENDING", "RUNNING"}:
                    raise BusinessError("DOCUMENT_BUSY", "该文档仍在处理，请等待收尾。")
        else:
            document_id = new_id()
            self.db.execute("INSERT INTO documents(document_id,kb_id,display_name,created_at) VALUES (?,?,?,?)",
                            (document_id, kb_id, metadata.get("filename") or source_key, now()))
        if source is None:
            source_id = new_id()
            self.db.execute(
                "INSERT INTO document_sources(source_id,document_id,kb_id,kind,source_root_id,relative_path,source_key,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (source_id, document_id, kb_id, kind, root_id, metadata.get("relative_path"), source_key, now()),
            )
        else:
            source_id = source["source_id"]
        return document_id, source_id

    def queue_input(self, instance: str, request_key: str, payload: dict) -> dict:
        """Called only inside the producer's BEGIN IMMEDIATE transaction."""
        if not self.db.in_transaction:
            raise RuntimeError("task/input/outbox producer requires one transaction")
        digest = json_hash(payload)
        task_id, attempt_id, outbox_id = new_id(), new_id(), new_id()
        timestamp = now()
        self.db.execute(
            "INSERT INTO tasks(task_id,data_instance_id,kind,request_key,request_sha256,status,current_attempt_id,"
            "stage_budget_scope_id,created_at,updated_at) VALUES (?,?,'DOCUMENT',?,?,'PENDING',NULL,?,?,?)",
            (task_id, instance, request_key, digest, new_id(), timestamp, timestamp),
        )
        self.db.execute("INSERT INTO attempts(attempt_id,task_id,data_instance_id,state,created_at) VALUES (?,?,?,'PENDING',?)",
                        (attempt_id, task_id, instance, timestamp))
        self.db.execute("UPDATE tasks SET current_attempt_id=? WHERE task_id=?", (attempt_id, task_id))
        self.db.execute(
            "INSERT INTO outbox(outbox_id,task_id,attempt_id,data_instance_id,state,created_at) VALUES (?,?,?,?,'PENDING',?)",
            (outbox_id, task_id, attempt_id, instance, timestamp),
        )
        self.db.execute(
            "INSERT INTO task_inputs(task_id,kb_id,document_id,processing_version_id,source_id,payload_json) VALUES (?,?,?,?,?,?)",
            (task_id, payload["kb_id"], payload["document_id"], payload["processing_version_id"],
             payload["source_id"], json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        self.db.execute("UPDATE documents SET active_prepare_task_id=? WHERE document_id=?",
                        (task_id, payload["document_id"]))
        event(self.db, task_id, attempt_id, "ACCEPTED", {"outbox_id": outbox_id, "scope": "ACQUISITION"})
        return {"task_id": task_id, "attempt_id": attempt_id, "outbox_id": outbox_id,
                "document_id": payload["document_id"], "data_instance_id": instance}

    @staticmethod
    def file_format(filename):
        suffix=Path(filename).suffix.lower()
        return {".md":"md",".markdown":"md",".html":"html",".htm":"html",
                ".docx":"docx",".xlsx":"xlsx",".csv":"csv",".json":"json",
                ".pdf":"pdf",".png":"image",".jpg":"image",".jpeg":"image",
                ".tif":"image",".tiff":"image",".webp":"image"}.get(suffix)

    def file_input_identity(self,metadata):
        from ..parsing_assets import parser_identity
        kind=self.file_format(metadata["filename"])
        if kind is None:
            return None
        try:
            policy=parser_identity(self.settings)
        except OSError:
            # Acquisition remains available before parser assets are prepared.
            # Unknown processing identity never qualifies for automatic reuse.
            return None
        return {"policy":"EXACT_NORMAL_FILE_INPUT_V1",
                "sha256":metadata["sha256"],"size_bytes":metadata["size_bytes"],
                "format":kind,"assets":[],
                "acquisition_config":{"schema_version":1,"rule":"local-archive-v1"},
                "processing_policy":policy}

    def reuse_file_input(self,instance,row,metadata,input_identity):
        """Resolve only inside the upload producer transaction; claims are hints."""
        if not self.db.in_transaction:
            raise RuntimeError("file reuse requires the upload transaction")
        fingerprint=json_hash(input_identity)
        key=(instance,row["kb_id"],fingerprint)
        claim=self.db.execute("SELECT * FROM file_input_claims WHERE data_instance_id=? AND kb_id=? AND fingerprint=?",key).fetchone()
        if claim is None:
            return None
        if json.loads(claim["input_json"])!=input_identity:
            raise BusinessError("FILE_FINGERPRINT_CONFLICT","重复资料记录与实际输入不一致，请检查资料状态。")
        def stale():
            self.db.execute("DELETE FROM file_input_claims WHERE data_instance_id=? AND kb_id=? AND fingerprint=?",key)
            return None
        doc=self.db.execute("SELECT * FROM documents WHERE document_id=? AND kb_id=? AND removed_at IS NULL",
                            (claim["document_id"],row["kb_id"])).fetchone()
        source=self.db.execute("SELECT * FROM document_sources WHERE source_id=? AND document_id=? AND kind='FILE'",
                               (claim["source_id"],claim["document_id"])).fetchone()
        rooted=self.db.execute("SELECT 1 FROM document_sources WHERE document_id=? AND kind!='FILE' LIMIT 1",
                               (claim["document_id"],)).fetchone()
        if doc is None or source is None or rooted is not None:
            return stale()
        if doc["active_prepare_task_id"] and doc["active_prepare_task_id"]!=claim["task_id"]:
            return stale()
        task=self.db.execute("SELECT * FROM tasks WHERE task_id=? AND data_instance_id=?",
                             (claim["task_id"],instance)).fetchone()
        if task is None:
            return stale()
        recorded=self.input_for(task["task_id"])
        if (json_hash(recorded)!=task["request_sha256"]
                or recorded.get("file_input_identity")!=input_identity):
            raise BusinessError("FILE_REUSE_INPUT_CHANGED","原任务的冻结输入已改变；本次暂存保留，请先核对原任务状态。")
        raw_id=doc["current_raw_version_id"]
        processing_id=doc["current_processing_version_id"]
        processing_status="NOT_STARTED"
        if raw_id:
            raw=self.db.execute("SELECT * FROM raw_versions WHERE raw_version_id=? AND document_id=?",
                                (raw_id,doc["document_id"])).fetchone()
            if raw is None:
                raise BusinessError("FILE_REUSE_REVIEW_REQUIRED","原件记录不完整；本次上传暂存仍保留，请先核对原资料。")
            archive=verify_archive(Path(raw["archive_manifest_path"]))
            files=archive["files"]
            if (len(files)!=1 or not files[0]["path"].startswith("original.")
                    or files[0]["sha256"]!=input_identity["sha256"]
                    or files[0]["size_bytes"]!=input_identity["size_bytes"]
                    or self.file_format(files[0]["path"])!=input_identity["format"]):
                return stale()
            processing=self.db.execute("SELECT * FROM processing_versions WHERE processing_version_id=? AND raw_version_id=?",
                                       (processing_id,raw_id)).fetchone()
            if processing is None:
                raise BusinessError("FILE_REUSE_REVIEW_REQUIRED","处理版本与原件不一致；本次上传暂存仍保留，请先核对状态。")
            config_row=self.db.execute("SELECT * FROM processing_configs WHERE processing_version_id=?",(processing_id,)).fetchone()
            if config_row:
                config=json.loads(config_row["config_json"])
                if (config.get("format",input_identity["format"])!=input_identity["format"]
                        or json_hash(config)!=processing["config_sha256"]
                        or any(config.get(k)!=v for k,v in input_identity["processing_policy"].items())):
                    return stale()
            elif processing["config_sha256"]!=json_hash(input_identity["acquisition_config"]):
                return stale()
            if self.db.execute("SELECT 1 FROM processing_results WHERE processing_version_id=?",(processing_id,)).fetchone():
                from ..parsing.store import ParsingStore
                ParsingStore(self.db,self.settings).result(processing_id)
            processing_status=processing["status"]
        elif task["status"]=="SUCCEEDED":
            raise BusinessError("FILE_REUSE_REVIEW_REQUIRED","任务结束但没有采用原件；请先读取原任务状态，不能直接当作重复成功。")
        latest=self.db.execute("SELECT details_json FROM source_observations WHERE source_id=? AND raw_version_id IS ? "
                               "ORDER BY created_at DESC,observation_id DESC LIMIT 1",(claim["source_id"],raw_id)).fetchone()
        details=json.loads(latest["details_json"]) if latest else {"kind":"FILE","parsing":"NOT_STARTED"}
        details.update({"provenance":metadata["provenance"],"filename":metadata["filename"],
                        "raw_version_id":raw_id,
                        "duplicate_receipt":{"policy":"EXACT_NORMAL_FILE_INPUT_V1","fingerprint":fingerprint,
                            "import_item_id":row["item_id"],"incoming_metadata":metadata,
                            "associated_task_id":task["task_id"],"processing_version_id":processing_id}})
        self.db.execute("INSERT INTO source_observations(observation_id,source_id,raw_version_id,task_id,details_json,created_at) "
                        "VALUES (?,?,?,?,?,?)",(new_id(),claim["source_id"],raw_id,task["task_id"],
                        json.dumps(details,ensure_ascii=False,sort_keys=True),now()))
        outbox=self.db.execute("SELECT outbox_id FROM outbox WHERE attempt_id=? ORDER BY created_at DESC LIMIT 1",
                               (task["current_attempt_id"],)).fetchone()
        return {"task_id":task["task_id"],"attempt_id":task["current_attempt_id"],
                "outbox_id":outbox["outbox_id"] if outbox else None,
                "document_id":doc["document_id"],"data_instance_id":instance,
                "raw_version_id":raw_id,"processing_version_id":processing_id,
                "processing_status":processing_status,"associated_task_status":task["status"],
                "duplicate_reused":True,"input_fingerprint":fingerprint}

    def accept_upload(self, instance: str, item_id: str, upload_id: str, path: Path, digest: str, size: int) -> dict:
        with transaction(self.db):
            require_instance(self.db, instance)
            row = self.db.execute(
                "SELECT i.*,b.kb_id,b.data_instance_id,b.preview_id,b.received_bytes,b.expected_bytes FROM import_items i "
                "JOIN import_batches b ON b.batch_id=i.batch_id WHERE item_id=?", (item_id,),
            ).fetchone()
            if row is None or row["state"] != "RECEIVING" or row["upload_id"] != upload_id or row["data_instance_id"] != instance:
                raise BusinessError("UPLOAD_OWNER_CHANGED", "上传的接收状态已改变；保留暂存，不采用。")
            metadata = json.loads(row["metadata_json"])
            if (digest, size) != (metadata["sha256"], metadata["size_bytes"]):
                raise BusinessError("UPLOAD_BYTES_MISMATCH", "实际文件与预览的大小或 SHA-256 不一致。", 422)
            if row["received_bytes"] + size > row["expected_bytes"]:
                raise BusinessError("BATCH_BYTES_EXCEEDED", "批次实际接收字节超出确认清单。", 413)
            root_id = None
            kind = "FILE"
            if row["preview_id"]:
                preview = self.read_preview(row["preview_id"])
                if preview["root_binding_version"] != preview["binding_version"]:
                    raise BusinessError("SOURCE_ROOT_CHANGED", "来源目录绑定改变，本次上传不采用。")
                root_id, kind = preview["source_root_id"], "DIRECTORY"
            input_identity=self.file_input_identity(metadata) if kind=="FILE" and not metadata.get("document_id") else None
            if input_identity is not None:
                reused=self.reuse_file_input(instance,row,metadata,input_identity)
                if reused is not None:
                    self.db.execute("UPDATE import_items SET state=\'ACCEPTED\',task_id=?,size_bytes=?,content_sha256=? WHERE item_id=?",
                                    (reused["task_id"],size,digest,item_id))
                    self.db.execute("UPDATE import_batches SET received_bytes=received_bytes+? WHERE batch_id=?",
                                    (size,row["batch_id"]))
                    return {**reused,"item_id":item_id,"archive_status":"REUSED" if reused["raw_version_id"] else "EXISTING_TASK"}
            doc_id, source_id = self.source_for(row["kb_id"], kind, metadata, root_id=root_id)
            payload = {
                "schema_version": 1, "kind": kind, "kb_id": row["kb_id"], "document_id": doc_id,
                "processing_version_id": new_id(), "source_id": source_id,
                "source_root_id": root_id, "metadata": metadata, "upload_path": str(path),
                "acquisition_config": {"schema_version": 1, "rule": "local-archive-v1"},
            }
            received_size = size
            if kind == "DIRECTORY":
                relations = [item for item in preview["manifest"]["image_relations"]
                             if item["markdown_relative_path"] == metadata["relative_path"]]
                file_map = {item["relative_path"]: item for item in preview["manifest"]["files"]}
                assets = [asset for asset in preview["manifest"].get("local_asset_snapshots", [])
                          if asset["markdown_relative_path"] == metadata["relative_path"]]
                asset_map = {asset["relative_path"]: asset for asset in assets}
                file_map.update(asset_map)
                if assets:
                    from ..obsidian import asset_member
                    from .archive import hash_file
                    frozen_assets = []
                    for asset in assets:
                        member = asset_member(asset)
                        source = inside(path.parent / member, self.settings.home)
                        actual_digest, actual_size = hash_file(source)
                        if (actual_digest, actual_size) != (asset["sha256"], asset["size_bytes"]):
                            raise BusinessError("OBSIDIAN_ASSET_BYTES_MISMATCH", "图片暂存与已确认清单不同。", 422)
                        frozen_assets.append({**asset, "archive_member": member, "upload_path": str(source)})
                    payload["local_asset_uploads"] = frozen_assets
                    received_size += sum(asset["size_bytes"] for asset in assets)
                    if row["received_bytes"] + received_size > row["expected_bytes"]:
                        raise BusinessError("BATCH_BYTES_EXCEEDED", "主件与图片超出批次确认预算。", 413)
                payload["directory_context"] = {
                    "source_root_id": root_id, "relative_path": metadata["relative_path"],
                    "image_relations": [{**item, "image_sha256": file_map[item["resolved_relative_path"]]["sha256"]
                                        if item["status"] == "RESOLVED" else None} for item in relations],
                }
                payload["acquisition_config"]["directory_context_sha256"] = json_hash(payload["directory_context"])
            if input_identity is not None:
                payload["file_input_identity"]=input_identity
            accepted = self.queue_input(instance, item_id, payload)
            if kind=="FILE" and metadata.get("document_id"):
                self.db.execute("DELETE FROM file_input_claims WHERE document_id=?",(doc_id,))
            if input_identity is not None:
                self.db.execute("INSERT INTO file_input_claims(data_instance_id,kb_id,fingerprint,input_json,document_id,source_id,task_id,created_at) "
                                "VALUES (?,?,?,?,?,?,?,?)",(instance,row["kb_id"],json_hash(input_identity),
                                json.dumps(input_identity,sort_keys=True),doc_id,source_id,accepted["task_id"],now()))
            self.db.execute(
                "UPDATE import_items SET state='ACCEPTED',task_id=?,size_bytes=?,content_sha256=? WHERE item_id=?",
                (accepted["task_id"], size, digest, item_id),
            )
            self.db.execute("UPDATE import_batches SET received_bytes=received_bytes+? WHERE batch_id=?",
                            (received_size, row["batch_id"]))
            return {**accepted, "item_id": item_id, "archive_status": "QUEUED"}

    def submit_url(self, instance: str, request_key: str, request: URLRequest, config: dict) -> dict:
        uuid_text(request_key)
        body = request.model_dump(mode="json")
        request_digest = json_hash({"request": body, "config": config})
        with transaction(self.db):
            require_instance(self.db, instance)
            kb_id = str(request.kb_id)
            require_kb(self.db, kb_id)
            old = self.db.execute("SELECT * FROM tasks WHERE data_instance_id=? AND request_key=?",
                                  (instance, request_key)).fetchone()
            if old:
                recorded = json.loads(self.db.execute("SELECT payload_json FROM task_inputs WHERE task_id=?",
                                                     (old["task_id"],)).fetchone()[0])
                if recorded.get("url_request_sha256") != request_digest:
                    raise BusinessError("IDEMPOTENCY_CONFLICT", "同一 URL 提交键关联了不同内容。")
                return self.task_status(old["task_id"])
            metadata = {"document_id": str(request.document_id) if request.document_id else None,
                        "provenance": body["provenance"]}
            doc_id, source_id = self.source_for(kb_id, "URL", metadata, source_key=url_source_key(request.url))
            payload = {
                "schema_version": 1, "kind": "URL", "kb_id": kb_id, "document_id": doc_id,
                "processing_version_id": new_id(), "source_id": source_id, "metadata": metadata,
                "url": request.url, "profile": request.profile,
                "url_request_sha256": request_digest, "acquisition_config": config,
            }
            return self.queue_input(instance, request_key, payload)

    def input_for(self, task_id: str) -> dict:
        row = self.db.execute("SELECT payload_json FROM task_inputs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            row = self.db.execute("SELECT payload_json FROM generation_inputs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise BusinessError("TASK_INPUT_NOT_FOUND", "任务输入不存在。", 404)
        payload = json.loads(row[0])
        if payload.get("schema_version") != 1:
            raise RuntimeError("unsupported input schema")
        return payload

    def current_owner(self, instance: str, task_id: str, attempt_id: str, epoch: int, worker_id: str):
        state = system_state(self.db)
        row = self.db.execute(
            "SELECT t.*,a.state AS attempt_state,a.owner_epoch,a.worker_id,a.recovery_quarantined AS aq "
            "FROM tasks t JOIN attempts a ON a.task_id=t.task_id WHERE t.task_id=? AND a.attempt_id=?",
            (task_id, attempt_id),
        ).fetchone()
        if (row is None or state["data_instance_id"] != instance or row["data_instance_id"] != instance
                or state["verification_status"] != "VERIFIED" or not state["dispatch_open"]
                or state["mode"] not in {"IMPORT", "DRAINING"} or row["recovery_quarantined"] or row["aq"]
                or row["current_attempt_id"] != attempt_id or row["status"] != "RUNNING"
                or row["attempt_state"] != "RUNNING" or row["owner_epoch"] != epoch
                or row["worker_id"] != worker_id or row["cancel_requested"]):
            raise BusinessError("EXECUTION_FENCE_REJECTED", "执行身份、模式或取消状态改变，结果不能采用。")
        return row

    def progress(self, identity: tuple, stage: str, details: dict) -> None:
        with transaction(self.db):
            self.current_owner(*identity)
            self.db.execute("UPDATE tasks SET stage=?,updated_at=? WHERE task_id=?", (stage, now(), identity[1]))
            self.db.execute("UPDATE attempts SET last_progress_at=? WHERE attempt_id=?", (now(), identity[2]))
            event(self.db, identity[1], identity[2], "PROGRESS", {"stage": stage, **details})

    def use_stage_budget(self, identity: tuple, stage: str, digest: str) -> int:
        with transaction(self.db):
            task = self.current_owner(*identity)
            scope = task["stage_budget_scope_id"]
            self.db.execute(
                "INSERT OR IGNORE INTO stage_budgets(budget_scope_id,stage,stage_input_sha256,execution_count) VALUES (?,?,?,0)",
                (scope, stage, digest),
            )
            count = self.db.execute(
                "SELECT execution_count FROM stage_budgets WHERE budget_scope_id=? AND stage=? AND stage_input_sha256=?",
                (scope, stage, digest),
            ).fetchone()[0]
            if count >= 3:
                raise BusinessError("STAGE_BUDGET_EXHAUSTED", "本阶段同输入的三次执行预算已耗尽；新 attempt 不清零。")
            self.db.execute(
                "UPDATE stage_budgets SET execution_count=execution_count+1 WHERE budget_scope_id=? AND stage=? AND stage_input_sha256=?",
                (scope, stage, digest),
            )
            event(self.db, identity[1], identity[2], "STAGE_EXECUTION_RESERVED", {"stage": stage, "count": count + 1})
            return count + 1

    def adopt_archive(self, identity: tuple, archive: dict, details: dict, stage_input_sha256: str, reuse_of=None) -> dict:
        verified = verify_archive(Path(archive["manifest_path"]))
        if verified != archive:
            raise RuntimeError("archive changed before adoption")
        payload = self.input_for(identity[1])
        with transaction(self.db):
            self.current_owner(*identity)
            doc = require_document(self.db, payload["kb_id"], payload["document_id"])
            if doc["active_prepare_task_id"] != identity[1]:
                raise BusinessError("DOCUMENT_PREPARATION_CHANGED", "文档当前准备任务已改变。")
            old_raw = self.db.execute("SELECT * FROM raw_versions WHERE raw_version_id=?",
                                      (doc["current_raw_version_id"],)).fetchone()
            retained = verify_archive(Path(old_raw["archive_manifest_path"])) if old_raw else None
            if retained and retained["content_sha256"] != old_raw["sha256"]:
                raise RuntimeError("retained raw version failed revalidation")
            # Legacy multi-member archives used only their first member hash.
            # Compare the verified complete closure so changed notes/images
            # get a new version and unchanged legacy bytes can still be reused.
            same_raw = bool(retained and retained["files"] == archive["files"])
            raw_id = doc["current_raw_version_id"] if same_raw else Path(archive["manifest_path"]).parent.name
            if not same_raw:
                self.db.execute(
                    "INSERT INTO raw_versions(raw_version_id,document_id,sha256,archive_manifest_path,created_at) VALUES (?,?,?,?,?)",
                    (raw_id, payload["document_id"], archive["content_sha256"], archive["manifest_path"], now()),
                )
            config_sha256 = json_hash(payload["acquisition_config"])
            prior_processing = self.db.execute("SELECT * FROM processing_versions WHERE processing_version_id=?",
                                                (doc["current_processing_version_id"],)).fetchone()
            unchanged = bool(same_raw and prior_processing
                             and prior_processing["raw_version_id"] == raw_id
                             and prior_processing["config_sha256"] == config_sha256)
            if same_raw and prior_processing and prior_processing["raw_version_id"] == raw_id:
                # A parsed version stores the effective parsing config, not the acquisition digest.
                prior_config = self.db.execute("SELECT config_json FROM processing_configs WHERE processing_version_id=?",
                                               (prior_processing["processing_version_id"],)).fetchone()
                if prior_config:
                    observed = self.db.execute(
                        "SELECT i.payload_json FROM source_observations o JOIN task_inputs i USING(task_id) "
                        "WHERE o.source_id=? AND o.raw_version_id=? ORDER BY o.created_at DESC,o.observation_id DESC LIMIT 1",
                        (payload["source_id"], raw_id)).fetchone()
                    previous_input = json.loads(observed["payload_json"]) if observed else None
                    config = json.loads(prior_config["config_json"])
                    if json_hash(config) != prior_processing["config_sha256"]:
                        raise BusinessError("PROCESSING_CONFIG_CHANGED", "已有解析配置无法校验，停止复用并保留原版本。")
                    from ..parsing_assets import parser_identity, parsing_models
                    policy = parser_identity(self.settings, parsing_models(self.settings))
                    unchanged = bool(previous_input
                        and previous_input["document_id"] == payload["document_id"]
                        and previous_input["source_id"] == payload["source_id"]
                        and previous_input["acquisition_config"] == payload["acquisition_config"]
                        and all(config.get(key) == value for key, value in policy.items()))
                    if unchanged and self.db.execute("SELECT 1 FROM processing_results WHERE processing_version_id=?",
                                                    (prior_processing["processing_version_id"],)).fetchone():
                        from ..parsing.store import ParsingStore
                        ParsingStore(self.db, self.settings).result(prior_processing["processing_version_id"])
            processing_id = doc["current_processing_version_id"] if unchanged else payload["processing_version_id"]
            if not unchanged:
                self.db.execute(
                    "INSERT INTO processing_versions(processing_version_id,raw_version_id,config_sha256,status,created_at)"
                    " VALUES (?,?,?,'NEW',?)", (processing_id, raw_id, config_sha256, now()),
                )
                self.db.execute("UPDATE documents SET current_raw_version_id=?,current_processing_version_id=? WHERE document_id=?",
                                (raw_id, processing_id, payload["document_id"]))
            artifact_id = new_id()
            self.db.execute(
                "INSERT INTO stage_artifacts(artifact_id,task_id,attempt_id,stage,relative_path,sha256,adopted,created_at)"
                " VALUES (?,?,?,\'ACQUIRE\',?,?,1,?)",
                (artifact_id, identity[1], identity[2], str(Path(archive["manifest_path"]).relative_to(self.settings.home)),
                 archive["manifest_sha256"], now()),
            )
            self.db.execute(
                "INSERT INTO stage_outputs(artifact_id,processing_version_id,stage_input_sha256,manifest_json,reuse_of_artifact_id) VALUES (?,?,?,?,?)",
                (artifact_id, processing_id, stage_input_sha256, json.dumps(archive, sort_keys=True), reuse_of),
            )
            if details.get("resource_review_required"):
                self.db.execute("UPDATE processing_versions SET status=\'REVIEW_REQUIRED\' WHERE processing_version_id=?", (processing_id,))
            details.update({
                "kind": payload["kind"], "source_root_id": payload.get("source_root_id"),
                "provenance": payload["metadata"]["provenance"], "raw_version_id": raw_id,
                "unchanged": unchanged, "acquisition_attempt_manifest": archive["manifest_path"],
                "stage_status": "ACQUISITION_ONLY", "parsing": "NOT_STARTED",
                "processing_pointer_semantics": "PREPARATION_ONLY_NOT_QUERY_PUBLICATION",
                "directory_context": payload.get("directory_context"),
            })
            self.db.execute(
                "INSERT INTO source_observations(observation_id,source_id,raw_version_id,task_id,details_json,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (new_id(), payload["source_id"], raw_id, identity[1], json.dumps(details, ensure_ascii=False, sort_keys=True), now()),
            )
            if payload["kind"]=="FILE":
                self.db.execute("UPDATE source_observations SET raw_version_id=? WHERE source_id=? AND task_id=? AND raw_version_id IS NULL",
                                (raw_id,payload["source_id"],identity[1]))
            self.db.execute("UPDATE tasks SET stage=?,updated_at=? WHERE task_id=?",
                            ("UNCHANGED" if unchanged else "ARCHIVED", now(), identity[1]))
            event(self.db, identity[1], identity[2], "ARCHIVE_ADOPTED" if not unchanged else "UNCHANGED_VERIFIED",
                  {"raw_version_id": raw_id, "processing_version_id": processing_id,
                   "manifest_sha256": archive["manifest_sha256"], "parsing": "NOT_STARTED"})
            return {"raw_version_id": raw_id, "processing_version_id": processing_id,
                    "unchanged": unchanged, "parsing": "NOT_STARTED"}

    def completion_owner(self, identity: tuple, *, required: bool = False):
        """Existing task fence for journaling/ending work, including cancelled work."""
        row=self.db.execute(
            "SELECT t.*,a.owner_epoch,a.worker_id,a.state AS attempt_state,a.recovery_quarantined AS aq FROM tasks t "
            "JOIN attempts a ON a.task_id=t.task_id WHERE t.task_id=? AND a.attempt_id=?",
            (identity[1],identity[2])).fetchone()
        state=system_state(self.db)
        if (row is None or row["data_instance_id"]!=identity[0] or state["data_instance_id"]!=identity[0]
                or row["current_attempt_id"]!=identity[2] or row["owner_epoch"]!=identity[3]
                or row["worker_id"]!=identity[4] or row["status"]!="RUNNING" or row["attempt_state"]!="RUNNING"
                or state["verification_status"]!="VERIFIED" or not state["dispatch_open"]
                or row["recovery_quarantined"] or row["aq"] or state["mode"] not in {"IMPORT","DRAINING"}):
            if required:
                raise BusinessError("EXECUTION_FENCE_REJECTED","执行身份失效，禁止写入结果或发送记录。")
            return None
        return row

    def finish(self, identity: tuple, *, error: dict | None = None, cancelled: bool = False) -> bool:
        with transaction(self.db):
            row=self.completion_owner(identity)
            if row is None:
                event(self.db,identity[1],identity[2],"LATE_RESULT_REJECTED",{"error":error})
                return False
            status="CANCELLED" if cancelled or row["cancel_requested"] else ("FAILED" if error else "SUCCEEDED")
            if cancelled:
                self.db.execute("UPDATE tasks SET cancel_requested=1 WHERE task_id=?",(identity[1],))
            self.db.execute("UPDATE tasks SET status=?,last_error_json=?,updated_at=? WHERE task_id=?",
                (status,json.dumps(error,ensure_ascii=False,sort_keys=True) if error else None,now(),identity[1]))
            self.db.execute("UPDATE attempts SET state=?,finished_at=? WHERE attempt_id=?",(status,now(),identity[2]))
            payload={} if row["kind"]=="OCR_SUGGESTION" else self.input_for(identity[1])
            scope="OCR_SUGGESTION_ONLY" if row["kind"]=="OCR_SUGGESTION" else "INDEX_GENERATION" if payload.get("operation")=="BUILD_GENERATION" else "PARSE_PREPARATION_ONLY" if payload.get("operation")=="PARSE_ARCHIVE" else "DOUYIN_NOTE_ONLY" if payload.get("operation")=="DOUYIN_NOTE" else "ACQUISITION"
            processing_written=False
            if scope=="PARSE_PREPARATION_ONLY" and status in {"FAILED","CANCELLED"}:
                p=payload["processing_version_id"]
                changed=self.db.execute("UPDATE processing_versions SET status='FAILED' WHERE processing_version_id=? AND raw_version_id=? "
                    "AND EXISTS (SELECT 1 FROM documents WHERE document_id=? AND current_processing_version_id=? AND active_prepare_task_id=?)",
                    (p,payload["raw_version_id"],payload["document_id"],p,identity[1]))
                processing_written=changed.rowcount==1
            event(self.db,identity[1],identity[2],status,{"error":error,"scope":scope,"processing_terminal_written":processing_written})
            return True

    def task_status(self, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise BusinessError("TASK_NOT_FOUND", "任务不存在。", 404)
        attempts = self.db.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        outboxes = self.db.execute("SELECT * FROM outbox WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        events = self.db.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY event_id", (task_id,)).fetchall()
        input_row=self.db.execute("SELECT payload_json FROM task_inputs WHERE task_id=?",(task_id,)).fetchone()
        if input_row is None:
            input_row=self.db.execute("SELECT payload_json FROM generation_inputs WHERE task_id=?",(task_id,)).fetchone()
        payload=json.loads(input_row["payload_json"]) if input_row else {}
        return {
            **dict(row), "last_error": json.loads(row["last_error_json"]) if row["last_error_json"] else None,
            "attempts": [dict(item) for item in attempts], "outboxes": [dict(item) for item in outboxes],
            "events": [{**dict(item), "details": json.loads(item["details_json"])} for item in events],
            "scope": ("OCR_SUGGESTION_ONLY" if row["kind"]=="OCR_SUGGESTION" else "INDEX_GENERATION" if payload.get("operation")=="BUILD_GENERATION" else "PARSE_PREPARATION_ONLY" if payload.get("operation")=="PARSE_ARCHIVE" else "ACQUISITION_ONLY" if row["stage"] in {"ARCHIVED","UNCHANGED"} else "IN_PROGRESS"),
        }

    def cancel(self, instance: str, task_id: str) -> dict:
        with send_gate(self.settings), transaction(self.db):
            require_instance(self.db, instance, importing=False)
            task = self.db.execute("SELECT * FROM tasks WHERE task_id=? AND data_instance_id=?", (task_id, instance)).fetchone()
            if task is None:
                raise BusinessError("TASK_NOT_FOUND", "任务不存在。", 404)
            if task["status"] in {"PENDING", "RUNNING"}:
                self.db.execute("UPDATE tasks SET cancel_requested=1,updated_at=? WHERE task_id=?", (now(), task_id))
                if task["status"] == "PENDING":
                    self.db.execute("UPDATE tasks SET status='CANCELLED' WHERE task_id=?", (task_id,))
                    self.db.execute("UPDATE attempts SET state='CANCELLED',finished_at=? WHERE attempt_id=?",
                                    (now(), task["current_attempt_id"]))
                    self.db.execute("UPDATE outbox SET state='CANCELLED' WHERE attempt_id=?", (task["current_attempt_id"],))
                event(self.db, task_id, task["current_attempt_id"], "CANCEL_REQUESTED", {"physical_stop": "WAIT_FOR_BOUNDARY"})
            return self.task_status(task_id)

    def _recovery_snapshot(self, instance: str, task_id: str, expected_attempt_id: str) -> dict:
        """Read all recovery fences inside either short transaction."""
        state = require_instance(self.db, instance)
        task = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None or task["current_attempt_id"] != expected_attempt_id:
            raise BusinessError("RECOVERY_ATTEMPT_CHANGED", "任务当前尝试已改变，请重新查看。")
        payload = self.input_for(task_id)
        kb = require_kb(self.db, payload["kb_id"])
        generation_task = payload.get("operation") == "BUILD_GENERATION"
        document = None if generation_task else require_document(self.db, payload["kb_id"], payload["document_id"])
        if task["kind"] == "DOCUMENT" and not generation_task and document["active_prepare_task_id"] != task_id:
            raise BusinessError("DOCUMENT_PREPARATION_REPLACED", "该任务已被新的文档准备任务取代，不能恢复旧任务。")
        if task["kind"] == "OCR_SUGGESTION" and document["current_processing_version_id"] != payload["processing_version_id"]:
            raise BusinessError("OCR_BASE_CHANGED", "建议的处理基准已改变，不能恢复旧任务。")
        attempt = self.db.execute("SELECT * FROM attempts WHERE attempt_id=?", (expected_attempt_id,)).fetchone()
        if attempt is None or attempt["task_id"] != task_id:
            raise BusinessError("RECOVERY_ATTEMPT_CHANGED", "旧尝试与任务的关联已改变。")
        if task["status"] not in {"FAILED", "INTERRUPTED", "RUNNING"} and not (
                task["recovery_quarantined"] and task["status"] == "PENDING"):
            raise BusinessError("TASK_NOT_RECOVERABLE", "该任务没有可手动恢复的失败或中断。")
        session = self.db.execute("SELECT * FROM worker_sessions WHERE worker_id=?", (attempt["worker_id"],)).fetchone()
        if attempt["owner_epoch"] is not None and session is None:
            raise BusinessError("OLD_OWNER_UNKNOWN", "旧执行者身份无法核实，不能接管。")
        if generation_task:
            from ..indexing.store import validate_input_records
            validate_input_records(self.db, payload)
        documents = ([dict(require_document(self.db, payload["kb_id"], item["document_id"]))
                      for item in payload["documents"]] if generation_task else [dict(document)])
        version_inputs = payload["documents"] if generation_task else [payload]
        versions = []
        for item in version_inputs:
            rows = {}
            for table, key in (("raw_versions", "raw_version_id"), ("processing_versions", "processing_version_id")):
                version_id = item.get(key)
                if version_id is not None:
                    row = self.db.execute(f"SELECT * FROM {table} WHERE {key}=?", (version_id,)).fetchone()
                    rows[table] = dict(row) if row else None
            processing_id = item.get("processing_version_id")
            if processing_id is not None:
                row = self.db.execute("SELECT * FROM processing_results WHERE processing_version_id=?", (processing_id,)).fetchone()
                rows["processing_results"] = dict(row) if row else None
            versions.append(rows)
        candidates = [dict(row) for row in self.db.execute(
            "SELECT * FROM generations WHERE task_id=? AND attempt_id=? ORDER BY generation_id",
            (task_id, expected_attempt_id)).fetchall()]
        model_events = [dict(row) for row in self.db.execute(
            "SELECT event_id,attempt_id,event_type,details_json FROM task_events WHERE task_id=? "
            "AND event_type IN ('MODEL_OPERATION_REGISTERED','MODEL_OPERATION_OBSERVED') ORDER BY event_id",
            (task_id,)).fetchall()]
        providers = [dict(row) for row in self.db.execute(
            "SELECT * FROM provider_attempts WHERE task_id=? ORDER BY provider_attempt_id", (task_id,)).fetchall()]
        if any(item["state"] in {"SENT", "UNKNOWN", "REGISTERED"} for item in providers):
            raise BusinessError("PROVIDER_HISTORY_UNCERTAIN", "旧云端调用有已发或未知状态，不能自动重放。")
        worker = dict(session) if session else None
        if worker is not None:
            worker.pop("heartbeat_at")
        return {
            "instance_state": dict(state), "task": dict(task), "attempt": dict(attempt), "payload": payload,
            "knowledge_base": dict(kb), "documents": documents, "versions": versions, "candidates": candidates,
            "worker": worker, "model_events": model_events, "providers": providers,
        }

    def recover(self, instance: str, task_id: str, expected_attempt_id: str) -> dict:
        with transaction(self.db):
            snapshot = self._recovery_snapshot(instance, task_id, expected_attempt_id)
        task, attempt, payload, session = (
            snapshot["task"], snapshot["attempt"], snapshot["payload"], snapshot["worker"])
        generation_task = payload.get("operation") == "BUILD_GENERATION"
        observation = {"old_worker": session, "process_state": "NOT_CLAIMED"}
        if attempt["owner_epoch"] is not None:
            try:
                process = psutil.Process(session["pid"])
                alive = (process.is_running() and process.create_time() == session["process_created_at"]
                         and process.status() != psutil.STATUS_ZOMBIE)
            except psutil.NoSuchProcess:
                alive = False
            except psutil.Error as exc:
                raise BusinessError("OLD_OWNER_UNKNOWN", "无法确认旧执行者已停止。") from exc
            observation["process_state"] = "ALIVE" if alive else "STOPPED"
            if alive and not (task["status"] == "FAILED" and attempt["finished_at"]
                              and session["active_task_id"] != task_id):
                raise BusinessError("OLD_OWNER_STILL_ACTIVE", "旧执行者尚未确认停止，不能恢复中断任务。")
        if generation_task:
            from ..indexing.store import validate_input_files
            from ..indexing.model_client import require_task_operations_stopped
            validate_input_files(self.settings, payload)
            require_task_operations_stopped(snapshot["model_events"], self.settings)
        elif payload.get("operation") == "PARSE_ARCHIVE":
            from .archive import verify_archive
            if verify_archive(Path(payload["archive"]["manifest_path"])) != payload["archive"]:
                raise BusinessError("RECOVERY_INPUT_INVALID", "归档输入已改变，不能恢复解析。")
        elif payload.get("operation") == "OCR_SUGGESTION":
            from .archive import hash_file
            if hash_file(Path(payload["input_path"]))[0] != payload["input_sha256"]:
                raise BusinessError("RECOVERY_INPUT_INVALID", "建议输入已改变，不能恢复。")
        elif payload["kind"] != "URL":
            from .archive import hash_file
            if hash_file(Path(payload["upload_path"])) != (payload["metadata"]["sha256"], payload["metadata"]["size_bytes"]):
                raise BusinessError("RECOVERY_INPUT_INVALID", "原上传或配置输入失效，不能复用。")
        with transaction(self.db):
            if self._recovery_snapshot(instance, task_id, expected_attempt_id) != snapshot:
                raise BusinessError("RECOVERY_SNAPSHOT_CHANGED", "恢复检查期间任务、所有权或输入状态已改变，请重新查看。")
            if generation_task:
                self.db.execute("UPDATE generations SET state='ABORTED' WHERE task_id=? AND attempt_id=? "
                                "AND state IN ('BUILDING','CHECKING','READY')", (task_id, expected_attempt_id))
            new_attempt, new_outbox = new_id(), new_id()
            timestamp = now()
            self.db.execute(
                "UPDATE attempts SET state=CASE WHEN state='RUNNING' THEN 'INTERRUPTED' ELSE state END,"
                "finished_at=COALESCE(finished_at,?) WHERE attempt_id=?", (timestamp, expected_attempt_id),
            )
            self.db.execute(
                "INSERT INTO attempts(attempt_id,task_id,data_instance_id,state,predecessor_attempt_id,created_at)"
                " VALUES (?,?,?,'PENDING',?,?)", (new_attempt, task_id, instance, expected_attempt_id, timestamp),
            )
            self.db.execute(
                "UPDATE tasks SET data_instance_id=?,status='PENDING',current_attempt_id=?,recovery_quarantined=0,"
                "cancel_requested=0,last_error_json=NULL,updated_at=? WHERE task_id=?",
                (instance, new_attempt, timestamp, task_id),
            )
            self.db.execute(
                "INSERT INTO outbox(outbox_id,task_id,attempt_id,data_instance_id,state,created_at)"
                " VALUES (?,?,?,?,'PENDING',?)", (new_outbox, task_id, new_attempt, instance, timestamp),
            )
            recovery_id = system_state(self.db)["recovery_id"]
            self.db.execute(
                "INSERT INTO recovery_choices(choice_id,task_id,predecessor_attempt_id,new_attempt_id,recovery_id,"
                "observations_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id(), task_id, expected_attempt_id, new_attempt, recovery_id,
                 json.dumps(observation, ensure_ascii=False, sort_keys=True), timestamp),
            )
            event(self.db, task_id, new_attempt, "MANUAL_RECOVERY", {
                "predecessor_attempt_id": expected_attempt_id, "outbox_id": new_outbox,
                "stage_budget_scope_preserved": task["stage_budget_scope_id"], "observations": observation,
            })
            return self.task_status(task_id)

    def preview_status(self, preview_id: str) -> dict:
        row = self.read_preview(preview_id)
        batches = [self.batch_status(item[0]) for item in self.db.execute(
            "SELECT batch_id FROM import_batches WHERE preview_id=? ORDER BY created_at", (preview_id,)).fetchall()]
        accepted = {item["metadata"]["relative_path"] for batch in batches for item in batch["items"] if item["state"] == "ACCEPTED"}
        complete = bool(row["manifest"]["files"]) and accepted == {item["relative_path"] for item in row["manifest"]["files"]}
        return {"preview_id": preview_id, "preview_sha256": row["manifest_sha256"],
                "source_root_id": row["source_root_id"], "manifest": row["manifest"], "batches": batches,
                "status": "CONSUMED" if complete else "CONFIRMED" if row["confirmed_at"] else "PREVIEWED"}

    def request_status(self, request_key: str) -> dict:
        uuid_text(request_key)
        batch = self.db.execute("SELECT batch_id FROM import_batches WHERE request_key=? ORDER BY created_at DESC LIMIT 1", (request_key,)).fetchone()
        if batch is None:
            raise BusinessError("REQUEST_NOT_FOUND", "未找到该请求键；先核对原提交与实例，不自动重发。", 404)
        return self.batch_status(batch[0])

    def rebind_root(self, instance, root_id, request) -> dict:
        path = canonical_root(request.absolute_root)
        with transaction(self.db):
            require_instance(self.db, instance)
            row = self.db.execute("SELECT * FROM source_roots WHERE source_root_id=?", (root_id,)).fetchone()
            if row is None or row["binding_version"] != request.binding_version:
                raise BusinessError("ROOT_BINDING_CHANGED", "来源目录绑定已改变，请重新核对。")
            self.db.execute("UPDATE source_roots SET absolute_path=?,binding_version=binding_version+1 WHERE source_root_id=?", (path, root_id))
            return {"source_root_id": root_id, "absolute_root": path, "binding_version": request.binding_version + 1, "old_previews": "INVALIDATED"}

    def associate_rename(self, instance, request) -> dict:
        with transaction(self.db):
            require_instance(self.db, instance)
            root = self.db.execute("SELECT * FROM source_roots WHERE source_root_id=?", (str(request.source_root_id),)).fetchone()
            if root is None:
                raise BusinessError("SOURCE_ROOT_NOT_FOUND", "来源目录不存在。", 404)
            doc = require_document(self.db, root["kb_id"], str(request.document_id))
            source = self.db.execute("SELECT * FROM document_sources WHERE source_root_id=? AND relative_path=? AND document_id=?",
                                     (str(request.source_root_id), request.old_relative_path, doc["document_id"])).fetchone()
            if source is None:
                raise BusinessError("RENAME_SOURCE_MISMATCH", "原路径与文档的已确认关联不一致。")
            if doc["active_prepare_task_id"]:
                active = self.db.execute("SELECT status FROM tasks WHERE task_id=?", (doc["active_prepare_task_id"],)).fetchone()
                if active and active[0] in {"PENDING", "RUNNING"}:
                    raise BusinessError("DOCUMENT_BUSY", "当前准备任务尚未收尾，不能改变路径关联。")
            candidate = inside(Path(root["absolute_path"]) / request.new_relative_path, Path(root["absolute_path"]))
            if candidate.is_symlink() or not candidate.is_file():
                raise BusinessError("RENAME_TARGET_INVALID", "新路径必须是来源根内现有普通文件。")
            if self.db.execute("SELECT 1 FROM document_sources WHERE source_root_id=? AND relative_path=?",
                               (str(request.source_root_id), request.new_relative_path)).fetchone():
                raise BusinessError("RENAME_TARGET_ASSOCIATED", "新路径已有来源关联，不能覆盖。")
            self.db.execute("UPDATE document_sources SET relative_path=? WHERE source_id=?", (request.new_relative_path, source["source_id"]))
            self.db.execute("INSERT INTO source_observations(observation_id,source_id,raw_version_id,task_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                            (new_id(), source["source_id"], doc["current_raw_version_id"], doc["active_prepare_task_id"], json.dumps({"action": "EXPLICIT_RENAME_ASSOCIATION",
                             "old_relative_path": request.old_relative_path, "new_relative_path": request.new_relative_path}), now()))
            return {"document_id": doc["document_id"], "source_id": source["source_id"], "relative_path": request.new_relative_path, "original_files_modified": False}

    def reusable_acquisition(self, task_id: str, digest: str):
        rows = self.db.execute("SELECT a.artifact_id,a.relative_path,a.sha256,o.manifest_json FROM stage_artifacts a JOIN stage_outputs o "
                               "ON o.artifact_id=a.artifact_id WHERE a.task_id=? AND a.stage=\'ACQUIRE\' AND a.adopted=1 AND o.stage_input_sha256=? ORDER BY a.created_at DESC",
                               (task_id, digest)).fetchall()
        if not rows:
            return None
        row = rows[0]
        record = json.loads(row["manifest_json"])
        path = inside(self.settings.home / row["relative_path"], self.settings.home)
        verified = verify_archive(path)
        if verified != record or verified["manifest_sha256"] != row["sha256"]:
            raise BusinessError("STAGE_OUTPUT_INVALID", "既有成功阶段产物不完整，停止恢复并保留现场。")
        observation = self.db.execute("SELECT details_json FROM source_observations WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        return verified, json.loads(observation[0]) if observation else {}, row["artifact_id"]
