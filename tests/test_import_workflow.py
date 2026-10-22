"""Affected workflow contracts; no cloud providers or personal-vault writes."""
from __future__ import annotations
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import test_obsidian_import as fixtures
from raghub.db import new_id, open_db
from raghub.ingestion.store import BusinessError
from raghub.import_workflow import ImportWorkflow
from raghub.obsidian import Vault
from raghub.indexing.store import scoped_documents

class WorkflowTest(unittest.TestCase):
    def setUp(self):
        fixtures.ObsidianFunctionalTest.setUp(self)
        self.engine = ImportWorkflow(self.settings, Vault(self.settings, self.vault))
    post = fixtures.ObsidianFunctionalTest.post
    get = fixtures.ObsidianFunctionalTest.get
    archive_task = fixtures.ObsidianFunctionalTest.archive_task

    def test_real_markdown_continues_from_archive_to_parse_and_build_queue(self):
        _, flow = self.create()
        self.engine.tick()
        self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            archive = db.execute("SELECT task_id FROM tasks").fetchone()[0]
        self.archive_task(archive)
        self.engine.tick()
        value = self.engine.read(self.instance,flow["workflow_id"])
        self.assertEqual(value["items"][0]["state"],"PARSE_QUEUED")
        parse = value["items"][0]["parse_task_id"]
        self.assertIsNotNone(parse)
        self.archive_task(parse)
        self.engine.tick()
        value = self.engine.read(self.instance,flow["workflow_id"])
        self.assertEqual(value["state"],"INDEX_QUEUED",value)
        self.assertIsNotNone(value["build_task_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            scope = json.loads(db.execute("SELECT payload_json FROM generation_inputs WHERE task_id=?",
                                         (value["build_task_id"],)).fetchone()[0])["publication_scope"]
            self.assertEqual(len(scope["versions"]),1)
            self.assertEqual(scope["versions"][0]["raw_version_id"],value["items"][0]["raw_version_id"])

    def duplicate_parsed_note(self, *, changed_policy=False):
        _, first = self.create()
        self.engine.tick(); self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            archive = db.execute("SELECT task_id FROM tasks").fetchone()[0]
        self.archive_task(archive); self.engine.tick()
        parse = self.engine.read(self.instance,first["workflow_id"])["items"][0]["parse_task_id"]
        self.archive_task(parse)
        with closing(open_db(self.settings.sqlite)) as db:
            before = dict(db.execute("SELECT * FROM documents WHERE kb_id=?", (self.kb,)).fetchone())
            tasks = {r[0] for r in db.execute("SELECT task_id FROM tasks")}
            configs = db.execute("SELECT count(*) FROM processing_configs").fetchone()[0]
        self.engine.action(self.instance,first["workflow_id"],"pause")
        self.engine.action(self.instance,first["workflow_id"],"finish")
        _, second = self.create()
        self.engine.tick(); self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            incoming = {r[0] for r in db.execute("SELECT task_id FROM tasks")} - tasks
        self.assertEqual(len(incoming),1)
        if changed_policy:
            from raghub.parsing_assets import parser_identity
            def changed(settings, lock=None):
                return {**parser_identity(settings,lock),"adapter":"INCOMPATIBLE_TEST_POLICY"}
            with patch("raghub.parsing_assets.parser_identity",side_effect=changed):
                self.archive_task(incoming.pop())
        else:
            self.archive_task(incoming.pop())
        with closing(open_db(self.settings.sqlite)) as db:
            after = dict(db.execute("SELECT * FROM documents WHERE document_id=?", (before["document_id"],)).fetchone())
            config_count = db.execute("SELECT count(*) FROM processing_configs").fetchone()[0]
        self.assertEqual(after["document_id"],before["document_id"])
        self.assertEqual(after["current_raw_version_id"],before["current_raw_version_id"])
        return before,after,second,configs,config_count

    def test_identical_obsidian_reimport_keeps_valid_ready_processing(self):
        before,after,second,configs,config_count = self.duplicate_parsed_note()
        self.assertEqual(after["current_processing_version_id"],before["current_processing_version_id"])
        self.assertEqual(config_count,configs)
        self.engine.tick()
        item = self.engine.read(self.instance,second["workflow_id"])["items"][0]
        self.assertTrue(item["reused"])
        self.assertEqual(item["processing_version_id"],before["current_processing_version_id"])

    def test_parser_policy_change_does_not_reuse_prior_processing(self):
        before,after,_second,_configs,_count = self.duplicate_parsed_note(changed_policy=True)
        self.assertNotEqual(after["current_processing_version_id"],before["current_processing_version_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT status FROM processing_versions WHERE processing_version_id=?",
                                        (after["current_processing_version_id"],)).fetchone()[0],"NEW")


    def plan(self, path="notes/002.md"):
        selected = self.post("/api/ui/obsidian/selection", {"paths":[path],"directories":[]})["entries"]
        preview = self.post("/api/ui/obsidian/preview", {"kb_id":self.kb,"entries":[
            {"relative_path":i["relative_path"],"signature":i["signature"]} for i in selected]})
        body = {"kb_id":self.kb,"preview_id":preview["preview_id"],
                "preview_sha256":preview["preview_sha256"],"confirmed":True}
        return preview, body

    def create(self):
        preview, body = self.plan()
        flow = self.post("/api/ui/obsidian/workflows", body, key=new_id())
        return preview, flow

    def test_query_preview_and_idempotent_confirmation(self):
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE system_state SET mode='QUERY' WHERE singleton=1")
        _, body = self.plan()
        key = new_id()
        first = self.post("/api/ui/obsidian/workflows",body,key=key)
        second = self.post("/api/ui/obsidian/workflows",body,key=key)
        self.assertEqual(first["workflow_id"],second["workflow_id"])
        read = self.get("/api/ui/obsidian/workflows?kb_id="+self.kb+"&request_key="+key)["workflow"]
        self.assertEqual(read["state"],"QUEUED")
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM import_workflows").fetchone()[0],1)
        invalid = self.client.post("/api/ui/obsidian/workflows",json={**body,"kb_id":self.other_kb},
                                  headers={**self.headers,"X-Request-ID":key})
        self.assertEqual(invalid.json()["code"],"IDEMPOTENCY_CONFLICT")

    def test_waits_for_active_question_without_receiving_files(self):
        _, flow = self.create()
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE system_state SET mode='QUERY' WHERE singleton=1")
        with patch("raghub.import_workflow.request_mode",
                   side_effect=BusinessError("QUERY_STILL_ACTIVE","still active")):
            self.engine.tick()
        value = self.engine.read(self.instance, flow["workflow_id"])
        self.assertEqual(value["state"],"WAITING_IMPORT")
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0],0)

    def test_restart_reads_existing_batch_and_task(self):
        _, flow = self.create()
        self.engine.tick()
        self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            batch = db.execute("SELECT batch_id FROM import_batches").fetchone()[0]
            task = db.execute("SELECT task_id FROM tasks").fetchone()[0]
        restarted = ImportWorkflow(self.settings, Vault(self.settings, self.vault))
        restarted.tick()
        restarted.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM import_batches").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT batch_id FROM import_batches").fetchone()[0],batch)
        value = restarted.read(self.instance,flow["workflow_id"])
        self.assertEqual(value["items"][0]["archive_task_id"],task)
        self.assertEqual(value["items"][0]["state"],"ARCHIVE_QUEUED")
        self.assertEqual(value["published_count"],0)

    def test_finish_checks_received_task_even_before_progress_binding(self):
        _, flow = self.create()
        self.engine.tick()
        self.engine.tick()
        with self.assertRaises(BusinessError) as error:
            self.engine.action(self.instance,flow["workflow_id"],"finish")
        self.assertEqual(error.exception.code,"WORKFLOW_TASKS_ACTIVE")
        value=self.get("/api/ui/obsidian/workflows/"+flow["workflow_id"])
        self.assertEqual(value["state"],"PROCESSING")

    def test_progress_removes_source_access_immediately_after_removal(self):
        _, flow = self.create()
        self.engine.tick()
        self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            task=db.execute("SELECT task_id FROM tasks").fetchone()[0]
        doc=self.archive_task(task)
        self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            from raghub.indexing.store import IndexStore
            IndexStore(db,self.settings).remove_document(self.instance,self.kb,doc["document_id"])
        value=self.get("/api/ui/obsidian/workflows/"+flow["workflow_id"])
        self.assertEqual(value["items"],[])
        self.assertEqual((value["total"],value["ready_count"],value["published_count"],value["problem_count"]),(0,0,0,0))

    def test_changed_source_stops_and_is_never_received_twice(self):
        _, flow = self.create()
        self.engine.tick()
        (self.vault/"notes/002.md").write_text("# changed after confirmation")
        self.engine.tick()
        first = self.engine.read(self.instance,flow["workflow_id"])
        self.assertEqual(first["state"],"FAILED")
        self.assertIsNotNone(first["error"])
        self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM import_items").fetchone()[0],1)
        self.assertEqual(self.engine.read(self.instance,flow["workflow_id"])["error"],first["error"])

    def test_pause_and_finish_do_not_delete_preserved_work(self):
        _, flow = self.create()
        self.engine.action(self.instance,flow["workflow_id"],"pause")
        self.engine.tick()
        self.assertEqual(self.engine.read(self.instance,flow["workflow_id"])["state"],"PAUSED")
        self.engine.action(self.instance,flow["workflow_id"],"finish")
        self.assertEqual(self.engine.read(self.instance,flow["workflow_id"])["state"],"CANCELLED")
        self.create()

    def test_wrong_instance_and_unconfirmed_scope_are_rejected(self):
        _, body = self.plan()
        response = self.client.post("/api/ui/obsidian/workflows",json={**body,"confirmed":False},
                                    headers={**self.headers,"X-Request-ID":new_id()})
        self.assertEqual(response.json()["code"],"WORKFLOW_CONFIRMATION_REQUIRED")
        with self.assertRaises(BusinessError) as error:
            self.engine.latest(new_id(),self.kb)
        self.assertEqual(error.exception.code,"DATA_INSTANCE_MISMATCH")

class PublicationScopeTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE documents(document_id TEXT,kb_id TEXT,removed_at TEXT,current_raw_version_id TEXT,current_processing_version_id TEXT);
          CREATE TABLE generation_documents(generation_id TEXT,document_id TEXT,raw_version_id TEXT,processing_version_id TEXT);
          CREATE TABLE processing_versions(processing_version_id TEXT,raw_version_id TEXT,status TEXT);
          CREATE TABLE processing_results(processing_version_id TEXT,bundle_path TEXT,bundle_sha256 TEXT);
        """)
        self.kb,self.base = new_id(),new_id()
        self.a,self.ar,self.ap = self.add_doc()
        self.b,self.br,self.bp = self.add_doc()
        self.c,self.cr,self.cp = self.add_doc()
        self.db.execute("INSERT INTO generation_documents VALUES (?,?,?,?)",(self.base,self.a,self.ar,self.ap))
    def add_doc(self):
        doc,raw,processing = new_id(),new_id(),new_id()
        self.db.execute("INSERT INTO documents VALUES (?,?,NULL,?,?)",(doc,self.kb,raw,processing))
        self.db.execute("INSERT INTO processing_versions VALUES (?,?,'READY')",(processing,raw))
        self.db.execute("INSERT INTO processing_results VALUES (?,?,?)",(processing,"/test/"+processing,"f"*64))
        return doc,raw,processing
    def scope(self):
        return {"base_generation_id":self.base,"versions":[
            {"document_id":self.b,"raw_version_id":self.br,"processing_version_id":self.bp}]}
    def test_only_selected_versions_join_published_baseline(self):
        result=scoped_documents(self.db,self.kb,self.base,self.scope())
        self.assertEqual({r["document_id"] for r in result},{self.a,self.b})
        self.assertNotIn(self.c,{r["document_id"] for r in result})
    def test_changed_or_removed_selected_version_fails_closed(self):
        self.db.execute("UPDATE documents SET current_raw_version_id=? WHERE document_id=?",(new_id(),self.b))
        with self.assertRaises(BusinessError) as error:
            scoped_documents(self.db,self.kb,self.base,self.scope())
        self.assertEqual(error.exception.code,"PUBLICATION_VERSION_CHANGED")
        self.db.execute("UPDATE documents SET current_raw_version_id=?,removed_at='removed' WHERE document_id=?",(self.br,self.b))
        with self.assertRaises(BusinessError):
            scoped_documents(self.db,self.kb,self.base,self.scope())
    def test_partial_publication_keeps_previous_version_of_failed_update(self):
        self.db.execute("UPDATE documents SET current_raw_version_id=?,current_processing_version_id=? WHERE document_id=?",
                        (new_id(),new_id(),self.a))
        result=scoped_documents(self.db,self.kb,self.base,self.scope())
        retained=next(r for r in result if r["document_id"]==self.a)
        self.assertEqual(retained["processing_version_id"],self.ap)
        with self.assertRaises(BusinessError):
            scoped_documents(self.db,self.kb,new_id(),self.scope())
