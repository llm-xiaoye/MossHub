"""SQLite snapshots and conditional publication coordinate the three stores."""
from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

from ..db import new_id, now, system_state, transaction
from ..ingestion.archive import hash_file, json_hash, uuid_text, verify_archive
from ..ingestion.store import BusinessError, IngestionStore, event, require_document, require_instance, require_kb
from ..model_profiles import canonical, identity, profiles
from ..parsing.bundle import verify_bundle
from .chunks import RULES
from .storage import exact, verify_chunk_set
from .vector_cache import register_profiles

INPUT_KEYS = {"schema_version", "operation", "kb_id", "expected_previous_generation_id", "documents",
              "profile", "source_bindings", "removal_snapshot"}
DOCUMENT_KEYS = {"document_id", "raw_version_id", "processing_version_id", "bundle_path",
                 "bundle_sha256", "selection_basis"}
PROFILE_KEYS = {"embedding_space_id", "execution_profile_id", "reranker_profile_id",
                "embedding_space", "execution_profile", "reranker_profile"}
SOURCE_PATHS = ("backend/raghub/model_profiles.py", "backend/raghub/model_service.py",
                "backend/raghub/indexing/chunks.py", "backend/raghub/indexing/storage.py",
                "backend/raghub/indexing/vector_cache.py", "backend/raghub/indexing/model_client.py",
                "backend/raghub/indexing/milvus_store.py", "backend/raghub/indexing/store.py",
                "backend/raghub/indexing/worker.py", "backend/raghub/indexing/manifest.py",
                "backend/raghub/indexing/cleanup.py", "backend/raghub/indexing/router.py",
                "backend/raghub/mode.py", "backend/alembic/versions/index_004.py",
                "uv.lock", "configs/services/models.lock.json")


def source_bindings(settings):
    return {name: hash_file(settings.project / name)[0] for name in SOURCE_PATHS}


def current_chunk_rules(settings):
    return {**RULES, "source_sha256": hash_file(settings.project / "backend/raghub/indexing/chunks.py")[0],
            "docling_core_version": version("docling-core")}


def removal_snapshot(db, kb_id):
    revision = db.execute("SELECT revision FROM removal_epochs WHERE kb_id=?", (kb_id,)).fetchone()
    removed = [row[0] for row in db.execute(
        "SELECT document_id FROM documents WHERE kb_id=? AND removed_at IS NOT NULL ORDER BY document_id",
        (kb_id,)).fetchall()]
    return {"kb_id": kb_id, "revision": revision[0] if revision else 0, "removed_document_ids": removed}


def selected_documents(db, kb_id, previous):
    rows = db.execute("SELECT document_id,current_processing_version_id FROM documents "
                      "WHERE kb_id=? AND removed_at IS NULL ORDER BY document_id", (kb_id,)).fetchall()
    result = []
    for row in rows:
        processing = db.execute("SELECT p.*,r.bundle_path,r.bundle_sha256 FROM processing_versions p "
                                "JOIN processing_results r USING(processing_version_id) "
                                "WHERE p.processing_version_id=? AND p.status='READY'",
                                (row["current_processing_version_id"],)).fetchone()
        basis = "CURRENT_READY"
        if processing is None and previous:
            processing = db.execute(
                "SELECT p.*,r.bundle_path,r.bundle_sha256 FROM generation_documents g "
                "JOIN processing_versions p USING(processing_version_id) "
                "JOIN processing_results r USING(processing_version_id) "
                "WHERE g.generation_id=? AND g.document_id=? AND p.status='READY'",
                (previous, row["document_id"])).fetchone()
            basis = "RETAINED_PUBLISHED_PREDECESSOR"
        if processing is None:
            continue
        result.append({"document_id": row["document_id"], "raw_version_id": processing["raw_version_id"],
                       "processing_version_id": processing["processing_version_id"],
                       "bundle_path": processing["bundle_path"], "bundle_sha256": processing["bundle_sha256"],
                       "selection_basis": basis})
    return result


