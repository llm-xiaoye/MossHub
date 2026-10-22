"""Each request owns one SQLite scope, reader and immutable retrieval trace."""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import psutil

from ..db import now, open_db, transaction
from ..ingestion.archive import hash_file, json_hash, make_directory, uuid_text, write_closed
from ..ingestion.store import BusinessError, require_instance, require_kb
from ..indexing.cleanup import require_query_generation
from ..indexing.storage import decode, regular
from ..indexing.store import removal_snapshot
from ..model_profiles import canonical

ACTIVE = {"RUNNING", "CANCELLING"}


class RetrievalStore:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def create_session(self, instance, session_id, kb_id):
        with transaction(self.db):
            return self.create_session_locked(instance, session_id, kb_id)

    def create_session_locked(self, instance, session_id, kb_id):
        uuid_text(session_id)
        uuid_text(kb_id)
        state = require_instance(self.db, instance, importing=False)
        existing = self.db.execute("SELECT * FROM generation_sessions WHERE session_id=?", (session_id,)).fetchone()
        if existing is not None:
            if existing["kb_id"] != kb_id:
                raise BusinessError("REQUEST_KEY_CONFLICT", "会话请求键已用于其他知识库。")
            return dict(existing)
        if state["mode"] != "QUERY":
            raise BusinessError("QUERY_MODE_REQUIRED", "请先切换到问答模式。")
        kb = require_kb(self.db, kb_id)
        generation = require_query_generation(self.db, kb_id, kb["current_generation_id"])
        self.db.execute(
            "INSERT INTO generation_sessions(session_id,kb_id,generation_id,read_only,created_at) VALUES (?,?,?,0,?)",
            (session_id, kb_id, generation["generation_id"], now()))
        return dict(self.db.execute("SELECT * FROM generation_sessions WHERE session_id=?", (session_id,)).fetchone())

    def session(self, instance, session_id):
        require_instance(self.db, instance, importing=False)
        row = self.db.execute("SELECT * FROM generation_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise BusinessError("SESSION_NOT_FOUND", "会话不存在。", 404)
        return dict(row)

    def admit(self, instance, session_id, request_id, inputs):
        with transaction(self.db):
            return self.admit_locked(instance, session_id, request_id, inputs)

    def admit_locked(self, instance, session_id, request_id, inputs):
        uuid_text(request_id)
        fingerprint = json_hash({"session_id": session_id, "inputs": inputs})
        state = require_instance(self.db, instance, importing=False)
        previous = self.db.execute("SELECT * FROM retrieval_requests WHERE request_id=?", (request_id,)).fetchone()
        if previous is not None:
            if previous["data_instance_id"] != instance or previous["request_sha256"] != fingerprint:
                raise BusinessError("REQUEST_KEY_CONFLICT", "检索请求键已用于不同内容或实例。")
            return {"new": False, "request": dict(previous)}
        if state["mode"] != "QUERY":
            raise BusinessError("QUERY_MODE_REQUIRED", "请等待系统进入问答模式。")
        session = self.session(instance, session_id)
        if session["read_only"]:
            raise BusinessError("SESSION_READ_ONLY", "该旧会话只可查看历史，请创建当前代次的新会话。")
        generation = require_query_generation(self.db, session["kb_id"], session["generation_id"])
        active = self.db.execute(
            "SELECT count(*) FROM retrieval_requests WHERE data_instance_id=? AND state IN ('RUNNING','CANCELLING')",
            (instance,)).fetchone()[0]
        if active >= 2:
            raise BusinessError("QUERY_CAPACITY", "已有两个问答请求在执行或等待确认结束，请稍后再试。", 429)
        if self.db.execute("SELECT 1 FROM generation_readers WHERE request_id=?", (request_id,)).fetchone():
            raise BusinessError("REQUEST_KEY_CONFLICT", "该请求键已用于索引读取。")
        documents = [dict(row) for row in self.db.execute(
            "SELECT g.document_id,g.raw_version_id,g.processing_version_id,g.chunk_set_id,c.manifest_path,c.manifest_sha256 "
            "FROM generation_documents g JOIN chunk_sets c USING(chunk_set_id) WHERE g.generation_id=? ORDER BY g.document_id",
            (generation["generation_id"],)).fetchall()]
        snapshot = {"kb_id": session["kb_id"], "generation_id": session["generation_id"],
                    "data_instance_id": instance, "generation": generation, "documents": documents,
                    "removal_snapshot": removal_snapshot(self.db, session["kb_id"])}
        created = now()
        self.db.execute(
            "INSERT INTO generation_readers(request_id,generation_id,data_instance_id,state,created_at) VALUES (?,?,?,'OPEN',?)",
            (request_id, session["generation_id"], instance, created))
        self.db.execute(
            "INSERT INTO retrieval_requests(request_id,session_id,data_instance_id,request_sha256,input_json,snapshot_json,"
            "state,owner_pid,owner_created_at,created_at) VALUES (?,?,?,?,?,?,'RUNNING',?,?,?)",
            (request_id, session_id, instance, fingerprint, canonical(inputs).decode(),
             canonical(snapshot).decode(), os.getpid(), psutil.Process().create_time(), created))
        row = self.db.execute("SELECT * FROM retrieval_requests WHERE request_id=?", (request_id,)).fetchone()
        return {"new": True, "request": dict(row)}

    def read(self, instance, request_id):
        require_instance(self.db, instance, importing=False)
        row = self.db.execute("SELECT * FROM retrieval_requests WHERE request_id=? AND data_instance_id=?",
                              (request_id, instance)).fetchone()
        if row is None:
            raise BusinessError("RETRIEVAL_NOT_FOUND", "检索请求不存在。", 404)
        value = dict(row)
        if value["trace_path"]:
            target = regular(value["trace_path"], self.settings.home / "data/retrieval")
            if target.stat().st_size > 32 * 1024 * 1024:
                raise BusinessError("TRACE_INVALID", "检索轨迹超出读取上限。")
            raw = target.read_bytes()
            import hashlib
            if hashlib.sha256(raw).hexdigest() != value["trace_sha256"]:
                raise BusinessError("TRACE_INVALID", "检索轨迹指纹不匹配。")
            value["trace"] = decode(raw)
        value["snapshot"] = decode(value.pop("snapshot_json"))
        value["input"] = decode(value.pop("input_json"))
        value["error"] = decode(value.pop("error_json")) if value["error_json"] else None
        progress = value.pop("progress_json")
        value["progress"] = decode(progress) if progress else None
        return value

    def current_removals(self, instance, kb_id):
        require_instance(self.db, instance, importing=False)
        require_kb(self.db, kb_id)
        return removal_snapshot(self.db, kb_id)

    def fence(self, instance, request_id):
        require_instance(self.db, instance, importing=False)
        row = self.db.execute("SELECT state FROM retrieval_requests WHERE request_id=? AND data_instance_id=?",
                              (request_id, instance)).fetchone()
        if row is None or row["state"] != "RUNNING":
            raise BusinessError("QUERY_CANCELLED", "本轮已停止接收结果。")
        return True

    def register_operation(self, instance, request_id, registration):
        with transaction(self.db):
            self.fence(instance, request_id)
            self.db.execute(
                "INSERT INTO retrieval_model_operations(operation_id,request_id,registration_json,updated_at) VALUES (?,?,?,?)",
                (registration["operation_id"], request_id, canonical(registration).decode(), now()))

    def observe_operation(self, instance, request_id, operation_id, packet):
        with transaction(self.db):
            require_instance(self.db, instance, importing=False)
            row = self.db.execute(
                "UPDATE retrieval_model_operations SET observation_json=?,updated_at=? WHERE operation_id=? AND request_id=?",
                (canonical(packet).decode(), now(), operation_id, request_id))
            if row.rowcount != 1:
                raise BusinessError("MODEL_OWNERSHIP_LOST", "模型操作不属于当前请求。")

    def operations(self, instance, request_id):
        require_instance(self.db, instance, importing=False)
        return [{"registration": decode(row["registration_json"]),
                 "observation": decode(row["observation_json"]) if row["observation_json"] else None}
                for row in self.db.execute("SELECT * FROM retrieval_model_operations WHERE request_id=? ORDER BY updated_at,operation_id",
                                           (request_id,)).fetchall()]

    def save_progress(self, instance, request_id, trace):
        raw = canonical(trace)
        if len(raw) > 32 * 1024 * 1024:
            raise BusinessError("TRACE_TOO_LARGE", "检索进展超过32 MiB上限。")
        with transaction(self.db):
            require_instance(self.db, instance, importing=False)
            self.db.execute("UPDATE retrieval_requests SET progress_json=? WHERE request_id=? "
                            "AND data_instance_id=? AND state IN ('RUNNING','CANCELLING')",
                            (raw.decode(), request_id, instance))

    def cancel(self, instance, request_id):
        with transaction(self.db):
            require_instance(self.db, instance, importing=False)
            row = self.db.execute("SELECT state FROM retrieval_requests WHERE request_id=? AND data_instance_id=?",
                                  (request_id, instance)).fetchone()
            if row is None:
                raise BusinessError("RETRIEVAL_NOT_FOUND", "检索请求不存在。", 404)
            if row["state"] == "RUNNING":
                self.db.execute("UPDATE retrieval_requests SET state='CANCELLING' WHERE request_id=?", (request_id,))
            return {"request_id": request_id, "state": "CANCELLING" if row["state"] in ACTIVE else row["state"]}

    def _completion_record(self, row):
        directory = self.settings.home / "data/retrieval" / row["request_id"]
        if not directory.exists() and not directory.is_symlink():
            return None
        try:
            if directory.is_symlink() or not directory.is_dir() or directory.resolve(strict=True) != directory:
                raise ValueError("completion directory is not canonical")
            target = regular(directory / "trace.json", self.settings.home / "data/retrieval")
            import hashlib
            import stat
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > 32 * 1024 * 1024:
                    raise ValueError("completion record is not a bounded regular file")
                raw = handle.read(32 * 1024 * 1024 + 1)
                after = os.fstat(handle.fileno())
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, key) != getattr(after, key) for key in fields) or len(raw) != after.st_size:
                raise ValueError("completion record changed during its read")
            value = decode(raw)
            if type(value) is not dict or canonical(value) + b"\n" != raw:
                raise ValueError("completion record is partial or noncanonical")
            intent = value.get("finalization_intent")
            intent_keys = {"schema", "request_id", "session_id", "data_instance_id", "request_sha256",
                           "requested_state", "error", "deadline_monotonic", "clock_binding"}
            if type(intent) is not dict or set(intent) != intent_keys or intent["schema"] != "raghub.retrieval-finalization.v1":
                raise ValueError("completion record has no supported finalization intent")
            for key in ("request_id", "session_id", "data_instance_id", "request_sha256"):
                if intent[key] != row[key]:
                    raise ValueError("completion intent does not belong to this request")
            if (value.get("schema") != "raghub.retrieval-trace.v1"
                    or value.get("request_id") != row["request_id"]
                    or value.get("session_id") != row["session_id"]
                    or value.get("created_at") != row["created_at"]
                    or value.get("scope") != decode(row["snapshot_json"])
                    or value.get("input") != decode(row["input_json"])):
                raise ValueError("completion trace does not match the admitted request")
            if intent["requested_state"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
                raise ValueError("completion intent has an unknown terminal state")
            if intent["error"] is not None and type(intent["error"]) is not dict:
                raise ValueError("completion error must be an object or null")
            completion = value.get("completion")
            if (type(completion) is not dict or completion.get("requested_state") != intent["requested_state"]
                    or "error" not in completion or completion["error"] != intent["error"]):
                raise ValueError("completion summary disagrees with the first intent")
            if intent["requested_state"] == "SUCCEEDED":
                if intent["error"] is not None:
                    raise ValueError("successful completion has an error")
                import math
                deadline = intent["deadline_monotonic"]
                clock = intent["clock_binding"]
                if (type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline <= 0
                        or type(clock) is not dict or set(clock) != {"boot_time", "implementation"}
                        or type(clock["boot_time"]) not in {int, float} or not math.isfinite(clock["boot_time"])
                        or type(clock["implementation"]) is not str or not clock["implementation"]):
                    raise ValueError("successful completion has no bound original deadline")
            elif intent["deadline_monotonic"] is not None or intent["clock_binding"] is not None:
                raise ValueError("non-success intent has an unexpected deadline")
            return {"trace": value, "intent": intent, "path": str(target),
                    "sha256": hashlib.sha256(raw).hexdigest()}
        except Exception as caught:
            raise BusinessError("FINALIZATION_BLOCKED", "已存在的收尾记录不完整或绑定不符，保留原件与活动 reader。") from caught

    def stored_completion(self, instance, request_id):
        require_instance(self.db, instance, importing=False)
        row = self.db.execute("SELECT * FROM retrieval_requests WHERE request_id=? AND data_instance_id=?",
                              (request_id, instance)).fetchone()
        if row is None:
            raise BusinessError("RETRIEVAL_NOT_FOUND", "检索请求不存在。", 404)
        return self._completion_record(row)

    def record_finalization_error(self, instance, request_id, phase, diagnostic):
        with transaction(self.db):
            require_instance(self.db, instance, importing=False)
            self.db.execute("UPDATE retrieval_requests SET error_json=? WHERE request_id=? "
                            "AND data_instance_id=? AND state IN ('RUNNING','CANCELLING')",
                            (canonical({"code": "FINALIZATION_BLOCKED", "phase": phase,
                                        "cause": diagnostic, "observed_at": now()}).decode(), request_id, instance))

    def finish(self, instance, request_id, state, trace, error=None, deadline=None):
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            raise ValueError("invalid retrieval completion")
        require_instance(self.db, instance, importing=False)
        row = self.db.execute("SELECT * FROM retrieval_requests WHERE request_id=? AND data_instance_id=?",
                              (request_id, instance)).fetchone()
        if row is None:
            raise BusinessError("RETRIEVAL_NOT_FOUND", "检索请求不存在。", 404)
        record = self._completion_record(row)
        if record is None:
            if row["state"] not in ACTIVE:
                raise BusinessError("REQUEST_STATE_CHANGED", "检索请求终态已改变。")
            if type(trace) is not dict or "finalization_intent" in trace:
                raise ValueError("new trace must not supply its own finalization intent")
            clock = None
            if state == "SUCCEEDED":
                import math
                import time
                if type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline <= 0:
                    raise ValueError("successful completion requires its original deadline")
                clock = {"boot_time": psutil.boot_time(),
                         "implementation": time.get_clock_info("monotonic").implementation}
            value = dict(trace)
            value.setdefault("request_id", request_id)
            value.setdefault("session_id", row["session_id"])
            value.setdefault("created_at", row["created_at"])
            completion = value.get("completion")
            if (value.get("schema") != "raghub.retrieval-trace.v1"
                    or value["request_id"] != request_id or value["session_id"] != row["session_id"]
                    or value["created_at"] != row["created_at"]
                    or value.get("scope") != decode(row["snapshot_json"])
                    or value.get("input") != decode(row["input_json"])
                    or type(completion) is not dict or completion.get("requested_state") != state
                    or "error" not in completion or completion["error"] != error
                    or (error is not None and type(error) is not dict)
                    or (state == "SUCCEEDED" and error is not None)):
                raise ValueError("new completion does not match the admitted request and result")
            value["finalization_intent"] = {"schema": "raghub.retrieval-finalization.v1",
                "request_id": request_id, "session_id": row["session_id"], "data_instance_id": instance,
                "request_sha256": row["request_sha256"], "requested_state": state, "error": error,
                "deadline_monotonic": deadline if state == "SUCCEEDED" else None, "clock_binding": clock}
            raw = canonical(value) + b"\n"
            if len(raw) > 32 * 1024 * 1024:
                raise BusinessError("TRACE_TOO_LARGE", "完整检索轨迹超过32 MiB上限。")
            directory = self.settings.home / "data/retrieval" / request_id
            make_directory(directory, self.settings.home, exclusive=True)
            target = directory / "trace.json"
            write_closed(target, raw)
            target.chmod(0o444)
            record = self._completion_record(row)
            if record is None:
                raise BusinessError("FINALIZATION_BLOCKED", "首次完整记录已不可读，保留活动 reader。")
        # The first complete file is authoritative even if a later caller has
        # a different in-memory completion. No trace file is reopened for write.
        intent = record["intent"]
        state, error = intent["requested_state"], intent["error"]
        with transaction(self.db):
            require_instance(self.db, instance, importing=False)
            current = self.db.execute("SELECT state,trace_path,trace_sha256 FROM retrieval_requests "
                                      "WHERE request_id=? AND data_instance_id=?", (request_id, instance)).fetchone()
            if current is None:
                raise BusinessError("REQUEST_STATE_CHANGED", "检索请求已改变，保留原轨迹。")
            if current["state"] not in ACTIVE:
                if current["trace_path"] != record["path"] or current["trace_sha256"] != record["sha256"]:
                    raise BusinessError("REQUEST_STATE_CHANGED", "检索请求已绑定其他终态记录。")
            else:
                if current["state"] == "CANCELLING" and state == "SUCCEEDED":
                    state, error = "CANCELLED", {"code": "QUERY_CANCELLED", "message": "停止请求先于结果提交。"}
                if state == "SUCCEEDED":
                    import time
                    clock = {"boot_time": psutil.boot_time(),
                             "implementation": time.get_clock_info("monotonic").implementation}
                    if clock != intent["clock_binding"]:
                        state, error = "FAILED", {"code": "QUERY_DEADLINE_CLOCK_UNKNOWN", "message": "原始预算时钟无法确认，不接收成功结果。"}
                    elif time.monotonic() >= intent["deadline_monotonic"]:
                        state, error = "FAILED", {"code": "QUERY_DEADLINE", "message": "整轮预算在结果提交前耗尽。"}
                completed = now()
                state, error = self.finish_related(instance, request_id, state, record["trace"], error, completed)
                self.db.execute(
                    "UPDATE retrieval_requests SET state=?,trace_path=?,trace_sha256=?,error_json=?,finished_at=? WHERE request_id=?",
                    (state, record["path"], record["sha256"], canonical(error).decode() if error else None, completed, request_id))
                self.db.execute("UPDATE generation_readers SET state='CLOSED',closed_at=? WHERE request_id=? AND state='OPEN'",
                                (completed, request_id))
        return self.read(instance, request_id)

    def finish_related(self, instance, request_id, state, trace, error, completed):
        return state, error

    def active(self):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM retrieval_requests WHERE state IN ('RUNNING','CANCELLING') ORDER BY created_at").fetchall()]


class AsyncStore:
    """Connections live entirely on two DB threads; they never cross threads."""
    def __init__(self, settings, store_type=RetrievalStore):
        self.settings = settings
        self.store_type = store_type
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rag-query-db")

    async def call(self, method, *args):
        def invoke():
            with closing(open_db(self.settings.sqlite)) as db:
                return getattr(self.store_type(db, self.settings), method)(*args)
        return await asyncio.get_running_loop().run_in_executor(self.pool, invoke)

    async def close(self):
        self.pool.shutdown(wait=True, cancel_futures=False)
