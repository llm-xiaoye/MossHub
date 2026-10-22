"""Generation access and manual cleanup preserve current and previous versions."""
from __future__ import annotations

import json
import time

from ..db import new_id, now, system_state, transaction
from ..ingestion.archive import json_hash, uuid_text
from ..ingestion.store import BusinessError, require_instance, require_kb
from .milvus_store import DedicatedClient, collection_name


def retained_generations(db, kb_id):
    kb = require_kb(db, kb_id)
    published = db.execute(
        "SELECT generation_id FROM generations WHERE kb_id=? AND published_at IS NOT NULL ORDER BY published_at DESC,generation_id DESC",
        (kb_id,)).fetchall()
    current = kb["current_generation_id"]
    if current is None:
        return []
    ids = [row["generation_id"] for row in published]
    if not ids or ids[0] != current:
        raise BusinessError("GENERATION_HISTORY_INCONSISTENT", "当前代次与成功发布历史不一致。")
    return ids[:2]


def require_query_generation(db, kb_id, generation_id):
    allowed = retained_generations(db, kb_id)
    row = db.execute("SELECT * FROM generations WHERE kb_id=? AND generation_id=?", (kb_id, generation_id)).fetchone()
    if (generation_id not in allowed or row is None or row["manifest_path"] is None or row["manifest_sha256"] is None
            or row["state"] not in {"CURRENT", "RETIRED"} or row["cleanup_state"] != "NONE" or row["collection_removed_at"]):
        raise BusinessError("GENERATION_READ_ONLY", "只可向当前及上一成功代次提问；更旧会话保留为只读。")
    return dict(row)


def open_reader(db, instance, kb_id, generation_id, request_id):
    uuid_text(request_id)
    with transaction(db):
        state = require_instance(db, instance, importing=False)
        if state["mode"] != "QUERY":
            raise BusinessError("QUERY_MODE_REQUIRED", "请等待系统进入问答模式。")
        generation = require_query_generation(db, kb_id, generation_id)
        if db.execute("SELECT 1 FROM generation_readers WHERE request_id=?", (request_id,)).fetchone():
            raise BusinessError("REQUEST_KEY_CONFLICT", "阅读请求键已使用。")
        db.execute("INSERT INTO generation_readers(request_id,generation_id,data_instance_id,state,created_at) VALUES (?,?,?,'OPEN',?)",
                   (request_id, generation_id, instance, now()))
        return {"request_id": request_id, "generation": generation}


def close_reader(db, instance, request_id):
    with transaction(db):
        require_instance(db, instance, importing=False)
        owned = db.execute("SELECT state FROM retrieval_requests WHERE request_id=?", (request_id,)).fetchone()
        if owned is not None and owned["state"] in {"RUNNING", "CANCELLING"}:
            raise BusinessError("QUERY_READER_OWNED", "活动问答拥有该读取保护，请先停止问答并等待计算结束。")
        row = db.execute("UPDATE generation_readers SET state='CLOSED',closed_at=? "
                         "WHERE request_id=? AND data_instance_id=? AND state='OPEN'", (now(), request_id, instance))
        return {"request_id": request_id, "closed": row.rowcount == 1}


def cleanup_snapshot(db, kb_id):
    retained = retained_generations(db, kb_id)
    generations = [dict(row) for row in db.execute(
        "SELECT generation_id,state,collection_name,published_at,cleanup_state,collection_removed_at,manifest_sha256 "
        "FROM generations WHERE kb_id=? ORDER BY generation_id", (kb_id,)).fetchall()]
    targets = [row for row in generations if row["generation_id"] not in retained and not row["collection_removed_at"]
               and row["state"] in {"RETIRED", "FAILED", "ABORTED"} and row["cleanup_state"] in {"NONE", "FAILED"}]
    sessions = [dict(row) for row in db.execute(
        "SELECT session_id,generation_id,read_only FROM generation_sessions WHERE kb_id=? ORDER BY session_id", (kb_id,)).fetchall()]
    readers = [dict(row) for row in db.execute(
        "SELECT request_id,generation_id FROM generation_readers WHERE state='OPEN' "
        "AND generation_id IN (SELECT generation_id FROM generations WHERE kb_id=?) ORDER BY request_id", (kb_id,)).fetchall()]
    busy = [dict(row) for row in db.execute(
        "SELECT t.task_id,t.current_attempt_id,t.status FROM generation_inputs g JOIN tasks t USING(task_id) "
        "WHERE g.kb_id=? AND t.status IN ('PENDING','RUNNING') ORDER BY t.task_id", (kb_id,)).fetchall()]
    return {"kb_id": kb_id, "retained_generation_ids": retained, "generations": generations,
            "targets": targets, "affected_sessions": [row for row in sessions if row["generation_id"] not in retained],
            "active_readers": readers, "active_builds": busy}


