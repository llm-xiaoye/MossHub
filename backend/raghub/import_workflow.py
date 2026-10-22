"""One durable workflow composes existing intake, parsing and index tasks.

No source file is changed, no task is retried here, and publication has an
explicit version scope. The API's existing process owns the single tick loop.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import closing

from .db import new_id, now, open_db, system_state, transaction
from .ingestion.archive import json_hash, uuid_text
from .ingestion.contracts import BatchRequest, BATCH_LIMIT, ModeRequest
from .ingestion.store import BusinessError, IngestionStore, require_instance, require_kb
from .indexing.store import IndexStore
from .mode import request_mode, maybe_finish_mode
from .parsing.store import ParsingStore

LOG = logging.getLogger(__name__)
TERMINAL = {"COMPLETE", "PARTIAL_COMPLETE", "CANCELLED"}
ACTIVE_TASK = {"PENDING", "RUNNING"}
LABELS = {
    "QUEUED": "准备处理", "WAITING_IMPORT": "等待当前问答结束",
    "PROCESSING": "正在处理所选笔记", "NEEDS_ATTENTION": "有笔记需要处理",
    "INDEX_QUEUED": "检查通过，等待建立索引",
    "BUILDING": "正在建立索引", "WAITING_QUERY": "等待后台工作收尾",
    "COMPLETE": "可用于问答", "PARTIAL_COMPLETE": "已通过的笔记可用于问答",
    "FAILED": "连续处理已停止", "PAUSED": "已暂停自动继续", "CANCELLED": "已结束本次处理",
}
ITEM_LABELS = {
    "PENDING": "等待读取笔记", "ARCHIVING": "正在保存导入时的原文",
    "ARCHIVE_QUEUED": "等待保存原文", "PARSE_QUEUED": "等待解析和质量检查",
    "PARSING": "正在解析和检查质量", "READY": "检查通过，等待建库",
    "REVIEW_REQUIRED": "需要处理问题", "FAILED": "处理停止", "PUBLISHED": "可用于问答",
    "HISTORICAL": "本批版本已转入历史记录",
}


def partitions(manifest):
    files, groups, current, size = manifest["files"], [], [], 0
    if not files or len(files) > 2000:
        raise BusinessError("WORKFLOW_SELECTION_LIMIT", "每次请选择1至2000份资料。", 422)
    for item in files:
        weight = item["size_bytes"] + sum(a["size_bytes"] for a in manifest.get("local_asset_snapshots", [])
                                                if a["markdown_relative_path"] == item["relative_path"])
        if weight > BATCH_LIMIT:
            raise BusinessError("BATCH_BYTES_EXCEEDED", "单份笔记及随附图片超过批次限制。", 413)
        if len(current) == 50 or size + weight > BATCH_LIMIT:
            groups.append(current)
            current, size = [], 0
        current.append(item)
        size += weight
    if current:
        groups.append(current)
    return groups


class ImportWorkflow:
    def __init__(self, settings, vault):
        self.settings, self.vault = settings, vault
        self.lock = threading.RLock()

    def _row(self, db, instance, workflow_id):
        require_instance(db, instance, importing=False)
        row = db.execute("SELECT * FROM import_workflows WHERE workflow_id=? AND data_instance_id=?",
                         (workflow_id, instance)).fetchone()
        if row is None:
            raise BusinessError("WORKFLOW_NOT_FOUND", "没有找到这次导入记录，请核对原实例。", 404)
        return row, json.loads(row["data_json"])

    def _save(self, db, row, data, state):
        with transaction(db):
            require_instance(db, row["data_instance_id"], importing=False)
            db.execute("UPDATE import_workflows SET state=?,data_json=?,updated_at=? WHERE workflow_id=?",
                       (state, json.dumps(data, ensure_ascii=False, sort_keys=True), now(), row["workflow_id"]))

    def create(self, instance, key, kb_id, preview_id, digest):
        uuid_text(key)
        with self.lock, closing(open_db(self.settings.sqlite)) as db, transaction(db):
            require_instance(db, instance, importing=False)
            kb = require_kb(db, kb_id)
            request_hash = json_hash({"kb_id": kb_id, "preview_id": preview_id, "preview_sha256": digest})
            prior = db.execute("SELECT * FROM import_workflows WHERE data_instance_id=? AND request_key=?", (instance, key)).fetchone()
            if prior:
                if prior["request_sha256"] != request_hash:
                    raise BusinessError("IDEMPOTENCY_CONFLICT", "同一提交记录的资料范围已改变。")
                return self._present(prior, json.loads(prior["data_json"]))
            if db.execute("SELECT 1 FROM document_deletions WHERE data_instance_id=? AND state NOT IN ('COMPLETE','FAILED')",(instance,)).fetchone():
                raise BusinessError("DELETION_BUSY","正在删除资料，请等本机副本清理完成后再导入。")

            active = db.execute("SELECT workflow_id FROM import_workflows WHERE data_instance_id=? "
                                "AND state NOT IN ('COMPLETE','PARTIAL_COMPLETE','CANCELLED')", (instance,)).fetchone()
            if active:
                raise BusinessError("IMPORT_WORKFLOW_BUSY", "已有一批资料正在处理或等待处理问题，请先查看原进度。")
            store = IngestionStore(db, self.settings)
            preview = store.read_preview(preview_id)
            if (preview["data_instance_id"] != instance or preview["kb_id"] != kb_id
                    or preview["manifest_sha256"] != digest or preview["absolute_path"] != str(self.vault.root)
                    or preview["manifest"].get("scope") != "SELECTION"
                    or preview["root_binding_version"] != preview["binding_version"]):
                raise BusinessError("PREVIEW_MISMATCH", "所选笔记、图片或目标知识库与确认清单不一致。")
            if db.execute("SELECT 1 FROM import_batches WHERE preview_id=?", (preview_id,)).fetchone():
                raise BusinessError("PREVIEW_ALREADY_SUBMITTED", "这份清单已有旧批次，请读取旧记录，不重复导入。")
            workflow_id = new_id()
            groups = partitions(preview["manifest"])
            data = {"preview_sha256": digest, "base_generation_id": kb["current_generation_id"],
                    "groups": groups, "batch_keys": [new_id() for _ in groups],
                    "build_request_key": new_id(), "batch_ids": [], "items": [], "error": None,
                    "build_task_id": None, "publication_scope": None, "partial": False}
            for ordinal, file in enumerate(preview["manifest"]["files"]):
                data["items"].append({"ordinal": ordinal, "path": file["relative_path"], "name": file["filename"],
                                      "state": "PENDING", "document_id": None, "raw_version_id": None,
                                      "processing_version_id": None, "archive_task_id": None, "parse_task_id": None,
                                      "issue": None, "reused": False, "parse_request_key": new_id()})
            stamp = now()
            db.execute("INSERT INTO import_workflows VALUES (?,?,?,?,?,?,'QUEUED',?,?,?)",
                       (workflow_id, instance, kb_id, key, request_hash, preview_id,
                        json.dumps(data, ensure_ascii=False, sort_keys=True), stamp, stamp))
            row = db.execute("SELECT * FROM import_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            return self._present(row, data)

    def continue_tasks(self, instance, key, kb_id, task_ids):
        """Continue already accepted intake; never submit or resend its source."""
        uuid_text(key)
        ids=list(dict.fromkeys(task_ids))
        if not 1 <= len(ids) <= 50:
            raise BusinessError("WORKFLOW_SELECTION_LIMIT","每批请选择1至50份资料。",422)
        for task_id in ids:uuid_text(task_id)
        with self.lock, closing(open_db(self.settings.sqlite)) as db, transaction(db):
            require_instance(db,instance,importing=False)
            kb=require_kb(db,kb_id)
            digest=json_hash({"kb_id":kb_id,"task_ids":ids})
            prior=db.execute("SELECT * FROM import_workflows WHERE data_instance_id=? AND request_key=?",(instance,key)).fetchone()
            if prior:
                if prior["request_sha256"]!=digest:raise BusinessError("IDEMPOTENCY_CONFLICT","原提交的处理范围已改变。")
                return self._present(prior,json.loads(prior["data_json"]),db=db)
            if db.execute("SELECT 1 FROM document_deletions WHERE data_instance_id=? AND state NOT IN ('COMPLETE','FAILED')",(instance,)).fetchone():
                raise BusinessError("DELETION_BUSY","正在删除资料，请等本机副本清理完成后再导入。")

            if db.execute("SELECT 1 FROM import_workflows WHERE data_instance_id=? AND state NOT IN ('COMPLETE','PARTIAL_COMPLETE','CANCELLED')",(instance,)).fetchone():
                raise BusinessError("IMPORT_WORKFLOW_BUSY","已有一批资料正在处理，请先查看原进度。")
            items=[]
            for task_id in ids:
                bound=db.execute("SELECT i.document_id,i.payload_json,d.display_name,d.removed_at,t.data_instance_id,i.kb_id "
                    "FROM task_inputs i JOIN tasks t USING(task_id) JOIN documents d USING(document_id) WHERE i.task_id=?",(task_id,)).fetchone()
                if bound is None or bound["data_instance_id"]!=instance or bound["kb_id"]!=kb_id or bound["removed_at"]:
                    raise BusinessError("WORKFLOW_TASK_BINDING","原任务与当前知识库或资料不一致。")
                payload=json.loads(bound["payload_json"])
                archive_id=task_id
                if payload.get("operation")=="PARSE_ARCHIVE":
                    original=db.execute("SELECT o.task_id FROM source_observations o JOIN document_sources s USING(source_id) "
                        "JOIN documents d USING(document_id) WHERE s.document_id=? AND o.raw_version_id=d.current_raw_version_id "
                        "ORDER BY o.created_at DESC LIMIT 1",(bound["document_id"],)).fetchone()
                    if original is None:raise BusinessError("WORKFLOW_ARCHIVE_BINDING","没有找到对应的原归档任务。")
                    archive_id=original["task_id"]
                elif payload.get("kind") not in {"FILE","DIRECTORY","URL"}:
                    raise BusinessError("WORKFLOW_TASK_BINDING","请选择已接收资料的原任务。")
                if any(i["document_id"]==bound["document_id"] for i in items):continue
                items.append({"ordinal":len(items),"path":bound["document_id"],"name":bound["display_name"],
                    "state":"PENDING","document_id":bound["document_id"],"raw_version_id":None,
                    "processing_version_id":None,"archive_task_id":archive_id,"parse_task_id":None,
                    "issue":None,"reused":False,"parse_request_key":new_id()})
            data={"external_tasks":True,"preview_sha256":None,"base_generation_id":kb["current_generation_id"],
                "groups":[],"batch_keys":[],"build_request_key":new_id(),"batch_ids":[],"items":items,"error":None,
                "build_task_id":None,"publication_scope":None,"partial":False}
            workflow_id,stamp=new_id(),now()
            db.execute("INSERT INTO import_workflows VALUES (?,?,?,?,?,NULL,'QUEUED',?,?,?)",
                (workflow_id,instance,kb_id,key,digest,json.dumps(data,ensure_ascii=False,sort_keys=True),stamp,stamp))
            row=db.execute("SELECT * FROM import_workflows WHERE workflow_id=?",(workflow_id,)).fetchone()
            return self._present(row,data,db=db)

    def _versions(self, data):
        return [{"document_id": i["document_id"], "raw_version_id": i["raw_version_id"],
                 "processing_version_id": i["processing_version_id"]}
                for i in data["items"] if i["state"] == "READY"]

    def _present(self, row, data, offset=0, limit=50, db=None):
        items = [dict(i) for i in data["items"]]
        if db is not None:
            heads = {d["document_id"]: d for d in db.execute(
                "SELECT document_id,removed_at,current_raw_version_id,current_processing_version_id FROM documents WHERE kb_id=?",
                (row["kb_id"],)).fetchall()}
            published = {(d["document_id"], d["processing_version_id"]) for d in db.execute(
                "SELECT g.document_id,g.processing_version_id FROM generation_documents g JOIN knowledge_bases k "
                "ON k.current_generation_id=g.generation_id WHERE k.kb_id=?", (row["kb_id"],)).fetchall()}
            for item in items:
                head = heads.get(item["document_id"])
                item["source_available"] = bool(head and not head["removed_at"])
                if item["document_id"] and (head is None or head["removed_at"]):
                    self._problem(item, "DOCUMENT_REMOVED", "该资料已移除，不再用于检索，也不开放原文。")
                elif item["state"] == "PUBLISHED" and (item["document_id"], item["processing_version_id"]) not in published:
                    item["state"] = "HISTORICAL"
                elif item["state"] == "READY" and head and (
                    head["current_raw_version_id"], head["current_processing_version_id"]) != (
                        item["raw_version_id"], item["processing_version_id"]):
                    self._problem(item, "WORKFLOW_VERSION_CHANGED", "资料版本已改变，请重新核对；不沿用旧的发布确认。")
        # Pagination and counts describe the same visible documents after deletion.
        items = [i for i in items if not i["document_id"] or i.get("source_available") is not False]
        versions = self._versions({**data, "items": items})
        published_count = sum(i["state"] == "PUBLISHED" for i in items)
        label = LABELS[row["state"]]
        if row["state"] in {"COMPLETE", "PARTIAL_COMPLETE"} and not published_count:
            label = "本批处理已完成；这些版本已不在当前问答范围"
        return {"workflow_id": row["workflow_id"], "preview_id": row["preview_id"], "data_instance_id": row["data_instance_id"],
                "kb_id": row["kb_id"], "state": row["state"], "label": label,
                "updated_at": row["updated_at"], "total": len(items),
                "ready_count": len(versions), "published_count": published_count,
                "problem_count": sum(i["state"] in {"FAILED", "REVIEW_REQUIRED"} for i in items),
                "items": [{**i, "label": ITEM_LABELS[i["state"]]} for i in items[offset:offset+limit]],
                "offset": offset, "error": data["error"], "build_task_id": data["build_task_id"],
                "generation_id": data.get("generation_id"),
                "ready_sha256": json_hash(versions), "partial": data["partial"],
                "can_use_ready": row["state"] == "NEEDS_ATTENTION" and bool(versions) and versions == self._versions(data),
                "has_previous_publication": data["base_generation_id"] is not None,
                "archive_retained": True}

    def _owned_tasks(self, db, row, data):
        ids = {i[k] for i in data["items"] for k in ("archive_task_id", "parse_task_id") if i[k]}
        ids.update(i["current_processing_task_id"] for i in data["items"] if i.get("current_processing_task_id"))
        keys = set(data["batch_keys"]) | {i["parse_request_key"] for i in data["items"]} | {data["build_request_key"]}
        for task in db.execute("SELECT task_id,request_key FROM tasks WHERE data_instance_id=?", (row["data_instance_id"],)).fetchall():
            if task["request_key"] in keys:
                ids.add(task["task_id"])
        for batch_id in data["batch_ids"]:
            ids.update(t["task_id"] for t in db.execute("SELECT task_id FROM import_items WHERE batch_id=? AND task_id IS NOT NULL",
                                                        (batch_id,)).fetchall())
        return ids

    def read(self, instance, workflow_id, offset=0):
        with self.lock, closing(open_db(self.settings.sqlite)) as db:
            row, data = self._row(db, instance, workflow_id)
            return self._present(row, data, offset, db=db)

    def latest(self, instance, kb_id, request_key=None):
        with self.lock, closing(open_db(self.settings.sqlite)) as db:
            require_instance(db, instance, importing=False)
            require_kb(db, kb_id)
            if request_key:
                row = db.execute("SELECT * FROM import_workflows WHERE data_instance_id=? AND request_key=? AND kb_id=?",
                                 (instance, request_key, kb_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM import_workflows WHERE data_instance_id=? AND kb_id=? "
                                 "ORDER BY created_at DESC,workflow_id DESC LIMIT 1", (instance, kb_id)).fetchone()
            return {"workflow": self._present(row, json.loads(row["data_json"]), db=db) if row else None}

    def action(self, instance, workflow_id, action, ready_sha256=None):
        with self.lock, closing(open_db(self.settings.sqlite)) as db:
            row, data = self._row(db, instance, workflow_id)
            if row["state"] in TERMINAL:
                return self._present(row, data, db=db)
            if action == "pause":
                self._save(db, row, data, "PAUSED")
            elif action == "finish":
                ids = self._owned_tasks(db, row, data)
                if any(self._task(db, t)["status"] in ACTIVE_TASK for t in ids):
                    raise BusinessError("WORKFLOW_TASKS_ACTIVE", "已接收的后台任务还未结束，请等待收尾后再结束本批。")
                published = db.execute("SELECT 1 FROM generations g JOIN knowledge_bases k ON k.current_generation_id=g.generation_id "
                                       "WHERE k.kb_id=? AND g.task_id IN (SELECT task_id FROM tasks WHERE request_key=?)",
                                       (row["kb_id"], data["build_request_key"])).fetchone()
                if published or data.get("generation_id"):
                    raise BusinessError("WORKFLOW_PUBLICATION_PENDING", "请先核对原建库任务，不能在发布结果未知时结束。")
                self._save(db, row, data, "CANCELLED")
            elif action == "resume":
                if row["state"] == "FAILED" and data.get("build_task_id"):
                    failed_id = data["build_task_id"]
                    task = self._task(db, failed_id)
                    if task["status"] not in ACTIVE_TASK and task["status"] != "SUCCEEDED":
                        candidates = db.execute(
                            "SELECT generation_id,state,expected_previous_generation_id FROM generations "
                            "WHERE task_id=? AND kb_id=?", (failed_id, row["kb_id"])).fetchall()
                        current = require_kb(db, row["kb_id"])["current_generation_id"]
                        if (task["status"] not in {"FAILED", "CANCELLED", "INTERRUPTED"}
                                or len(candidates) != 1 or candidates[0]["state"] != "ABORTED"
                                or candidates[0]["generation_id"] == current
                                or candidates[0]["expected_previous_generation_id"] != data["base_generation_id"]
                                or current != data["base_generation_id"] or data.get("generation_id")):
                            raise BusinessError("WORKFLOW_BUILD_RECOVERY_UNCONFIRMED",
                                "原建库任务的停止状态或已发布版本尚未核对，不能重新建库。请先读取原任务。")
                        # A failed task may still have local CPU work in flight.
                        # Reuse the existing recovery fence before allocating a new key.
                        from .indexing.model_client import require_task_operations_stopped
                        model_events = db.execute(
                            "SELECT event_type,details_json FROM task_events WHERE task_id=? "
                            "AND event_type IN ('MODEL_OPERATION_REGISTERED','MODEL_OPERATION_OBSERVED') ORDER BY rowid",
                            (failed_id,)).fetchall()
                        require_task_operations_stopped(model_events, self.settings)
                        # The old, definitely aborted attempt remains intact. A user's
                        # explicit continue action gets a fresh scope and request key;
                        # unknown or published attempts never enter this branch.
                        data.setdefault("prior_builds", []).append({
                            "task_id": failed_id, "generation_id": candidates[0]["generation_id"],
                            "state": "ABORTED", "request_key": data["build_request_key"]})
                        data["build_task_id"] = None
                        data["build_request_key"] = new_id()
                        data["publication_scope"] = None
                        data["partial"] = False
                data["error"] = None
                self._save(db, row, data, "PROCESSING")
            elif action == "use-ready":
                if row["state"] != "NEEDS_ATTENTION" or not self._versions(data) or ready_sha256 != json_hash(self._versions(data)):
                    raise BusinessError("READY_SELECTION_CHANGED", "已通过资料的范围改变，请重新查看并确认。")
                # Recheck all heads before freezing a user's partial-publication decision.
                versions = self._versions(data)
                from .indexing.store import scoped_documents
                scope = {"base_generation_id": data["base_generation_id"], "versions": versions}
                scoped_documents(db, row["kb_id"], require_kb(db, row["kb_id"])["current_generation_id"], scope)
                data["partial"], data["publication_scope"] = True, scope
                self._save(db, row, data, "PROCESSING")
            else:
                raise BusinessError("WORKFLOW_ACTION_INVALID", "不支持的处理操作。", 422)
            return self.read(instance, workflow_id)

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            with closing(open_db(self.settings.sqlite)) as db:
                state = system_state(db)
                if state["verification_status"] != "VERIFIED" or not state["dispatch_open"]:
                    return
                row = db.execute("SELECT * FROM import_workflows WHERE data_instance_id=? AND state NOT IN "
                                 "('COMPLETE','PARTIAL_COMPLETE','CANCELLED','PAUSED','FAILED') LIMIT 1",
                                 (state["data_instance_id"],)).fetchone()
                if row is None:
                    return
                data = json.loads(row["data_json"])
                try:
                    self._step(db, row, data)
                except Exception as exc:
                    LOG.exception("import workflow stopped: %s", row["workflow_id"])
                    data["error"] = {"code": getattr(exc, "code", "WORKFLOW_STOPPED"), "message": str(exc)[:1500],
                                     "stage": row["state"]}
                    self._save(db, row, data, "FAILED")
        finally:
            self.lock.release()

    def _task(self, db, task_id):
        row = db.execute("SELECT status,stage,last_error_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise BusinessError("WORKFLOW_TASK_MISSING", "原任务记录无法读取，已停止继续提交。")
        return row

    def _problem(self, item, code, message, review=False):
        item["state"] = "REVIEW_REQUIRED" if review else "FAILED"
        item["issue"] = {"code": code, "message": message}

    def _refresh_item(self, db, row, data, item):
        if not item["archive_task_id"]:
            return
        task = self._task(db, item["archive_task_id"])
        if task["status"] in ACTIVE_TASK:
            item["state"] = "ARCHIVE_QUEUED" if task["status"] == "PENDING" else "ARCHIVING"
            item["issue"] = None
            return
        if task["status"] != "SUCCEEDED":
            self._problem(item, "ARCHIVE_STOPPED", "保存原文时停止；请查看原任务和已保留的文件。")
            return
        store = IngestionStore(db, self.settings)
        payload = store.input_for(item["archive_task_id"])
        item["document_id"] = payload["document_id"]
        observation = db.execute("SELECT raw_version_id,details_json FROM source_observations WHERE task_id=? "
                                 "AND raw_version_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                                 (item["archive_task_id"],)).fetchone()
        if observation is None:
            raise BusinessError("WORKFLOW_ARCHIVE_BINDING", "归档成功记录缺少对应原件，不能继续解析。")
        bound_raw = observation["raw_version_id"]
        doc = db.execute("SELECT * FROM documents WHERE document_id=? AND kb_id=?", (item["document_id"], row["kb_id"])).fetchone()
        item["raw_version_id"] = bound_raw
        if doc is None or doc["removed_at"]:
            self._problem(item, "DOCUMENT_REMOVED", "这份资料已移除，不会用于本次发布。")
            return
        if doc["current_raw_version_id"] != bound_raw:
            self._problem(item, "WORKFLOW_VERSION_CHANGED", "这份笔记已有其他更新，本次停止继续，请核对版本。")
            return
        processing = db.execute("SELECT p.*,c.parent_processing_version_id,c.origin_kind,c.prepare_task_id FROM processing_versions p "
                                "LEFT JOIN processing_configs c USING(processing_version_id) WHERE p.processing_version_id=?",
                                (doc["current_processing_version_id"],)).fetchone()
        if processing is None:
            raise BusinessError("WORKFLOW_PROCESSING_MISSING", "没有找到归档对应的处理版本。")
        if processing["raw_version_id"] != bound_raw:
            raise BusinessError("WORKFLOW_PROCESSING_RAW_MISMATCH", "处理结果不属于本次归档原件，停止发布。")
        item["processing_version_id"] = processing["processing_version_id"]
        if not item.get("processing_seen"):
            item["reused"] = processing["status"] == "READY"
            item["processing_seen"] = True
        if processing["prepare_task_id"]:
            item["current_processing_task_id"] = processing["prepare_task_id"]
            if item["parse_task_id"] is None:
                item["parse_task_id"] = processing["prepare_task_id"]
            current_task = self._task(db, processing["prepare_task_id"])
            if processing["status"] != "READY" and current_task["status"] in ACTIVE_TASK:
                item["state"] = "PARSE_QUEUED" if current_task["status"] == "PENDING" else "PARSING"
                item["issue"] = None
                return
        # Acquisition can flag resources before a parser has ever been queued.
        # Keep those flags, but produce the parsing report before asking for review.
        acquisition_review = (processing["status"] == "REVIEW_REQUIRED"
            and processing["origin_kind"] is None and processing["prepare_task_id"] is None)
        if item["parse_task_id"] is None and (processing["status"] == "NEW" or acquisition_review):
            key = item["parse_request_key"]
            prior = db.execute("SELECT task_id FROM tasks WHERE data_instance_id=? AND request_key=?",
                               (row["data_instance_id"], key)).fetchone()
            if prior:
                item["parse_task_id"] = prior["task_id"]
            else:
                try:
                    result = ParsingStore(db, self.settings).queue_parse(row["data_instance_id"], key, row["kb_id"],
                                          item["document_id"], {}, expected_raw_version_id=bound_raw)
                except BusinessError as exc:
                    self._problem(item, exc.code, str(exc))
                    return
                item["parse_task_id"] = result["task_id"]
                item["processing_version_id"] = result["processing_version_id"]
            queued = self._task(db, item["parse_task_id"])
            item["state"] = "PARSE_QUEUED" if queued["status"] == "PENDING" else "PARSING"
            item["issue"] = None
            return
        if processing["status"] == "READY":
            # Human revisions still pass the original parsing adoption and quality gates.
            item["state"], item["issue"] = "READY", None
        elif processing["status"] == "REVIEW_REQUIRED":
            self._problem(item, "QUALITY_REVIEW_REQUIRED", "请打开“处理问题”，对照原文核对。系统问题不会自动放行。", True)
        elif processing["status"] in {"NEW", "PROCESSING"}:
            self._problem(item, "PARSE_STOPPED", "对应解析任务尚未成功结束，请查看原任务状态。")
        else:
            self._problem(item, "PARSE_STOPPED", "解析任务停止；原文仍保留。请查看原任务，从允许恢复的步骤继续。")

    def _step(self, db, row, data):
        instance, kb_id = row["data_instance_id"], row["kb_id"]
        if data["build_task_id"] or data.get("reused_generation_id"):
            if data["build_task_id"]:
                task = self._task(db, data["build_task_id"])
                if task["status"] in ACTIVE_TASK:
                    self._save(db, row, data, "INDEX_QUEUED" if task["status"] == "PENDING" else "BUILDING")
                    return
                if task["status"] != "SUCCEEDED":
                    data["error"] = {"code": "INDEX_STOPPED", "message": "建库任务停止，之前已发布的版本继续保留。请查看原任务。"}
                    self._save(db, row, data, "FAILED")
                    return
                generation = db.execute("SELECT generation_id,state FROM generations WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                                        (data["build_task_id"],)).fetchone()
            else:
                generation = db.execute("SELECT generation_id,state FROM generations WHERE generation_id=?",
                                        (data["reused_generation_id"],)).fetchone()
            kb = require_kb(db, kb_id)
            if not generation or kb["current_generation_id"] != generation["generation_id"]:
                raise BusinessError("WORKFLOW_PUBLICATION_UNCONFIRMED", "建库任务结束，但当前发布版本尚未核对一致。")
            data["generation_id"] = generation["generation_id"]
            selected = {v["document_id"] for v in data["publication_scope"]["versions"]}
            for item in data["items"]:
                if item["document_id"] in selected:
                    item["state"] = "PUBLISHED"
            other = db.execute("SELECT 1 FROM tasks WHERE data_instance_id=? AND status IN ('PENDING','RUNNING') "
                               "AND recovery_quarantined=0 LIMIT 1", (instance,)).fetchone()
            if other:
                self._save(db, row, data, "WAITING_QUERY")
                return
            current = system_state(db)
            if current["mode"] == "IMPORT":
                current = request_mode(self.settings, instance, ModeRequest(mode="QUERY", kb_id=kb_id))
            elif current["mode"] == "DRAINING":
                current = maybe_finish_mode(self.settings)
            self._save(db, row, data, ("PARTIAL_COMPLETE" if data["partial"] else "COMPLETE") if current["mode"] == "QUERY" else "WAITING_QUERY")
            return
        current = system_state(db)
        if current["mode"] != "IMPORT":
            if current["mode"] == "DRAINING":
                maybe_finish_mode(self.settings)
                self._save(db, row, data, "WAITING_IMPORT")
                return
            try:
                request_mode(self.settings, instance, ModeRequest(mode="IMPORT"))
            except BusinessError as exc:
                if exc.code != "QUERY_STILL_ACTIVE":
                    raise
                self._save(db, row, data, "WAITING_IMPORT")
                return
        store = IngestionStore(db, self.settings)
        if len(data["batch_ids"]) < len(data["groups"]):
            store.confirm_preview(instance, row["preview_id"], data["preview_sha256"])
            index = len(data["batch_ids"])
            body = BatchRequest.model_validate({"kb_id": kb_id, "items": data["groups"][index],
                      "preview_id": row["preview_id"], "preview_sha256": data["preview_sha256"]})
            batch = store.create_batch(instance, data["batch_keys"][index], body)
            data["batch_ids"].append(batch["batch_id"])
            self._save(db, row, data, "PROCESSING")
            return
        by_path = {i["path"]: i for i in data["items"]}
        for batch_id in data["batch_ids"]:
            batch = store.batch_status(batch_id)
            for received in batch["items"]:
                item = by_path[received["metadata"]["relative_path"]]
                if received["state"] == "AWAITING_UPLOAD":
                    # Reserve and accept are the existing intake protocol. Never retry RECEIVING/FAILED/UNKNOWN.
                    self.vault.receive(instance, batch_id, received["item_id"])
                    self._save(db, row, data, "PROCESSING")
                    return
                if received["state"] != "ACCEPTED":
                    self._problem(item, "RECEIVE_UNCONFIRMED", "读取结果尚未确认，保留原批次；不会重新提交。请查看原任务。")
                    continue
                item["archive_task_id"] = received["task_id"]
                self._refresh_item(db, row, data, item)
        if data.get("external_tasks"):
            for item in data["items"]:
                self._refresh_item(db, row, data, item)
        if data["publication_scope"] is None:
            if any(i["state"] in {"PENDING", "ARCHIVE_QUEUED", "ARCHIVING", "PARSE_QUEUED", "PARSING"} for i in data["items"]):
                self._save(db, row, data, "PROCESSING")
                return
            if any(i["state"] != "READY" for i in data["items"]):
                self._save(db, row, data, "NEEDS_ATTENTION")
                return
            data["publication_scope"] = {"base_generation_id": data["base_generation_id"], "versions": self._versions(data)}
        prior = db.execute("SELECT t.task_id,t.status,g.payload_json FROM tasks t JOIN generation_inputs g USING(task_id) "
                           "WHERE t.data_instance_id=? AND t.request_key=?", (instance, data["build_request_key"])).fetchone()
        if prior:
            payload = json.loads(prior["payload_json"])
            if payload["kb_id"] != kb_id or payload.get("publication_scope") != data["publication_scope"]:
                raise BusinessError("WORKFLOW_BUILD_BINDING_CHANGED", "原建库任务的发布范围无法核对，停止提交。")
            data["build_task_id"] = prior["task_id"]
            self._save(db, row, data, "INDEX_QUEUED" if prior["status"] == "PENDING" else "BUILDING")
            return
        from .indexing.store import scoped_documents
        previous = require_kb(db, kb_id)["current_generation_id"]
        scoped_documents(db, kb_id, previous, data["publication_scope"])
        existing = {(d["document_id"], d["raw_version_id"], d["processing_version_id"]) for d in db.execute(
            "SELECT document_id,raw_version_id,processing_version_id FROM generation_documents WHERE generation_id=?", (previous,)).fetchall()}
        if previous and all((v["document_id"],v["raw_version_id"],v["processing_version_id"]) in existing
                            for v in data["publication_scope"]["versions"]):
            data["reused_generation_id"] = previous
            self._save(db, row, data, "WAITING_QUERY")
            return
        result = IndexStore(db, self.settings).queue_build(instance, data["build_request_key"],
                                                         kb_id, publication_scope=data["publication_scope"])
        data["build_task_id"] = result["task_id"]
        self._save(db, row, data, "INDEX_QUEUED")