def scoped_documents(db, kb_id, previous, scope):
    """Keep the published baseline and replace only explicitly confirmed versions."""
    exact(scope, {"base_generation_id", "versions"})
    if scope["base_generation_id"] != previous or not isinstance(scope["versions"], list):
        raise BusinessError("PUBLICATION_SCOPE_INVALID", "本次发布范围与原知识库版本不一致。")
    chosen = {}
    for item in scope["versions"]:
        exact(item, {"document_id", "raw_version_id", "processing_version_id"})
        for value in item.values():
            uuid_text(value)
        if item["document_id"] in chosen:
            raise BusinessError("PUBLICATION_SCOPE_INVALID", "发布范围中有重复资料。")
        doc = require_document(db, kb_id, item["document_id"])
        if (doc["current_raw_version_id"], doc["current_processing_version_id"]) != (item["raw_version_id"], item["processing_version_id"]):
            raise BusinessError("PUBLICATION_VERSION_CHANGED", "所选资料的版本已改变，请重新核对发布范围。")
        chosen[item["document_id"]] = item
    versions = {}
    if previous:
        for row in db.execute("SELECT g.document_id,g.raw_version_id,g.processing_version_id FROM generation_documents g "
                              "JOIN documents d USING(document_id) WHERE g.generation_id=? AND d.kb_id=? AND d.removed_at IS NULL",
                              (previous, kb_id)).fetchall():
            versions[row["document_id"]] = dict(row)
    versions.update(chosen)
    result = []
    for doc_id, item in sorted(versions.items()):
        row = db.execute("SELECT p.status,p.raw_version_id,r.bundle_path,r.bundle_sha256 FROM processing_versions p "
                         "JOIN processing_results r USING(processing_version_id) WHERE p.processing_version_id=?",
                         (item["processing_version_id"],)).fetchone()
        if row is None or row["status"] != "READY" or row["raw_version_id"] != item["raw_version_id"]:
            raise BusinessError("PUBLICATION_NOT_READY", "所选资料尚未通过解析和质量检查。")
        result.append({**item, "bundle_path": row["bundle_path"], "bundle_sha256": row["bundle_sha256"],
                       "selection_basis": "CURRENT_READY" if doc_id in chosen else "RETAINED_PUBLISHED_PREDECESSOR"})
    return result


def validate_input_files(settings, payload):
    """Validate immutable input bytes before opening a SQLite transaction."""
    exact(payload, INPUT_KEYS | ({"publication_scope"} if "publication_scope" in payload else set()))
    exact(payload["profile"], PROFILE_KEYS)
    if payload["schema_version"] != 1 or payload["operation"] != "BUILD_GENERATION":
        raise BusinessError("GENERATION_INPUT_SCHEMA", "索引任务输入格式不支持。")
    uuid_text(payload["kb_id"])
    if not payload["documents"]:
        raise BusinessError("NO_READY_DOCUMENTS", "没有可建库的已复核资料。")
    for doc in payload["documents"]:
        exact(doc, DOCUMENT_KEYS)
        for key in ("document_id", "raw_version_id", "processing_version_id"):
            uuid_text(doc[key])
        verified = verify_bundle(Path(doc["bundle_path"]))
        if (verified["status"] != "READY" or verified["manifest_sha256"] != doc["bundle_sha256"]):
            raise BusinessError("GENERATION_BUNDLE_CHANGED", "建库所用解析产物已改变。")
    if payload["profile"] != profiles(settings) or payload["source_bindings"] != source_bindings(settings):
        raise BusinessError("GENERATION_CONFIGURATION_CHANGED", "模型或建库实现已改变，请重新创建任务。")
    return payload


