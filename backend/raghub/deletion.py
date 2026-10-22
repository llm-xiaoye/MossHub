"""Delete RAG copies after publishing a replacement; original sources are never touched."""
from __future__ import annotations
import json
import hashlib
import logging
import os
import re
import shutil
import threading
from contextlib import closing
from pathlib import Path
from pymilvus import MilvusClient
from .db import new_id, now, open_db, system_state, transaction
from .ingestion.contracts import ModeRequest
from .ingestion.store import BusinessError, IngestionStore, require_instance, require_kb
from .indexing.store import IndexStore
from .mode import request_mode, maybe_finish_mode

LOG = logging.getLogger(__name__)
TERMINAL = {"PENDING", "RUNNING"}

def redact(value, document_id):
    if isinstance(value, list):
        return [redact(item, document_id) for item in value]
    if isinstance(value, dict):
        if value.get("document_id") == document_id:
            return {**{key:value[key] for key in ("document_id", "support_unit_id") if key in value},
                    "deleted": True, "label": "资料已删除"}
        return {key:redact(item, document_id) for key,item in value.items()}
    return value

def remove_internal(path, root):
    """Only explicit internal objects; never follow a symlink or remove the root."""
    path, root = Path(path), Path(root).resolve(strict=True)
    if not path.is_absolute() or path == root or root not in path.parents:
        raise ValueError("deletion path is outside its internal root")
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise ValueError("deletion path contains a symlink")
    if not path.exists():
        return
    if path.is_dir():
        for folder, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                if (Path(folder)/name).is_symlink():
                    raise ValueError("deletion tree contains a symlink")
        for folder, _dirs, _files in os.walk(path, topdown=False):
            Path(folder).chmod(0o700)
        shutil.rmtree(path)
    elif path.is_file():
        path.parent.chmod(path.parent.stat().st_mode | 0o200)
        path.unlink()
    else:
        raise ValueError("unsupported deletion object")

