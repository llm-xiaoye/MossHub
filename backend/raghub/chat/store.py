"""Chat writes share the retrieval admission and terminal transaction."""
from __future__ import annotations
import json
from uuid import uuid4
from ..db import now, transaction
from ..ingestion.archive import json_hash, uuid_text
from ..ingestion.store import BusinessError, require_instance
from ..indexing.cleanup import require_query_generation
from ..indexing.store import removal_snapshot
from ..indexing.storage import decode
from ..model_profiles import canonical
from ..retrieval.contracts import SearchRequest, normalized_queries
from ..retrieval.store import RetrievalStore, ACTIVE
from .query import clarification_original_question

class ChatStore(RetrievalStore):
    def create_conversation(self, instance, key, kb_id):
        uuid_text(key)
        uuid_text(kb_id)
        fingerprint = json_hash({"kb_id":kb_id})
        # create_session owns one transaction; this wrapper uses its locked primitive.
        with transaction(self.db):
            require_instance(self.db,instance,importing=False)
            old = self.db.execute("SELECT * FROM chat_conversations WHERE data_instance_id=? AND request_key=?",(instance,key)).fetchone()
            if old is not None:
                if old["request_sha256"] != fingerprint:
                    raise BusinessError("IDEMPOTENCY_CONFLICT","该请求键已用于其他创建内容。",409)
                return self.conversation(instance,old["conversation_id"])
            conversation_id = str(uuid4())
            self.create_session_locked(instance,conversation_id,kb_id)
            self.db.execute("INSERT INTO chat_conversations(conversation_id,data_instance_id,request_key,request_sha256,created_at) VALUES (?,?,?,?,?)",
                            (conversation_id,instance,key,fingerprint,now()))
            return self.conversation(instance,conversation_id)

    def conversation(self, instance, conversation_id):
        require_instance(self.db,instance,importing=False)
        row = self.db.execute("SELECT c.*,s.kb_id,s.generation_id,s.read_only FROM chat_conversations c JOIN generation_sessions s ON s.session_id=c.conversation_id WHERE c.conversation_id=? AND c.data_instance_id=?",
                              (conversation_id,instance)).fetchone()
        if row is None:
            raise BusinessError("CONVERSATION_NOT_FOUND","会话不存在。",404)
        value = dict(row)
        value["turns"] = [dict(v) for v in self.db.execute("SELECT * FROM chat_turns WHERE conversation_id=? ORDER BY ordinal",(conversation_id,)).fetchall()]
        for turn in value["turns"]:
            turn["result"] = decode(turn.pop("result_json"))
        value["awaiting"] = self.awaiting(conversation_id)
        value["summary"] = self.summary(value["summary_id"])
        value["requests"] = [dict(v) for v in self.db.execute("SELECT c.request_id,c.question,c.user_message_id,c.accepted_at,r.state,r.error_json FROM chat_requests c JOIN retrieval_requests r USING(request_id) WHERE c.conversation_id=? ORDER BY c.accepted_at",(conversation_id,)).fetchall()]
        for request in value["requests"]:
            request["error"] = decode(request.pop("error_json")) if request["error_json"] else None
        return value

    def awaiting(self, conversation_id):
        row = self.db.execute("SELECT * FROM chat_clarifications WHERE conversation_id=? AND state='AWAITING'",(conversation_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["supplements"] = decode(value.pop("supplements_json"))
        return value

    def summary(self, summary_id):
        if summary_id is None:
            return None
        row = self.db.execute("SELECT value_json FROM chat_summaries WHERE summary_id=?",(summary_id,)).fetchone()
        if row is None:
            raise BusinessError("SUMMARY_POINTER_INVALID","摘要版本不可读取。")
        return decode(row["value_json"])

    def admit_chat(self, instance, conversation_id, key, question):
        uuid_text(key)
        fingerprint = json_hash({"conversation_id":conversation_id,"question":question})
        with transaction(self.db):
            require_instance(self.db,instance,importing=False)
            # Exact retry lookup precedes mode/readiness/capacity; no provider replay.
            old = self.db.execute("SELECT * FROM chat_requests WHERE data_instance_id=? AND request_key=?",(instance,key)).fetchone()
            if old is not None:
                if old["request_sha256"] != fingerprint:
                    raise BusinessError("IDEMPOTENCY_CONFLICT","相同请求键对应不同内容。",409)
                row = self.db.execute("SELECT * FROM retrieval_requests WHERE request_id=?",(old["request_id"],)).fetchone()
                return {"new":False,"request":dict(row)}
            conv = self.conversation(instance,conversation_id)
            if self.db.execute("SELECT 1 FROM retrieval_requests WHERE session_id=? AND state IN ('RUNNING','CANCELLING')",(conversation_id,)).fetchone():
                raise BusinessError("CHAT_BUSY","该会话有尚未实际结束的请求。",409)
            request_id, user_message_id = str(uuid4()), str(uuid4())
            inputs = normalized_queries(SearchRequest(effective_original_query=question))
            try:
                admitted = self.admit_locked(instance,conversation_id,request_id,inputs)
            except BusinessError as caught:
                if caught.code == "QUERY_CAPACITY":
                    raise BusinessError("SYSTEM_BUSY","已有两个问答请求，请稍后再试。",429) from caught
                raise
            boundary = conv["turns"][-1]["ordinal"] if conv["turns"] else 0
            self.db.execute("INSERT INTO chat_requests(request_id,conversation_id,data_instance_id,request_key,request_sha256,question,user_message_id,history_boundary,awaiting_json,accepted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (request_id,conversation_id,instance,key,fingerprint,question,user_message_id,boundary,
                             canonical(conv["awaiting"]).decode() if conv["awaiting"] else None,now()))
            if conv["awaiting"] is not None:
                # Raw supplements survive technical failure separately from completed history.
                awaiting = conv["awaiting"]
                supplements = awaiting["supplements"] + [{"request_id":request_id,"message_id":user_message_id,"text":question}]
                self.db.execute("UPDATE chat_clarifications SET supplements_json=? WHERE clarification_id=? AND version=? AND state='AWAITING'",
                                (canonical(supplements).decode(),awaiting["clarification_id"],awaiting["version"]))
            return admitted

    def context(self, instance, request_id):
        with transaction(self.db):
            self.fence(instance,request_id)
            row = self.db.execute("SELECT * FROM chat_requests WHERE request_id=? AND data_instance_id=?",(request_id,instance)).fetchone()
            if row is None:
                return None
            conv = self.conversation(instance,row["conversation_id"])
            self.db.execute("UPDATE chat_requests SET started_at=COALESCE(started_at,?) WHERE request_id=?",(now(),request_id))
            turns = [t for t in conv["turns"] if t["ordinal"] <= row["history_boundary"]]
            # Admission row order is real, persisted conversation order, including
            # failed supplements. A completed assistant follows its own user message.
            message_order = {}
            orders = self.db.execute(
                "SELECT q.rowid AS admitted_order,q.user_message_id,t.assistant_message_id "
                "FROM chat_requests q LEFT JOIN chat_turns t ON t.turn_id=q.request_id "
                "WHERE q.conversation_id=? AND q.request_id<>? ORDER BY q.rowid",
                (row["conversation_id"],request_id)).fetchall()
            for original in orders:
                message_order[original["user_message_id"]] = {"position":[original["admitted_order"],0],"role":"user"}
                if original["assistant_message_id"] is not None:
                    message_order[original["assistant_message_id"]] = {"position":[original["admitted_order"],1],"role":"assistant"}
            return {"request":dict(row),"conversation":{k:conv[k] for k in ("conversation_id","kb_id","generation_id","summary_id")},
                    "turns":turns,"summary":conv["summary"],"awaiting":decode(row["awaiting_json"]) if row["awaiting_json"] else None,
                    "clarification_chain":self.clarification_chain(row,turns),"source_message_order":message_order}

    def clarification_chain(self, row, turns):
        awaiting = self.awaiting(row["conversation_id"])
        if awaiting is None:
            return None
        origin = next((t for t in turns if t["turn_id"] == awaiting["source_turn_id"]),None)
        if origin is None:
            raise BusinessError("CLARIFICATION_SOURCE_MISSING","等待事项的原始已完成澄清轮次不可读取。")
        messages = []
        for turn in turns:
            if turn["ordinal"] >= origin["ordinal"]:
                messages.extend([{"message_id":turn["user_message_id"],"role":"user","text":turn["user_text"]},
                                 {"message_id":turn["assistant_message_id"],"role":"assistant","text":turn["assistant_text"]}])
        known = {m["message_id"] for m in messages}
        for supplement in awaiting["supplements"]:
            if supplement["message_id"] not in known and supplement["message_id"] != row["user_message_id"]:
                messages.append({"message_id":supplement["message_id"],"role":"user","text":supplement["text"]})
                known.add(supplement["message_id"])
        return {"start_ordinal":origin["ordinal"],"messages":messages}

    def save_history(self, instance, request_id, snapshot):
        with transaction(self.db):
            self.fence(instance,request_id)
            row = self.db.execute("UPDATE chat_requests SET history_snapshot_json=? WHERE request_id=? AND data_instance_id=?",
                                  (canonical(snapshot).decode(),request_id,instance))
            if row.rowcount != 1:
                raise BusinessError("CONVERSATION_NOT_FOUND","会话已删除，拒绝历史写入。",404)

    def publish_summary(self, instance, request_id, value, previous_id, boundary):
        with transaction(self.db):
            self.fence(instance,request_id)
            row = self.db.execute("SELECT c.conversation_id,c.summary_id,q.history_boundary FROM chat_requests q JOIN chat_conversations c USING(conversation_id) WHERE q.request_id=? AND q.data_instance_id=?",(request_id,instance)).fetchone()
            if row is None or row["summary_id"] != previous_id or row["history_boundary"] < boundary:
                raise BusinessError("SUMMARY_REVISION_CONFLICT","历史或摘要版本已变化，保留旧摘要。")
            self.db.execute("INSERT INTO chat_summaries(summary_id,conversation_id,previous_summary_id,covered_until_ordinal,value_json,request_id,created_at) VALUES (?,?,?,?,?,?,?)",
                            (value["summary_id"],row["conversation_id"],previous_id,boundary,canonical(value).decode(),request_id,now()))
            self.db.execute("UPDATE chat_conversations SET summary_id=? WHERE conversation_id=? AND summary_id IS ?",
                            (value["summary_id"],row["conversation_id"],previous_id))
            return value

    def provider_begin(self, instance, request_id, call_id, purpose):
        with transaction(self.db):
            self.fence(instance,request_id)
            self.db.execute("INSERT INTO provider_attempts(provider_attempt_id,request_id,purpose,budget_scope_id,state,created_at) VALUES (?,?,?,?,'REGISTERED',?)",
                            (call_id,request_id,purpose,request_id,now()))

    def provider_sent(self, instance, request_id, call_id):
        with transaction(self.db):
            self.fence(instance,request_id)
            self.db.execute("UPDATE provider_attempts SET state='SENT',sent_at=? WHERE provider_attempt_id=? AND request_id=? AND state='REGISTERED'",(now(),call_id,request_id))

    def provider_end(self, instance, request_id, call_id, state):
        with transaction(self.db):
            require_instance(self.db,instance,importing=False)
            self.db.execute("UPDATE provider_attempts SET state=?,result_at=? WHERE provider_attempt_id=? AND request_id=?",(state,now(),call_id,request_id))

    def finish_related(self, instance, request_id, state, trace, error, completed):
        row = self.db.execute("SELECT * FROM chat_requests WHERE request_id=? AND data_instance_id=?",(request_id,instance)).fetchone()
        if row is None:
            return state,error
        conversation_id = row["conversation_id"]
        conv = self.db.execute("SELECT * FROM chat_conversations WHERE conversation_id=? AND data_instance_id=?",(conversation_id,instance)).fetchone()
        if conv is None:
            raise BusinessError("CONVERSATION_NOT_FOUND","会话已删除，拒绝迟到提交。",404)
        if state == "SUCCEEDED":
            chat = trace.get("chat")
            if type(chat) is not dict or type(chat.get("formal_result")) is not dict:
                state,error = "FAILED",{"code":"ANSWER_NOT_VALIDATED","message":"整轮没有已校验的正式结果。"}
            else:
                current_ordinal = self.db.execute("SELECT COALESCE(MAX(ordinal),0) FROM chat_turns WHERE conversation_id=?",(conversation_id,)).fetchone()[0]
                awaiting = self.awaiting(conversation_id)
                expected = decode(row["awaiting_json"]) if row["awaiting_json"] else None
                same_awaiting = ((awaiting is None and expected is None) or (awaiting is not None and expected is not None
                    and all(awaiting[k] == expected[k] for k in ("clarification_id","version","source_turn_id"))))
                scope = trace["scope"]
                current_removal = removal_snapshot(self.db,scope["kb_id"])
                try:
                    require_query_generation(self.db,scope["kb_id"],scope["generation_id"])
                    scope_valid = current_removal == scope["removal_snapshot"]
                except BusinessError:
                    scope_valid = False
                if current_ordinal != row["history_boundary"] or not same_awaiting or not scope_valid:
                    state,error = "FAILED",{"code":"CHAT_SNAPSHOT_CHANGED","message":"历史、澄清或资料范围改变，整条结果不提交。"}
                else:
                    result, action = chat["formal_result"], chat["understanding"]["main_action"]
                    resolution = chat["understanding"]["clarification_resolution"]
                    starts_new_awaiting = action == "clarify" and (expected is None or (type(resolution) is dict and resolution.get("intent") in {"cancel","replace"}))
                    new_original_question = row["question"]
                    if starts_new_awaiting:
                        try:
                            new_original_question = clarification_original_question(row["question"],chat)
                        except ValueError as caught:
                            raise BusinessError("CLARIFICATION_ORIGINAL_INVALID",str(caught)) from caught
                    if expected is not None:
                        if resolution is None or any(resolution[k] != expected[k] for k in ("clarification_id","source_turn_id")) or resolution["clarification_version"] != expected["version"]:
                            raise BusinessError("CLARIFICATION_BINDING_INVALID","澄清提交没有绑定当前等待版本。")
                        intent = resolution["intent"]
                        if action == "clarify" and intent == "keep":
                            self.db.execute("UPDATE chat_clarifications SET question=?,version=version+1,updated_at=? WHERE clarification_id=? AND version=? AND state='AWAITING'",
                                            (chat["understanding"]["clarification"],completed,expected["clarification_id"],expected["version"]))
                        else:
                            target = {"cancel":"CANCELLED","replace":"REPLACED","complete":"RESOLVED","keep":"RESOLVED"}[intent]
                            self.db.execute("UPDATE chat_clarifications SET state=?,version=version+1,updated_at=? WHERE clarification_id=? AND version=? AND state='AWAITING'",(target,completed,expected["clarification_id"],expected["version"]))
                    if starts_new_awaiting:
                        self.db.execute("INSERT INTO chat_clarifications(clarification_id,conversation_id,version,state,source_turn_id,original_question,question,supplements_json,updated_at) VALUES (?,?,1,'AWAITING',?,?,?,'[]',?)",
                                        (str(uuid4()),conversation_id,request_id,new_original_question,chat["understanding"]["clarification"],completed))
                    self.db.execute("INSERT INTO chat_turns(turn_id,conversation_id,ordinal,user_message_id,assistant_message_id,user_text,assistant_text,result_type,result_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    (request_id,conversation_id,current_ordinal+1,row["user_message_id"],str(uuid4()),row["question"],result["text"],result["result_type"],canonical(result).decode(),completed))
                    self.db.execute("UPDATE chat_requests SET result_json=? WHERE request_id=?",(canonical(result).decode(),request_id))
        return state,error

    def question_request(self, instance, request_id):
        self.conversation_for_request(instance,request_id)
        carrier_row = self.db.execute("SELECT state,error_json,trace_path,trace_sha256,json_extract(progress_json,'$.progress') AS progress_json FROM retrieval_requests WHERE request_id=? AND data_instance_id=?",(request_id,instance)).fetchone()
        if carrier_row is None:
            raise BusinessError("QUESTION_NOT_FOUND","请求不存在。",404)
        carrier = dict(carrier_row)
        carrier["error"] = decode(carrier.pop("error_json")) if carrier["error_json"] else None
        row = self.db.execute("SELECT * FROM chat_requests WHERE request_id=? AND data_instance_id=?",(request_id,instance)).fetchone()
        state = carrier["state"]
        if state == "RUNNING" and row["started_at"] is None:
            state = "ACCEPTED"
        return {"request_id":request_id,"conversation_id":row["conversation_id"],"data_instance_id":instance,
                "state":state,"question":row["question"],"accepted_at":row["accepted_at"],
                "result":decode(row["result_json"]) if row["result_json"] else None,
                "error":carrier["error"],"trace_path":carrier["trace_path"],"trace_sha256":carrier["trace_sha256"],
                "progress":decode(carrier["progress_json"]) if carrier["progress_json"] else None,
                "history_snapshot":decode(row["history_snapshot_json"]) if row["history_snapshot_json"] else None}

    def conversation_for_request(self, instance, request_id):
        row = self.db.execute("SELECT conversation_id FROM chat_requests WHERE request_id=? AND data_instance_id=?",(request_id,instance)).fetchone()
        if row is None:
            raise BusinessError("QUESTION_NOT_FOUND","请求不存在。",404)
        require_instance(self.db,instance,importing=False)
        if self.db.execute("SELECT 1 FROM chat_conversations WHERE conversation_id=? AND data_instance_id=?",(row["conversation_id"],instance)).fetchone() is None:
            raise BusinessError("CONVERSATION_NOT_FOUND","会话不存在。",404)
        return row["conversation_id"]

    def _conversation_deletion_plan(self, instance, conversation_id):
        from ..indexing.storage import regular
        root = self.settings.home / "data/chat-deletions"
        path = root / (conversation_id + ".json")
        if not path.exists() and not path.is_symlink():
            return path, None
        target = regular(path, root)
        if target.stat().st_size > 1024 * 1024:
            raise ValueError("conversation deletion plan exceeds its bound")
        plan = decode(target.read_bytes())
        if (type(plan) is not dict or set(plan) != {"schema", "data_instance_id", "conversation_id", "request_ids"}
                or plan["schema"] != "raghub.conversation-deletion.v1"
                or plan["data_instance_id"] != instance or plan["conversation_id"] != conversation_id
                or type(plan["request_ids"]) is not list
                or any(type(value) is not str for value in plan["request_ids"])
                or plan["request_ids"] != sorted(set(plan["request_ids"]))):
            raise ValueError("conversation deletion plan has an invalid scope")
        for request_id in plan["request_ids"]:
            uuid_text(request_id)
        return path, plan

    def _purge_conversation_files(self, path, plan):
        import shutil
        from ..ingestion.archive import inside
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("safe directory removal is unavailable")
        if self.db.execute("SELECT 1 FROM chat_conversations WHERE conversation_id=?", (plan["conversation_id"],)).fetchone():
            raise RuntimeError("conversation is still live")
        targets = []
        for request_id in plan["request_ids"]:
            if self.db.execute("SELECT 1 FROM retrieval_requests WHERE request_id=?", (request_id,)).fetchone():
                raise RuntimeError("request is still live")
            target = self.settings.home / "data/retrieval" / request_id
            inside(target, self.settings.home)
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise RuntimeError("request directory has an unexpected object type")
            targets.append(target)
        for target in targets:
            if target.exists():
                shutil.rmtree(target)
        path.unlink()

    def resume_conversation_deletions(self):
        from ..db import system_state
        from ..ingestion.archive import inside
        instance = system_state(self.db)["data_instance_id"]
        root = self.settings.home / "data/chat-deletions"
        inside(root, self.settings.home)
        if not root.exists():
            return []
        failures = []
        for path in sorted(root.glob("*.json")):
            try:
                uuid_text(path.stem)
                with transaction(self.db):
                    instance = system_state(self.db)["data_instance_id"]
                    path, plan = self._conversation_deletion_plan(instance, path.stem)
                    if plan is None:
                        continue
                    if self.db.execute("SELECT 1 FROM chat_conversations WHERE conversation_id=?", (path.stem,)).fetchone():
                        # A live row means deletion never committed. Cancel only its work record.
                        path.unlink()
                        continue
                self._purge_conversation_files(path, plan)
            except (OSError, ValueError, RuntimeError) as caught:
                failures.append({"conversation_id": path.stem, "error": str(caught)})
        return failures

    def delete_conversation(self, instance, conversation_id):
        from ..ingestion.archive import make_directory, write_closed
        uuid_text(conversation_id)
        with transaction(self.db):
            require_instance(self.db, instance, importing=False)
            path, plan = self._conversation_deletion_plan(instance, conversation_id)
            row = self.db.execute("SELECT 1 FROM chat_conversations WHERE conversation_id=? AND data_instance_id=?",
                                  (conversation_id, instance)).fetchone()
            if row is None and plan is None:
                raise BusinessError("CONVERSATION_NOT_FOUND", "会话不存在。", 404)
            if row is not None:
                if self.db.execute("SELECT 1 FROM retrieval_requests WHERE session_id=? AND state IN ('RUNNING','CANCELLING')", (conversation_id,)).fetchone() or self.db.execute("SELECT 1 FROM generation_readers g JOIN retrieval_requests r USING(request_id) WHERE r.session_id=? AND g.state='OPEN'", (conversation_id,)).fetchone():
                    raise BusinessError("CHAT_BUSY", "活动请求实际结束前不能删除会话。", 409)
                request_ids = [r[0] for r in self.db.execute("SELECT request_id FROM retrieval_requests WHERE session_id=? ORDER BY request_id", (conversation_id,)).fetchall()]
                expected = {"schema": "raghub.conversation-deletion.v1", "data_instance_id": instance,
                            "conversation_id": conversation_id, "request_ids": request_ids}
                raw = canonical(expected) + b"\n"
                if len(raw) > 1024 * 1024:
                    raise BusinessError("CONVERSATION_TOO_LARGE", "会话清理记录超过大小上限。", 409)
                if plan is not None:
                    # The live row and current request set are locked in this transaction.
                    # A prior rollback may have left an older, uncommitted cleanup plan.
                    path.unlink()
                make_directory(path.parent, self.settings.home)
                write_closed(path, raw)
                plan = expected
                self.db.execute("UPDATE chat_conversations SET summary_id=NULL WHERE conversation_id=?", (conversation_id,))
                for table in ("chat_summaries", "chat_clarifications", "chat_turns", "chat_requests"):
                    self.db.execute("DELETE FROM " + table + " WHERE conversation_id=?", (conversation_id,))
                for request_id in request_ids:
                    self.db.execute("DELETE FROM provider_attempts WHERE request_id=?", (request_id,))
                    self.db.execute("DELETE FROM retrieval_model_operations WHERE request_id=?", (request_id,))
                    self.db.execute("DELETE FROM generation_readers WHERE request_id=?", (request_id,))
                    self.db.execute("DELETE FROM retrieval_requests WHERE request_id=?", (request_id,))
                self.db.execute("DELETE FROM chat_conversations WHERE conversation_id=?", (conversation_id,))
                self.db.execute("DELETE FROM generation_sessions WHERE session_id=?", (conversation_id,))
        try:
            self._purge_conversation_files(path, plan)
        except (OSError, ValueError, RuntimeError) as caught:
            raise BusinessError("CONVERSATION_CLEANUP_PENDING", "聊天记录已删除，本地详情文件清理未完成；请重试删除，或重新启动服务继续清理。", 409) from caught
        return {"deleted": True, "conversation_id": conversation_id}