def validate_input_records(db, payload, *, check_current=True):
    """Recheck database relationships and publication fences inside the transaction."""
    for doc in payload["documents"]:
        require_document(db, payload["kb_id"], doc["document_id"])
        row = db.execute("SELECT p.raw_version_id,p.status,r.bundle_path,r.bundle_sha256 FROM processing_versions p "
                         "JOIN processing_results r USING(processing_version_id) WHERE p.processing_version_id=?",
                         (doc["processing_version_id"],)).fetchone()
        if (row is None or row["raw_version_id"] != doc["raw_version_id"] or row["status"] != "READY"
                or row["bundle_path"] != doc["bundle_path"] or row["bundle_sha256"] != doc["bundle_sha256"]):
            raise BusinessError("GENERATION_VERSION_CHANGED", "处理版本与建库快照不一致。")
    if check_current:
        kb = require_kb(db, payload["kb_id"])
        if kb["current_generation_id"] != payload["expected_previous_generation_id"]:
            raise BusinessError("GENERATION_POINTER_CHANGED", "知识库已发布另一代次，旧候选不能覆盖。")
        selected = (scoped_documents(db, payload["kb_id"], kb["current_generation_id"], payload["publication_scope"])
                    if "publication_scope" in payload else selected_documents(db, payload["kb_id"], kb["current_generation_id"]))
        if selected != payload["documents"]:
            raise BusinessError("GENERATION_DOCUMENT_SNAPSHOT_CHANGED", "资料准备状态已改变，请重新建库。")
        if removal_snapshot(db, payload["kb_id"]) != payload["removal_snapshot"]:
            raise BusinessError("GENERATION_REMOVAL_SNAPSHOT_CHANGED", "资料移除状态已改变，请重新建库。")
    return payload


def validate_input(db, settings, payload, *, check_current=True):
    validate_input_files(settings, payload)
    return validate_input_records(db, payload, check_current=check_current)


