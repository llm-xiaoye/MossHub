"""Conversation deletion uses isolated SQLite and disposable request directories."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from raghub.chat.store import ChatStore
from raghub.config import Settings
from raghub.db import IdentityStore, migrate, new_id, now, open_db, system_state, transaction
from raghub.ingestion.store import BusinessError, IngestionStore

PROJECT = Path(__file__).resolve().parents[1]


class ConversationDeletionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-chat-delete-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.settings = replace(Settings.load(), home=self.root, project=PROJECT, sqlite=self.root / "state.sqlite3")
        self.db = open_db(self.settings.sqlite, create=True)
        self.addCleanup(lambda: self.db.close())
        migrate(self.db, PROJECT / "backend/migrations")
        self.instance = system_state(self.db)["data_instance_id"]
        self.kb = IngestionStore(self.db, self.settings).create_kb(self.instance, "disposable conversations")["kb_id"]
        task, attempt, _outbox = IdentityStore(self.db).create_task(
            kind="DOCUMENT", request_key=new_id(), request_sha256="a" * 64, budget_scope_id=new_id())
        self.generation = new_id()
        with transaction(self.db):
            for profile, kind in (("b" * 64, "EMBEDDING_SPACE"), ("c" * 64, "MODEL_EXECUTION_PROFILE")):
                self.db.execute("INSERT INTO model_profiles(profile_id,kind,config_json,created_at) VALUES (?,?,?,?)",
                                (profile, kind, "{}", now()))
            self.db.execute("INSERT INTO generations(generation_id,kb_id,task_id,attempt_id,state,collection_name,embedding_space_id,execution_profile_id,input_sha256,manifest_path,manifest_sha256,created_at,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (self.generation, self.kb, task, attempt, "CURRENT", "disposable_chat_generation",
                             "b" * 64, "c" * 64, "d" * 64, str(self.root / "generation.json"), "e" * 64, now(), now()))
            self.db.execute("UPDATE knowledge_bases SET current_generation_id=? WHERE kb_id=?", (self.generation, self.kb))
            self.db.execute("UPDATE system_state SET mode='QUERY' WHERE singleton=1")
        self.store = ChatStore(self.db, self.settings)

    def populated_conversation(self, conversation=None):
        if conversation is None:
            conversation = self.store.create_conversation(self.instance, new_id(), self.kb)["conversation_id"]
        ordinal = self.db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM chat_turns WHERE conversation_id=?", (conversation,)).fetchone()[0]
        request, user, assistant, summary = (new_id() for _ in range(4))
        directory = self.root / "data/retrieval" / request
        directory.mkdir(parents=True)
        trace = directory / "trace.json"
        raw = json.dumps({"conversation": conversation, "detail": "disposable local evidence"}).encode()
        trace.write_bytes(raw)
        trace.chmod(0o444)
        with transaction(self.db):
            self.db.execute("INSERT INTO retrieval_requests(request_id,session_id,data_instance_id,request_sha256,input_json,snapshot_json,state,owner_pid,owner_created_at,trace_path,trace_sha256,created_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (request, conversation, self.instance, "f" * 64, "{}", "{}", "SUCCEEDED",
                             os.getpid(), 1.0, str(trace), hashlib.sha256(raw).hexdigest(), now(), now()))
            self.db.execute("INSERT INTO chat_requests(request_id,conversation_id,data_instance_id,request_key,request_sha256,question,user_message_id,history_boundary,accepted_at,result_json,history_snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (request, conversation, self.instance, new_id(), "a" * 64, "disposable question", user, ordinal - 1, now(), "{}", "{}"))
            self.db.execute("INSERT INTO chat_turns(turn_id,conversation_id,ordinal,user_message_id,assistant_message_id,user_text,assistant_text,result_type,result_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (request, conversation, ordinal, user, assistant, "disposable question", "disposable answer", "answered", "{}", now()))
            self.db.execute("INSERT INTO chat_summaries(summary_id,conversation_id,covered_until_ordinal,value_json,request_id,created_at) VALUES (?,?,?,?,?,?)",
                            (summary, conversation, ordinal, '{"items":[]}', request, now()))
            self.db.execute("UPDATE chat_conversations SET summary_id=? WHERE conversation_id=?", (summary, conversation))
            self.db.execute("INSERT INTO chat_clarifications(clarification_id,conversation_id,version,state,source_turn_id,original_question,question,supplements_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (new_id(), conversation, 1, "RESOLVED", request, "disposable question", "detail", "[]", now()))
            self.db.execute("INSERT INTO provider_attempts(provider_attempt_id,request_id,purpose,budget_scope_id,state,created_at) VALUES (?,?,?,?,?,?)",
                            (new_id(), request, "disposable_record", conversation, "SUCCEEDED", now()))
            self.db.execute("INSERT INTO retrieval_model_operations(operation_id,request_id,registration_json,observation_json,updated_at) VALUES (?,?,?,?,?)",
                            (new_id(), request, "{}", "{}", now()))
            self.db.execute("INSERT INTO generation_readers(request_id,generation_id,data_instance_id,state,created_at,closed_at) VALUES (?,?,?,?,?,?)",
                            (request, self.generation, self.instance, "CLOSED", now(), now()))
        return conversation, request, directory

    def test_deletes_history_summary_details_files_and_preserves_other_conversation(self):
        target, request, directory = self.populated_conversation()
        other, other_request, other_directory = self.populated_conversation()
        other_bytes = (other_directory / "trace.json").read_bytes()
        result = self.store.delete_conversation(self.instance, target)
        self.assertTrue(result["deleted"])
        for table in ("chat_conversations", "chat_requests", "chat_turns", "chat_summaries", "chat_clarifications"):
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM " + table + " WHERE conversation_id=?", (target,)).fetchone()[0], 0)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM " + table + " WHERE conversation_id=?", (other,)).fetchone()[0], 1)
        for table in ("retrieval_requests", "provider_attempts", "retrieval_model_operations", "generation_readers"):
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM " + table + " WHERE request_id=?", (request,)).fetchone()[0], 0)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM " + table + " WHERE request_id=?", (other_request,)).fetchone()[0], 1)
        self.assertFalse(directory.exists())
        self.assertEqual((other_directory / "trace.json").read_bytes(), other_bytes)
        self.assertFalse((self.root / "data/chat-deletions" / (target + ".json")).exists())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM generation_sessions WHERE session_id=?", (target,)).fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM generations").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0], 1)
        for read in (lambda: self.store.conversation(self.instance, target), lambda: self.store.read(self.instance, request)):
            with self.assertRaises(BusinessError) as caught:
                read()
            self.assertEqual(caught.exception.status, 404)

    def test_active_request_and_open_reader_block_deletion_before_any_cleanup(self):
        target, request, directory = self.populated_conversation()
        for state, reader in (("RUNNING", "CLOSED"), ("CANCELLING", "CLOSED"), ("SUCCEEDED", "OPEN")):
            with self.subTest(state=state, reader=reader):
                self.db.execute("UPDATE retrieval_requests SET state=? WHERE request_id=?", (state, request))
                self.db.execute("UPDATE generation_readers SET state=? WHERE request_id=?", (reader, request))
                with self.assertRaises(BusinessError) as caught:
                    self.store.delete_conversation(self.instance, target)
                self.assertEqual(caught.exception.code, "CHAT_BUSY")
                self.assertTrue((directory / "trace.json").is_file())
                self.assertFalse((self.root / "data/chat-deletions" / (target + ".json")).exists())
                self.assertEqual(len(self.store.conversation(self.instance, target)["turns"]), 1)

    def test_symlink_is_not_followed_and_explicit_retry_finishes_pending_cleanup(self):
        target, _request, directory = self.populated_conversation()
        outside = self.root / "outside"
        directory.rename(outside)
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(BusinessError) as caught:
            self.store.delete_conversation(self.instance, target)
        self.assertEqual(caught.exception.code, "CONVERSATION_CLEANUP_PENDING")
        self.assertTrue((outside / "trace.json").is_file())
        self.assertTrue((self.root / "data/chat-deletions" / (target + ".json")).is_file())
        directory.unlink()
        outside.rename(directory)
        self.assertTrue(self.store.delete_conversation(self.instance, target)["deleted"])
        self.assertFalse(directory.exists())

    def test_restart_recovers_file_cleanup_after_database_deletion(self):
        target, _request, directory = self.populated_conversation()
        held = self.root / "held"
        directory.rename(held)
        directory.symlink_to(held, target_is_directory=True)
        with self.assertRaises(BusinessError):
            self.store.delete_conversation(self.instance, target)
        directory.unlink()
        held.rename(directory)
        self.db.close()
        self.db = open_db(self.settings.sqlite)
        self.store = ChatStore(self.db, self.settings)
        self.assertEqual(self.store.resume_conversation_deletions(), [])
        self.assertFalse(directory.exists())
        self.assertFalse((self.root / "data/chat-deletions" / (target + ".json")).exists())

    def test_database_failure_keeps_history_and_files_until_a_new_delete(self):
        target, request, directory = self.populated_conversation()
        self.db.execute("CREATE TRIGGER hold_chat_delete BEFORE DELETE ON chat_conversations BEGIN SELECT RAISE(ABORT, 'disposable database failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_conversation(self.instance, target)
        self.assertEqual(len(self.store.conversation(self.instance, target)["turns"]), 1)
        self.assertTrue((directory / "trace.json").is_file())
        self.assertEqual(self.store.resume_conversation_deletions(), [])
        self.assertTrue(directory.exists())
        self.db.execute("DROP TRIGGER hold_chat_delete")
        self.assertTrue(self.store.delete_conversation(self.instance, target)["deleted"])
        self.assertFalse(directory.exists())

    def test_rollback_plan_retry_refreshes_all_requests(self):
        target, request, directory = self.populated_conversation()
        self.db.execute("CREATE TRIGGER hold_chat_delete BEFORE DELETE ON chat_conversations BEGIN SELECT RAISE(ABORT, 'disposable database failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_conversation(self.instance, target)
        plan_path = self.root / "data/chat-deletions" / (target + ".json")
        self.assertEqual(json.loads(plan_path.read_bytes())["request_ids"], [request])
        self.db.execute("DROP TRIGGER hold_chat_delete")
        _same, later_request, later_directory = self.populated_conversation(target)
        self.assertEqual(len(self.store.conversation(self.instance, target)["turns"]), 2)
        old_plan = plan_path.read_bytes()
        self.db.execute("UPDATE retrieval_requests SET state='RUNNING' WHERE request_id=?", (later_request,))
        with self.assertRaises(BusinessError) as caught:
            self.store.delete_conversation(self.instance, target)
        self.assertEqual(caught.exception.code, "CHAT_BUSY")
        self.assertEqual(plan_path.read_bytes(), old_plan)
        self.assertTrue(directory.exists())
        self.assertTrue(later_directory.exists())
        self.db.execute("UPDATE retrieval_requests SET state='SUCCEEDED' WHERE request_id=?", (later_request,))
        self.assertTrue(self.store.delete_conversation(self.instance, target)["deleted"])
        self.assertFalse(directory.exists())
        self.assertFalse(later_directory.exists())
        self.assertFalse(plan_path.exists())
        for request_id in (request, later_request):
            self.assertIsNone(self.db.execute("SELECT 1 FROM retrieval_requests WHERE request_id=?", (request_id,)).fetchone())

    def test_startup_cancels_uncommitted_plan_without_removing_live_data(self):
        target, _request, directory = self.populated_conversation()
        original = (directory / "trace.json").read_bytes()
        self.db.execute("CREATE TRIGGER hold_chat_delete BEFORE DELETE ON chat_conversations BEGIN SELECT RAISE(ABORT, 'disposable database failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_conversation(self.instance, target)
        plan_path = self.root / "data/chat-deletions" / (target + ".json")
        self.assertTrue(plan_path.is_file())
        self.db.execute("DROP TRIGGER hold_chat_delete")
        self.assertEqual(self.store.resume_conversation_deletions(), [])
        self.assertFalse(plan_path.exists())
        self.assertEqual((directory / "trace.json").read_bytes(), original)
        self.assertEqual(len(self.store.conversation(self.instance, target)["turns"]), 1)
        self.assertTrue(self.store.delete_conversation(self.instance, target)["deleted"])
        self.assertFalse(directory.exists())

    def test_startup_preserves_malformed_and_cross_instance_plans(self):
        target, _request, directory = self.populated_conversation()
        self.db.execute("CREATE TRIGGER hold_chat_delete BEFORE DELETE ON chat_conversations BEGIN SELECT RAISE(ABORT, 'disposable database failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_conversation(self.instance, target)
        self.db.execute("DROP TRIGGER hold_chat_delete")
        plan_path = self.root / "data/chat-deletions" / (target + ".json")
        original = json.loads(plan_path.read_bytes())
        for change in ({"data_instance_id": new_id()}, {"schema": "invalid"}):
            with self.subTest(change=change):
                raw = json.dumps({**original, **change}).encode()
                plan_path.write_bytes(raw)
                failures = self.store.resume_conversation_deletions()
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["conversation_id"], target)
                self.assertEqual(plan_path.read_bytes(), raw)
                self.assertTrue((directory / "trace.json").is_file())
                self.assertEqual(len(self.store.conversation(self.instance, target)["turns"]), 1)

    def test_wrong_instance_cannot_delete_anything(self):
        target, _request, directory = self.populated_conversation()
        with self.assertRaises(BusinessError):
            self.store.delete_conversation(new_id(), target)
        self.assertTrue(directory.exists())
        self.assertEqual(len(self.store.conversation(self.instance, target)["turns"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