def preview_cleanup(db, instance, kb_id):
    with transaction(db):
        require_instance(db, instance, importing=False)
        require_kb(db, kb_id)
        snapshot = cleanup_snapshot(db, kb_id)
        preview = new_id()
        digest = json_hash(snapshot)
        db.execute("INSERT INTO generation_cleanup_previews(preview_id,kb_id,data_instance_id,snapshot_json,snapshot_sha256,created_at) "
                   "VALUES (?,?,?,?,?,?)", (preview, kb_id, instance, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), digest, now()))
        return {"preview_id": preview, "preview_sha256": digest, **snapshot}


def confirm_cleanup(db, settings, instance, preview_id, expected_sha):
    with transaction(db):
        require_instance(db, instance, importing=False)
        preview = db.execute("SELECT * FROM generation_cleanup_previews WHERE preview_id=?", (preview_id,)).fetchone()
        if (preview is None or preview["data_instance_id"] != instance or preview["snapshot_sha256"] != expected_sha
                or preview["consumed_at"] is not None):
            raise BusinessError("CLEANUP_PREVIEW_INVALID", "清理预览失效或已使用，请重新预览。")
        snapshot = cleanup_snapshot(db, preview["kb_id"])
        if json_hash(snapshot) != expected_sha:
            raise BusinessError("CLEANUP_PREVIEW_CHANGED", "代次、会话或活动请求已经改变，请重新预览。")
        if snapshot["active_readers"] or snapshot["active_builds"]:
            raise BusinessError("CLEANUP_ACTIVE_REQUESTS", "请先等待所有活动问答和建库任务结束。")
        for target in snapshot["targets"]:
            if target["collection_name"] != collection_name(preview["kb_id"], target["generation_id"]):
                raise BusinessError("CLEANUP_COLLECTION_BINDING", "待清理集合名称不匹配。")
        db.execute("UPDATE generation_cleanup_previews SET consumed_at=? WHERE preview_id=?", (now(), preview_id))
        for target in snapshot["targets"]:
            db.execute("UPDATE generations SET cleanup_state='RUNNING' WHERE generation_id=?", (target["generation_id"],))
        retained = snapshot["retained_generation_ids"]
        for session in snapshot["affected_sessions"]:
            db.execute("UPDATE generation_sessions SET read_only=1 WHERE session_id=?", (session["session_id"],))
    deadline = time.monotonic() + 120.0

    def remaining():
        require_instance(db, instance, importing=False)
        if retained_generations(db, preview["kb_id"]) != retained:
            raise BusinessError("CLEANUP_RETAINED_CHANGED", "清理期间保留代次发生改变。")
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("cleanup budget exhausted")
        return budget

    client = None
    results = []
    try:
        client = DedicatedClient(settings.milvus_uri, remaining)
        for target in snapshot["targets"]:
            name = target["collection_name"]
            existed = client.has_collection(collection_name=name, timeout=min(30, remaining()))
            if existed:
                client.drop_collection(collection_name=name, timeout=min(30, remaining()))
            absent = not client.has_collection(collection_name=name, timeout=min(30, remaining()))
            if not absent:
                raise BusinessError("CLEANUP_COLLECTION_REMAINS", "清理后集合仍然存在。")
            with transaction(db):
                remaining()
                db.execute("UPDATE generations SET cleanup_state='SUCCEEDED',collection_removed_at=? WHERE generation_id=? "
                           "AND cleanup_state='RUNNING'", (now(), target["generation_id"]))
            results.append({"generation_id": target["generation_id"], "collection_name": name,
                            "collection_existed_before": existed, "collection_absent_after": absent,
                            "application_drop_call_count": int(existed), "physical_rpc_count": "NOT_OBSERVED"})
        return {"preview_id": preview_id, "results": results, "retained_generation_ids": retained,
                "local_history_policy": "RETAIN_FILES_AND_ORIGINAL_VERSION_RECORDS"}
    except Exception as error:
        with transaction(db):
            require_instance(db, instance, importing=False)
            for target in snapshot["targets"]:
                prior = db.execute("SELECT last_error_json FROM generations WHERE generation_id=?", (target["generation_id"],)).fetchone()
                details = {"scope": "MANUAL_CLEANUP", "preview_id": preview_id, "error_type": type(error).__name__,
                           "message": str(error), "automatic_retry": False, "previous_error": prior["last_error_json"]}
                db.execute("UPDATE generations SET cleanup_state='FAILED',last_error_json=? WHERE generation_id=? AND cleanup_state='RUNNING'",
                           (json.dumps(details, ensure_ascii=False, sort_keys=True), target["generation_id"]))
        raise
    finally:
        if client is not None:
            client.close()