class DocumentDeletion:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()

    def _save(self, db, did, data, state, error=None):
        with transaction(db):
            db.execute("UPDATE document_deletions SET state=?,data_json=?,error_text=?,updated_at=? WHERE document_id=?",
                       (state,json.dumps(data,ensure_ascii=False,sort_keys=True),error,now(),did))

    def create(self, instance, kb_id, did):
        with self.lock, closing(open_db(self.settings.sqlite)) as db:
            with transaction(db):
                require_instance(db,instance,importing=False)
                require_kb(db,kb_id)
                row = db.execute("SELECT * FROM documents WHERE document_id=? AND kb_id=?",(did,kb_id)).fetchone()
                if row is None:
                    raise BusinessError("DOCUMENT_NOT_AVAILABLE","资料不存在。",404)
                prior = db.execute("SELECT state FROM document_deletions WHERE document_id=?",(did,)).fetchone()
                if prior:
                    return {"accepted":True,"state":prior["state"],"document_id":did}
                other = db.execute("SELECT 1 FROM document_deletions WHERE data_instance_id=? AND state NOT IN ('COMPLETE','FAILED')",
                                   (instance,)).fetchone()
                if other:
                    raise BusinessError("DELETION_BUSY","上一份资料还在删除，请等它完成。")
                IndexStore(db,self.settings).remove_document_locked(instance,kb_id,did)
                data = {"build_key":new_id(),"build_task_id":None,"base_generation_id":None,
                        "affected_generations":[],"inventory":None}
                db.execute("INSERT INTO document_deletions VALUES (?,?,?,?,?,?,?,?)",
                           (did,kb_id,instance,"QUEUED",json.dumps(data),None,now(),now()))
                db.execute("UPDATE generation_sessions SET read_only=1 WHERE generation_id IN "
                           "(SELECT generation_id FROM generation_documents WHERE document_id=?)",(did,))
            self._redact_answers(db,did)
            return {"accepted":True,"state":"QUEUED","document_id":did}

    def _redact_answers(self, db, did):
        """Parse outside the writer lock; compare-and-swap preserves concurrent results."""
        for table,key in (("chat_turns","turn_id"),("chat_requests","request_id")):
            answers=db.execute(f"SELECT {key},result_json FROM {table} WHERE result_json IS NOT NULL").fetchall()
            for answer in answers:
                raw=answer["result_json"]
                for _ in range(3):
                    value=json.loads(raw)
                    updated=redact(value,did)
                    if updated==value:break
                    replacement=json.dumps(updated,ensure_ascii=False)
                    changed=db.execute(f"UPDATE {table} SET result_json=? WHERE {key}=? AND result_json=?",
                                       (replacement,answer[key],raw))
                    if changed.rowcount==1:break
                    latest=db.execute(f"SELECT result_json FROM {table} WHERE {key}=?",(answer[key],)).fetchone()
                    if latest is None or latest[0] is None:break
                    raw=latest[0]
                else:
                    raise BusinessError("DELETE_ANSWER_CHANGED","资料已停用；历史回答正在变化，请先核对删除状态。")

    def status(self, instance, kb_id):
        with closing(open_db(self.settings.sqlite)) as db:
            require_instance(db,instance,importing=False);require_kb(db,kb_id)
            rows=db.execute("SELECT document_id,state,error_text FROM document_deletions WHERE kb_id=? AND state!='COMPLETE' "
                            "ORDER BY created_at",(kb_id,)).fetchall()
            return {"items":[dict(row) for row in rows]}

    def resume(self, instance, did):
        with self.lock, closing(open_db(self.settings.sqlite)) as db:
            require_instance(db,instance,importing=False)
            row=db.execute("SELECT * FROM document_deletions WHERE document_id=? AND data_instance_id=?",(did,instance)).fetchone()
            if row is None:raise BusinessError("DELETION_NOT_FOUND","删除记录不存在。",404)
            if row["state"]!="FAILED":return {"state":row["state"]}
            data=json.loads(row["data_json"])
            if data["build_task_id"]:
                task=db.execute("SELECT status FROM tasks WHERE task_id=?",(data["build_task_id"],)).fetchone()
                if task and task[0] not in {"PENDING","RUNNING","SUCCEEDED"}:
                    data["build_key"],data["build_task_id"]=new_id(),None
            self._save(db,did,data,"QUEUED")
            return {"state":"QUEUED"}

    def tick(self):
        if not self.lock.acquire(blocking=False):return
        try:
            with closing(open_db(self.settings.sqlite)) as db:
                row=db.execute("SELECT * FROM document_deletions WHERE state NOT IN ('COMPLETE','FAILED') ORDER BY created_at LIMIT 1").fetchone()
                if row is None:return
                data=json.loads(row["data_json"])
                try:self._step(db,row,data)
                except Exception as exc:
                    LOG.exception("document deletion stopped")
                    self._save(db,row["document_id"],data,"FAILED","删除已生效，但本机副本清理尚未完成。请核对后继续清理。")
        finally:self.lock.release()

    def _step(self, db, row, data):
        instance,kb,did=row["data_instance_id"],row["kb_id"],row["document_id"]
        require_instance(db,instance,importing=False)
        active=db.execute("SELECT 1 FROM import_workflows WHERE data_instance_id=? AND state NOT IN "
                          "('COMPLETE','PARTIAL_COMPLETE','CANCELLED','PAUSED','FAILED')",(instance,)).fetchone()
        if active:
            self._save(db,did,data,"WAITING_TASKS");return
        current=system_state(db)
        if current["mode"]!="IMPORT":
            if current["mode"]=="DRAINING":maybe_finish_mode(self.settings)
            else:
                try:request_mode(self.settings,instance,ModeRequest(mode="IMPORT"))
                except BusinessError as exc:
                    if exc.code!="QUERY_STILL_ACTIVE":raise
            self._save(db,did,data,"WAITING_TASKS");return
        # Our replacement may be queued for a worker; waiting is not failure.
        if data["build_task_id"]:
            build=db.execute("SELECT status FROM tasks WHERE task_id=?",(data["build_task_id"],)).fetchone()
            if build is not None and build[0] in {"PENDING","RUNNING"}:
                self._save(db,did,data,"REBUILDING");return
            if build is None or build[0]!="SUCCEEDED":
                raise BusinessError("DELETE_REBUILD_STOPPED","重新整理知识库未完成，请先核对原任务。")
        # A terminal DB task alone does not prove its executor has left.
        if db.execute("SELECT 1 FROM worker_sessions WHERE state='BUSY'").fetchone():
            self._save(db,did,data,"WAITING_TASKS");return
        if db.execute("SELECT 1 FROM tasks t JOIN task_inputs i USING(task_id) WHERE i.kb_id=? AND t.status IN ('PENDING','RUNNING')",(kb,)).fetchone():
            self._save(db,did,data,"WAITING_TASKS");return
        if db.execute("SELECT 1 FROM generation_readers r JOIN generations g USING(generation_id) WHERE g.kb_id=? AND r.state='OPEN'",(kb,)).fetchone():
            self._save(db,did,data,"WAITING_TASKS");return
        head=require_kb(db,kb)["current_generation_id"]
        if head and db.execute("SELECT 1 FROM generation_documents WHERE generation_id=? AND document_id=?",(head,did)).fetchone():
            remaining=db.execute("SELECT 1 FROM generation_documents g JOIN documents d USING(document_id) "
                                 "WHERE g.generation_id=? AND d.removed_at IS NULL",(head,)).fetchone()
            if remaining:
                if data["build_task_id"]:
                    task=db.execute("SELECT status FROM tasks WHERE task_id=?",(data["build_task_id"],)).fetchone()
                    if task is None or task[0]!="SUCCEEDED":raise BusinessError("DELETE_REBUILD_STOPPED","重新整理知识库尚未完成。")
                    raise BusinessError("DELETE_PUBLICATION_UNCONFIRMED","新索引尚未生效，停止清理原版本。")
                scope={"base_generation_id":head,"versions":[]}
                # Existing request slot is checked before another submission.
                prior=db.execute("SELECT task_id,status FROM tasks WHERE data_instance_id=? AND request_key=?",(instance,data["build_key"])).fetchone()
                result=prior or IndexStore(db,self.settings).queue_build(instance,data["build_key"],kb,publication_scope=scope)
                data["build_task_id"],data["base_generation_id"]=result["task_id"],head
                self._save(db,did,data,"REBUILDING");return
            with transaction(db):
                db.execute("UPDATE knowledge_bases SET current_generation_id=NULL WHERE kb_id=? AND current_generation_id=?",(kb,head))
        if data["inventory"] is None:
            if row["state"] != "CLEANING":
                self._save(db,did,data,"CLEANING");return
            data["inventory"]=self._inventory(db,kb,did)
            data["affected_generations"]=data["inventory"]["generations"]
            self._save(db,did,data,"CLEANING");return
        inventory=data["inventory"]
        with transaction(db):
            for g in inventory["generations"]:
                if require_kb(db,kb)["current_generation_id"]==g["generation_id"]:
                    raise BusinessError("DELETE_CURRENT_GENERATION","当前索引尚未替换，不能清理。")
                db.execute("UPDATE generations SET cleanup_state='RUNNING' WHERE generation_id=?",(g["generation_id"],))
                db.execute("UPDATE generation_sessions SET read_only=1 WHERE generation_id=?",(g["generation_id"],))
        self._preserve_sources(db,inventory)
        if inventory["generations"]:
            client=MilvusClient(uri=self.settings.milvus_uri,timeout=5)
            try:
                for g in inventory["generations"]:
                    if client.has_collection(g["collection_name"],timeout=5):
                        client.drop_collection(g["collection_name"],timeout=10)
                    if client.has_collection(g["collection_name"],timeout=5):
                        raise BusinessError("DELETE_COLLECTION_UNCONFIRMED","检索副本删除结果尚未确认。")
            finally:client.close()
        for path in inventory["paths"]:
            cache_key=inventory.get("cache_paths",{}).get(path)
            if cache_key and db.execute("SELECT 1 FROM chunk_vectors v JOIN chunk_sets s USING(chunk_set_id) "
                    "JOIN processing_versions p USING(processing_version_id) JOIN raw_versions r USING(raw_version_id) "
                    "WHERE v.cache_key=? AND r.document_id!=?",(cache_key,did)).fetchone():
                continue
            remove_internal(path,self.settings.home/"data")
        blobroot=self.settings.home/"data/archives/.blobs"
        for digest in inventory["blobs"]:
            path=blobroot/digest
            if path.is_file() and not path.is_symlink() and path.stat().st_nlink==1:
                remove_internal(path,self.settings.home/"data")
        self._redact_records(db,kb,did,inventory)
        current=system_state(db)
        if current["mode"]=="IMPORT" and require_kb(db,kb)["current_generation_id"]:
            request_mode(self.settings,instance,ModeRequest(mode="QUERY",kb_id=kb))
        self._save(db,did,{"document_id":did,"copies_deleted":True,"old_answers_retained":True},"COMPLETE")

    def _inventory(self, db, kb, did):
        root=self.settings.home/"data"
        paths={str(root/role/kb/did) for role in ("archives","processing","chunks")}
        gids=[]
        for r in db.execute("SELECT g.generation_id,g.collection_name,g.manifest_path,i.payload_json FROM generations g JOIN generation_inputs i USING(task_id) WHERE g.kb_id=?",(kb,)):
            payload=json.loads(r["payload_json"])
            registered=db.execute("SELECT 1 FROM generation_documents WHERE generation_id=? AND document_id=?",(r["generation_id"],did)).fetchone()
            if registered or any(d.get("document_id")==did for d in payload.get("documents",[])):
                gids.append({k:r[k] for k in ("generation_id","collection_name","manifest_path")})
        # Entire affected generations are retired; new questions use the replacement.
        for g in gids:
            paths.add(str(root/"generations"/kb/g["generation_id"]))
            if g["manifest_path"]:paths.add(g["manifest_path"])
        for r in db.execute("SELECT directory_path FROM attempts WHERE task_id IN (SELECT task_id FROM task_inputs WHERE document_id=?) AND directory_path IS NOT NULL",(did,)):
            paths.add(r[0])
        for query in ("SELECT revision_path FROM revision_previews WHERE document_id=?",
                      "SELECT input_path FROM ocr_suggestion_requests WHERE document_id=?",
                      "SELECT r.result_path FROM ocr_suggestion_results r JOIN ocr_suggestion_requests q USING(request_id) WHERE q.document_id=?"):
            paths.update(r[0] for r in db.execute(query,(did,)) if r[0])
        requests=[dict(r) for r in db.execute("SELECT r.request_id,r.trace_path FROM retrieval_requests r "
                     "JOIN generation_sessions s ON s.session_id=r.session_id WHERE s.generation_id IN "
                     "(SELECT generation_id FROM generation_documents WHERE document_id=?)",(did,))]
        paths.update(r["trace_path"] for r in requests if r["trace_path"])
        for r in requests:
            path=Path(r["trace_path"]) if r["trace_path"] else None
            if path and r["request_id"] in path.parts:
                parts=path.parts;paths.add(str(Path(*parts[:parts.index(r["request_id"])+1])))
        blobs=set()
        archive=root/"archives"/kb/did
        if archive.exists():
            for p in archive.rglob("archive-manifest.json"):
                if p.is_symlink():raise ValueError("archive manifest symlink")
                manifest=json.loads(p.read_bytes())
                blobs.update(f["sha256"] for f in manifest["files"])
        if any(not re.fullmatch("[0-9a-f]{64}",b) for b in blobs):raise ValueError("invalid blob digest")
        # This phase is read-only. Persist the complete inventory before cleanup.
        cache=[dict(r) for r in db.execute("SELECT DISTINCT c.cache_key,c.vector_path FROM vector_cache c JOIN chunk_vectors v USING(cache_key) "
                 "JOIN chunk_sets s USING(chunk_set_id) JOIN processing_versions p USING(processing_version_id) "
                 "JOIN raw_versions r USING(raw_version_id) WHERE r.document_id=?",(did,))]
        orphan=[];cache_paths={}
        for c in cache:
            shared=db.execute("SELECT 1 FROM chunk_vectors v JOIN chunk_sets s USING(chunk_set_id) JOIN processing_versions p "
                    "USING(processing_version_id) JOIN raw_versions r USING(raw_version_id) WHERE v.cache_key=? AND r.document_id!=?",
                    (c["cache_key"],did)).fetchone()
            if not shared:
                paths.add(c["vector_path"]);orphan.append(c["cache_key"]);cache_paths[c["vector_path"]]=c["cache_key"]
        return {"paths":sorted(paths,key=lambda p:(len(Path(p).parts),p),reverse=True),
                "blobs":sorted(blobs),"generations":gids,"requests":[r["request_id"] for r in requests],"cache_keys":orphan,"cache_paths":cache_paths}


    def _preserve_sources(self, db, inventory):
        """Keep only final-answer source ranges for documents that still exist."""
        from .retrieval.support_scope import support_texts
        identity=("evidence_id","support_unit_id","kb_id","generation_id","document_id",
                  "raw_version_id","processing_version_id","source_record_id")
        for rid in inventory["requests"]:
            row=db.execute("SELECT trace_path,trace_sha256,snapshot_json FROM retrieval_requests WHERE request_id=?",(rid,)).fetchone()
            if row is None or not row["trace_path"]:
                continue
            path=Path(row["trace_path"])
            root=(self.settings.home/"data/retrieval").resolve(strict=True)
            if root not in path.resolve(strict=True).parents or any(p.is_symlink() for p in (path,*path.parents) if p!=root and root in p.parents):
                raise ValueError("historical trace path is outside its archive")
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=row["trace_sha256"]:
                raise ValueError("historical trace identity changed")
            trace=json.loads(raw);snapshot=json.loads(row["snapshot_json"])
            evidence=trace.get("evidence") or {}
            whitelist=evidence.get("support_unit_whitelist") or {}
            groups=evidence.get("groups") or []
            answer=db.execute("SELECT result_json FROM chat_turns WHERE turn_id=?",(rid,)).fetchone()
            wanted={}
            if answer and answer[0]:
                for paragraph in json.loads(answer[0]).get("paragraphs",[]):
                    for ref in paragraph.get("references",[]):
                        if ref.get("deleted"):
                            continue
                        doc=db.execute("SELECT removed_at,kb_id FROM documents WHERE document_id=?",(ref.get("document_id"),)).fetchone()
                        if doc is None or doc["removed_at"] or doc["kb_id"]!=snapshot["kb_id"]:
                            continue
                        unit=whitelist.get(ref.get("support_unit_id"))
                        if unit is None or any(ref.get(k)!=unit.get(k) for k in identity):
                            raise ValueError("retained answer source differs from its sent support")
                        wanted[ref["support_unit_id"]]=unit
            kept=[]
            for group in groups:
                sent=[item for item in group["support_units"] if item["support_unit_id"] in wanted]
                if sent:
                    kept.append({**group,"support_units":sent})
            if wanted:
                support_texts(kept,wanted,kb_id=snapshot["kb_id"],generation_id=snapshot["generation_id"])
            if wanted:
                body=json.dumps({"retained_reference_only":True,"evidence":{"groups":kept,
                    "support_unit_whitelist":wanted}},ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
                digest=hashlib.sha256(body).hexdigest()
                folder=root/"retained";folder.mkdir(exist_ok=True)
                target=folder/(rid+"-"+digest+".json")
                if target.exists():
                    if target.read_bytes()!=body:raise ValueError("retained source bytes differ")
                else:
                    with target.open("xb") as out:
                        out.write(body);out.flush();os.fsync(out.fileno())
                target_path=str(target)
            else:
                target_path=digest=None
            minimal=json.dumps({key:snapshot[key] for key in ("kb_id","generation_id")},sort_keys=True)
            with transaction(db):
                db.execute("UPDATE retrieval_requests SET trace_path=?,trace_sha256=?,snapshot_json=?,progress_json=NULL WHERE request_id=?",
                           (target_path,digest,minimal,rid))

    def _redact_records(self, db, kb, did, inventory):
        marker=json.dumps({"document_id":did,"deleted":True},ensure_ascii=False)
        self._redact_answers(db,did)
        with transaction(db):
            for rid in inventory["requests"]:
                db.execute("UPDATE retrieval_requests SET progress_json=NULL WHERE request_id=?",(rid,))
            for g in inventory["generations"]:
                db.execute("UPDATE generations SET cleanup_state='SUCCEEDED',collection_removed_at=?,manifest_path=NULL,manifest_sha256=NULL WHERE generation_id=?",(now(),g["generation_id"]))
                db.execute("UPDATE generation_inputs SET payload_json=? WHERE task_id IN (SELECT task_id FROM generations WHERE generation_id=?)",(marker,g["generation_id"]))
            db.execute("DELETE FROM chunk_vectors WHERE chunk_set_id IN (SELECT s.chunk_set_id FROM chunk_sets s JOIN processing_versions p USING(processing_version_id) JOIN raw_versions r USING(raw_version_id) WHERE r.document_id=?)",(did,))
            for key in inventory["cache_keys"]:
                db.execute("DELETE FROM vector_cache WHERE cache_key=? AND NOT EXISTS (SELECT 1 FROM chunk_vectors WHERE cache_key=?)",(key,key))
            db.execute("DELETE FROM file_input_claims WHERE document_id=?",(did,))
            db.execute("UPDATE document_sources SET relative_path='deleted/'||source_id,source_key='deleted:'||source_id WHERE document_id=?",(did,))
            db.execute("UPDATE source_observations SET details_json=? WHERE source_id IN (SELECT source_id FROM document_sources WHERE document_id=?)",(marker,did))
            db.execute("UPDATE task_inputs SET payload_json=? WHERE document_id=?",(marker,did))
            db.execute("UPDATE task_events SET details_json=? WHERE task_id IN (SELECT task_id FROM task_inputs WHERE document_id=?)",(marker,did))
            db.execute("UPDATE processing_configs SET config_json='{}' WHERE processing_version_id IN (SELECT p.processing_version_id FROM processing_versions p JOIN raw_versions r USING(raw_version_id) WHERE r.document_id=?)",(did,))
            db.execute("UPDATE human_decisions SET note='资料已删除' WHERE processing_version_id IN (SELECT p.processing_version_id FROM processing_versions p JOIN raw_versions r USING(raw_version_id) WHERE r.document_id=?)",(did,))
            db.execute("UPDATE revision_previews SET difference_json='[]',review_json='[]' WHERE document_id=?",(did,))
            db.execute("UPDATE documents SET display_name='资料已删除',current_raw_version_id=NULL,current_processing_version_id=NULL WHERE document_id=?",(did,))