class IndexStore(IngestionStore):
    def _build_request_slot(self, instance, request_key, kb_id, current_profile, bindings, publication_scope=None):
        """Read request and busy state; this does not reserve a slot."""
        require_instance(self.db, instance)
        kb = require_kb(self.db, kb_id)
        prior = self.db.execute("SELECT t.task_id,g.payload_json FROM tasks t JOIN generation_inputs g USING(task_id) "
                                "WHERE t.data_instance_id=? AND t.request_key=?", (instance, request_key)).fetchone()
        if prior:
            old = json.loads(prior["payload_json"])
            if (old["kb_id"] != kb_id or old["profile"] != current_profile or old["source_bindings"] != bindings
                    or old.get("publication_scope") != publication_scope):
                raise BusinessError("REQUEST_KEY_CONFLICT", "同一请求键绑定了不同的建库输入。")
            return kb, {**self.task_status(prior["task_id"]), "already_accepted": True}
        other_key = self.db.execute("SELECT task_id FROM tasks WHERE data_instance_id=? AND request_key=?",
                                    (instance, request_key)).fetchone()
        if other_key:
            raise BusinessError("REQUEST_KEY_CONFLICT", "请求键已被另一用途使用。")
        busy = self.db.execute("SELECT t.task_id FROM generation_inputs g JOIN tasks t USING(task_id) "
                               "WHERE g.kb_id=? AND t.status IN ('PENDING','RUNNING') LIMIT 1", (kb_id,)).fetchone()
        if busy:
            raise BusinessError("KNOWLEDGE_BASE_BUILD_BUSY", "该知识库已有建库任务，请查看其进度。")
        return kb, None

    def queue_build(self, instance, request_key, kb_id, *, publication_scope=None):
        uuid_text(request_key)
        uuid_text(kb_id)
        current_profile = profiles(self.settings)
        bindings = source_bindings(self.settings)
        with transaction(self.db):
            kb, accepted = self._build_request_slot(instance, request_key, kb_id, current_profile, bindings, publication_scope)
            if accepted is not None:
                return accepted
            documents = (scoped_documents(self.db, kb_id, kb["current_generation_id"], publication_scope)
                         if publication_scope is not None else selected_documents(self.db, kb_id, kb["current_generation_id"]))
            if not documents:
                raise BusinessError("NO_READY_DOCUMENTS", "请先完成至少一份资料的解析与复核。")
            payload = {"schema_version": 1, "operation": "BUILD_GENERATION", "kb_id": kb_id,
                       "expected_previous_generation_id": kb["current_generation_id"], "documents": documents,
                       "profile": current_profile, "source_bindings": bindings,
                       "removal_snapshot": removal_snapshot(self.db, kb_id)}
            if publication_scope is not None:
                payload["publication_scope"] = publication_scope
        validate_input_files(self.settings, payload)
        with transaction(self.db):
            kb, accepted = self._build_request_slot(instance, request_key, kb_id, current_profile, bindings, publication_scope)
            if accepted is not None:
                return accepted
            validate_input_records(self.db, payload)
            digest = json_hash(payload)
            task_id, attempt_id, outbox_id, budget = new_id(), new_id(), new_id(), new_id()
            timestamp = now()
            self.db.execute("INSERT INTO tasks(task_id,data_instance_id,kind,request_key,request_sha256,status,current_attempt_id,"
                            "stage_budget_scope_id,created_at,updated_at) VALUES (?,?,'DOCUMENT',?,?,'PENDING',?,?,?,?)",
                            (task_id, instance, request_key, digest, attempt_id, budget, timestamp, timestamp))
            self.db.execute("INSERT INTO attempts(attempt_id,task_id,data_instance_id,state,created_at) VALUES (?,?,?,'PENDING',?)",
                            (attempt_id, task_id, instance, timestamp))
            self.db.execute("INSERT INTO outbox(outbox_id,task_id,attempt_id,data_instance_id,state,created_at) VALUES (?,?,?,?,'PENDING',?)",
                            (outbox_id, task_id, attempt_id, instance, timestamp))
            self.db.execute("INSERT INTO generation_inputs(task_id,kb_id,payload_json,input_sha256,created_at) VALUES (?,?,?,?,?)",
                            (task_id, kb_id, canonical(payload).decode("utf-8"), digest, timestamp))
            register_profiles(self.db, current_profile)
            event(self.db, task_id, attempt_id, "GENERATION_SNAPSHOT_ACCEPTED",
                  {"document_count": len(documents), "input_sha256": digest, "expected_previous_generation_id": kb["current_generation_id"]})
            return {**self.task_status(task_id), "already_accepted": False}

    def start_candidate(self, identity_tuple, payload):
        from .milvus_store import collection_name
        validate_input_files(self.settings, payload)
        generation_id = new_id()
        name = collection_name(payload["kb_id"], generation_id)
        with transaction(self.db):
            self.current_owner(*identity_tuple)
            validate_input_records(self.db, payload)
            self.db.execute(
                "INSERT INTO generations(generation_id,kb_id,task_id,attempt_id,state,collection_name,embedding_space_id,"
                "execution_profile_id,input_sha256,expected_previous_generation_id,created_at) "
                "VALUES (?,?,?,?,'BUILDING',?,?,?,?,?,?)",
                (generation_id, payload["kb_id"], identity_tuple[1], identity_tuple[2], name,
                 payload["profile"]["embedding_space_id"], payload["profile"]["execution_profile_id"],
                 json_hash(payload), payload["expected_previous_generation_id"], now()))
            self.db.executemany(
                "INSERT INTO generation_documents(generation_id,document_id,raw_version_id,processing_version_id) VALUES (?,?,?,?)",
                [(generation_id, d["document_id"], d["raw_version_id"], d["processing_version_id"]) for d in payload["documents"]])
            event(self.db, identity_tuple[1], identity_tuple[2], "GENERATION_CANDIDATE_CREATED",
                  {"generation_id": generation_id, "collection_name": name, "reuse_candidate_collection": False})
        return generation_id, name

    def reusable_chunks(self, doc, tokenizer, profile):
        rules = current_chunk_rules(self.settings)
        chunk_set_id = identity("CHUNK_SET", {"processing_version_id": doc["processing_version_id"],
                                            "bundle_sha256": doc["bundle_sha256"], "rules": rules,
                                            "embedding_space_id": profile["embedding_space_id"]})
        row = self.db.execute("SELECT * FROM chunk_sets WHERE chunk_set_id=?", (chunk_set_id,)).fetchone()
        if row is None:
            return None
        checked = verify_chunk_set(self.settings, row["manifest_path"], row["manifest_sha256"], tokenizer, profile)
        if checked["chunk_set_id"] != chunk_set_id:
            raise BusinessError("REUSABLE_CHUNKS_CHANGED", "旧成功切块产物校验失败。")
        return checked

    def adopt_chunks(self, identity_tuple, generation_id, doc, chunks, *, reused):
        with transaction(self.db):
            self.current_owner(*identity_tuple)
            require_document(self.db, self.input_for(identity_tuple[1])["kb_id"], doc["document_id"])
            prior = self.db.execute("SELECT * FROM chunk_sets WHERE chunk_set_id=?", (chunks["chunk_set_id"],)).fetchone()
            if prior:
                if prior["manifest_path"] != chunks["manifest_path"] or prior["manifest_sha256"] != chunks["manifest_sha256"]:
                    raise BusinessError("CHUNK_IDENTITY_COLLISION", "同一切块身份对应不同产物。")
            else:
                self.db.execute(
                    "INSERT INTO chunk_sets(chunk_set_id,processing_version_id,embedding_space_id,rules_sha256,bundle_sha256,"
                    "manifest_path,manifest_sha256,parent_count,child_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (chunks["chunk_set_id"], chunks["processing_version_id"], chunks["embedding_space_id"],
                     json_hash(chunks["rules"]), chunks["bundle_sha256"], chunks["manifest_path"],
                     chunks["manifest_sha256"], len(chunks["parents"]), len(chunks["children"]), now()))
            updated = self.db.execute(
                "UPDATE generation_documents SET chunk_set_id=? WHERE generation_id=? AND document_id=? AND processing_version_id=?",
                (chunks["chunk_set_id"], generation_id, doc["document_id"], doc["processing_version_id"]))
            if updated.rowcount != 1:
                raise BusinessError("GENERATION_DOCUMENT_BINDING", "候选代次的文档绑定改变。")
            artifact_id = new_id()
            self.db.execute("INSERT INTO stage_artifacts(artifact_id,task_id,attempt_id,stage,relative_path,sha256,adopted,created_at)"
                            " VALUES (?,?,?,'CHUNK',?,?,1,?)",
                            (artifact_id, identity_tuple[1], identity_tuple[2],
                             str(Path(chunks["manifest_path"]).relative_to(self.settings.home)), chunks["manifest_sha256"], now()))
            event(self.db, identity_tuple[1], identity_tuple[2], "CHUNKS_ADOPTED",
                  {"document_id": doc["document_id"], "chunk_set_id": chunks["chunk_set_id"],
                   "artifact_id": artifact_id, "reused": reused, "parent_count": len(chunks["parents"]), "child_count": len(chunks["children"])})

    def adopt_vectors(self, identity_tuple, chunk_set, vectors, profile, stats):
        with transaction(self.db):
            self.current_owner(*identity_tuple)
            for child in chunk_set["children"]:
                record = vectors[child["chunk_id"]]
                observed = self.db.execute(
                    "SELECT cache_key,chunk_ordinal FROM chunk_vectors WHERE chunk_set_id=? AND chunk_id=? AND execution_profile_id=?",
                    (chunk_set["chunk_set_id"], child["chunk_id"], profile["execution_profile_id"])).fetchone()
                if observed:
                    if observed["cache_key"] != record["cache_key"] or observed["chunk_ordinal"] != child["chunk_ordinal"]:
                        raise BusinessError("CHUNK_VECTOR_BINDING_CHANGED", "切块向量绑定发生改变。")
                else:
                    self.db.execute(
                        "INSERT INTO chunk_vectors(chunk_set_id,chunk_id,execution_profile_id,cache_key,chunk_ordinal) VALUES (?,?,?,?,?)",
                        (chunk_set["chunk_set_id"], child["chunk_id"], profile["execution_profile_id"], record["cache_key"], child["chunk_ordinal"]))
            event(self.db, identity_tuple[1], identity_tuple[2], "DOCUMENT_VECTORS_BOUND",
                  {"chunk_set_id": chunk_set["chunk_set_id"], "execution_profile_id": profile["execution_profile_id"],
                   **stats})

    def state(self, generation_id):
        row = self.db.execute("SELECT * FROM generations WHERE generation_id=?", (generation_id,)).fetchone()
        if row is None:
            raise BusinessError("GENERATION_NOT_FOUND", "代次不存在。", 404)
        return {**dict(row), "documents": [dict(d) for d in self.db.execute(
            "SELECT * FROM generation_documents WHERE generation_id=? ORDER BY document_id", (generation_id,)).fetchall()]}

    def transition(self, identity_tuple, generation_id, old, target):
        with transaction(self.db):
            self.current_owner(*identity_tuple)
            row = self.db.execute("UPDATE generations SET state=? WHERE generation_id=? AND attempt_id=? AND state=?",
                                  (target, generation_id, identity_tuple[2], old))
            if row.rowcount != 1:
                raise BusinessError("GENERATION_STATE_CHANGED", "候选代次阶段改变。")

    def fail_candidate(self, identity_tuple, generation_id, error):
        with transaction(self.db):
            if self.completion_owner(identity_tuple) is None:
                return False
            self.db.execute("UPDATE generations SET state='ABORTED',last_error_json=? "
                            "WHERE generation_id=? AND attempt_id=? AND state IN ('BUILDING','CHECKING','READY')",
                            (canonical(error).decode("utf-8"), generation_id, identity_tuple[2]))
            row = self.db.execute("SELECT g.expected_previous_generation_id,k.current_generation_id FROM generations g "
                                  "JOIN knowledge_bases k USING(kb_id) WHERE g.generation_id=?", (generation_id,)).fetchone()
            event(self.db, identity_tuple[1], identity_tuple[2], "GENERATION_ABORTED",
                  {"generation_id": generation_id, "error": error, "collection_reused": False,
                   "current_generation_id": row["current_generation_id"] if row else None,
                   "old_query_pointer_preserved": bool(row and row["current_generation_id"] == row["expected_previous_generation_id"])})
            return True

    def publish(self, identity_tuple, generation_id, payload, manifest_record):
        from .manifest import verify_generation_manifest
        validate_input_files(self.settings, payload)
        checked = verify_generation_manifest(self.settings, manifest_record["manifest_path"], manifest_record["manifest_sha256"])
        if (checked["generation_id"] != generation_id or checked["input_sha256"] != json_hash(payload)
                or checked["task_id"] != identity_tuple[1] or checked["attempt_id"] != identity_tuple[2]):
            raise BusinessError("GENERATION_COMPLETION_BINDING", "完成清单与当前任务不一致。")
        target = Path(manifest_record["manifest_path"])
        if hash_file(target)[0] != manifest_record["manifest_sha256"]:
            raise BusinessError("GENERATION_MANIFEST_CHANGED", "完成核验后索引清单已改变。")
        with transaction(self.db):
            self.current_owner(*identity_tuple)
            validate_input_records(self.db, payload)
            generation = self.state(generation_id)
            if generation["state"] != "CHECKING" or generation["attempt_id"] != identity_tuple[2]:
                raise BusinessError("GENERATION_NOT_CHECKED", "候选代次没有完成三处存储核验。")
            expected = {doc["document_id"]: doc for doc in payload["documents"]}
            if (len(generation["documents"]) != len(expected)
                    or any(row["chunk_set_id"] is None or row["processing_version_id"] != expected[row["document_id"]]["processing_version_id"]
                           for row in generation["documents"])):
                raise BusinessError("GENERATION_LOCAL_ARTIFACTS_INCOMPLETE", "父子块或文档版本绑定不完整。")
            timestamp = now()
            self.db.execute("UPDATE generations SET state='READY',manifest_path=?,manifest_sha256=?,ready_at=? "
                            "WHERE generation_id=?", (str(target), manifest_record["manifest_sha256"], timestamp, generation_id))
            pointer = self.db.execute("UPDATE knowledge_bases SET current_generation_id=? WHERE kb_id=? "
                                      "AND current_generation_id IS ?", (generation_id, payload["kb_id"], payload["expected_previous_generation_id"]))
            if pointer.rowcount != 1:
                raise BusinessError("GENERATION_PUBLICATION_RACE", "发布指针已经改变，候选不得覆盖。")
            self.db.execute("UPDATE generations SET state='RETIRED' WHERE generation_id=? AND state='CURRENT'",
                            (payload["expected_previous_generation_id"],))
            self.db.execute("UPDATE generations SET state='CURRENT',published_at=? WHERE generation_id=?", (timestamp, generation_id))
            event(self.db, identity_tuple[1], identity_tuple[2], "GENERATION_PUBLISHED",
                  {"generation_id": generation_id, "previous_generation_id": payload["expected_previous_generation_id"],
                   "manifest_sha256": manifest_record["manifest_sha256"]})
            task = self.db.execute("UPDATE tasks SET status='SUCCEEDED',stage='READY',last_error_json=NULL,updated_at=? "
                                   "WHERE task_id=? AND current_attempt_id=? AND status='RUNNING'",
                                   (timestamp, identity_tuple[1], identity_tuple[2]))
            attempt = self.db.execute("UPDATE attempts SET state='SUCCEEDED',finished_at=? "
                                      "WHERE attempt_id=? AND state='RUNNING'", (timestamp, identity_tuple[2]))
            if task.rowcount != 1 or attempt.rowcount != 1:
                raise BusinessError("GENERATION_COMPLETION_RACE", "发布与任务完成必须在同一事务内提交。")
            event(self.db, identity_tuple[1], identity_tuple[2], "SUCCEEDED",
                  {"scope": "INDEX_GENERATION", "generation_id": generation_id})
            return self.state(generation_id)

    def remove_document(self, instance, kb_id, document_id):
        with transaction(self.db):
            return self.remove_document_locked(instance, kb_id, document_id)

    def remove_document_locked(self, instance, kb_id, document_id):
        """Caller holds the transaction; deletion receipt and fence commit together."""
        require_instance(self.db, instance, importing=False)
        require_kb(self.db, kb_id)
        row = self.db.execute("SELECT removed_at FROM documents WHERE document_id=? AND kb_id=?", (document_id, kb_id)).fetchone()
        if row is None:
            raise BusinessError("DOCUMENT_NOT_AVAILABLE", "目标文档不存在。", 404)
        if row["removed_at"]:
            return removal_snapshot(self.db, kb_id)
        timestamp = now()
        self.db.execute("INSERT INTO removal_epochs(kb_id,revision,updated_at) VALUES (?,1,?) "
                        "ON CONFLICT(kb_id) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at", (kb_id, timestamp))
        revision = self.db.execute("SELECT revision FROM removal_epochs WHERE kb_id=?", (kb_id,)).fetchone()[0]
        self.db.execute("UPDATE documents SET removed_at=? WHERE kb_id=? AND document_id=?", (timestamp, kb_id, document_id))
        self.db.execute("INSERT INTO removal_events(event_id,kb_id,document_id,revision,removed_at) VALUES (?,?,?,?,?)",
                        (new_id(), kb_id, document_id, revision, timestamp))
        return removal_snapshot(self.db, kb_id)
