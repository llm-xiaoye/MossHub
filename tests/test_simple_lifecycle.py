"""Affected deletion and intake continuation regressions; isolated local copies only."""
import json
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import test_obsidian_import as fixtures
from test_import_workflow import WorkflowTest
from raghub.db import new_id,open_db
from raghub.obsidian import Vault
from raghub.import_workflow import ImportWorkflow
from raghub.deletion import DocumentDeletion,redact,remove_internal
from raghub.ingestion.store import BusinessError

class SimpleLifecycleTest(unittest.TestCase):
    post=fixtures.ObsidianFunctionalTest.post
    get=fixtures.ObsidianFunctionalTest.get
    archive_task=fixtures.ObsidianFunctionalTest.archive_task
    plan=WorkflowTest.plan
    create=WorkflowTest.create
    def setUp(self):
        fixtures.ObsidianFunctionalTest.setUp(self)
        self.engine=ImportWorkflow(self.settings,Vault(self.settings,self.vault))

    def archived(self):
        _,flow=self.create()
        self.engine.tick();self.engine.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            task=db.execute("SELECT task_id FROM tasks").fetchone()[0]
        doc=self.archive_task(task)
        self.engine.action(self.instance,flow["workflow_id"],"pause")
        self.engine.action(self.instance,flow["workflow_id"],"finish")
        return task,doc

    def test_received_file_can_continue_without_second_archive_or_default_confirmation(self):
        task,doc=self.archived()
        with closing(open_db(self.settings.sqlite)) as db:
            original=db.execute("SELECT count(*) FROM raw_versions").fetchone()[0]
        key=new_id()
        flow=self.engine.continue_tasks(self.instance,key,self.kb,[task])
        same=self.engine.continue_tasks(self.instance,key,self.kb,[task])
        self.assertEqual(flow["workflow_id"],same["workflow_id"])
        self.engine.tick()
        item=self.engine.read(self.instance,flow["workflow_id"])["items"][0]
        self.assertEqual(item["state"],"PARSE_QUEUED")
        self.archive_task(item["parse_task_id"])
        self.engine.tick()
        final=self.engine.read(self.instance,flow["workflow_id"])
        self.assertEqual(final["state"],"INDEX_QUEUED")
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM raw_versions").fetchone()[0],original)

    def test_delete_clears_internal_archive_and_processing_but_keeps_original_note(self):
        task,doc=self.archived()
        before=(self.vault/"notes/002.md").read_bytes()
        deletion=DocumentDeletion(self.settings)
        first=deletion.create(self.instance,self.kb,doc["document_id"])
        again=deletion.create(self.instance,self.kb,doc["document_id"])
        self.assertEqual(first["document_id"],again["document_id"])
        deletion.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            from raghub.ingestion.store import require_instance
            with self.assertRaises(BusinessError) as held:
                require_instance(db,self.instance)
            self.assertEqual(held.exception.code,"DELETION_CLEANUP_ACTIVE")
        deletion.tick();deletion.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            row=db.execute("SELECT * FROM document_deletions WHERE document_id=?",(doc["document_id"],)).fetchone()
            self.assertEqual(row["state"],"COMPLETE",dict(row))
            removed=db.execute("SELECT * FROM documents WHERE document_id=?",(doc["document_id"],)).fetchone()
            self.assertTrue(removed["removed_at"])
            self.assertEqual(removed["display_name"],"资料已删除")
            self.assertEqual(db.execute("SELECT count(*) FROM file_input_claims WHERE document_id=?",(doc["document_id"],)).fetchone()[0],0)
        for role in ("archives","processing","chunks"):
            self.assertFalse((self.home/"data"/role/self.kb/doc["document_id"]).exists())
        self.assertEqual((self.vault/"notes/002.md").read_bytes(),before)
        with self.assertRaises(BusinessError):
            self.engine.continue_tasks(self.instance,new_id(),self.kb,[task])

    def test_delete_rejects_wrong_instance_and_cross_knowledge_base(self):
        _task,doc=self.archived()
        deletion=DocumentDeletion(self.settings)
        with self.assertRaises(BusinessError):deletion.create(new_id(),self.kb,doc["document_id"])
        with self.assertRaises(BusinessError):deletion.create(self.instance,self.other_kb,doc["document_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertIsNone(db.execute("SELECT removed_at FROM documents WHERE document_id=?",(doc["document_id"],)).fetchone()[0])

    def test_continuation_scope_and_request_key_are_not_rebound(self):
        task,_doc=self.archived()
        with self.assertRaises(BusinessError):
            self.engine.continue_tasks(self.instance,new_id(),self.other_kb,[task])
        key=new_id()
        self.engine.continue_tasks(self.instance,key,self.kb,[task])
        with self.assertRaises(BusinessError):
            self.engine.continue_tasks(self.instance,key,self.kb,[new_id()])


    def test_deleting_one_source_preserves_other_final_answer_reference(self):
        import hashlib
        import sqlite3
        from types import SimpleNamespace
        from raghub.retrieval.support_scope import support_texts
        root=self.home/"source-retention-case"
        folder=root/"data/retrieval/old-request";folder.mkdir(parents=True)
        database=root/"source-retention.sqlite"
        db=sqlite3.connect(database);db.row_factory=sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("CREATE TABLE documents(document_id TEXT,removed_at TEXT,kb_id TEXT);"
            "CREATE TABLE retrieval_requests(request_id TEXT,trace_path TEXT,trace_sha256 TEXT,snapshot_json TEXT,progress_json TEXT);"
            "CREATE TABLE chat_turns(turn_id TEXT,result_json TEXT);")
        keys=("evidence_id","support_unit_id","kb_id","generation_id","document_id","raw_version_id","processing_version_id","source_record_id")
        whitelist={};groups=[];refs=[]
        for did,body in (("gone","删除资料正文"),("kept","保留资料正文")):
            ref=dict(zip(keys,(did+"-e",did+"-u","kb","gen",did,did+"-raw",did+"-processing",did+"-source")))
            unit={**ref,"parent_id":did+"-parent","parent_text_interval":[0,len(body)],
                  "text_sha256":hashlib.sha256(body.encode()).hexdigest(),
                  "sources":[{**{k:ref[k] for k in ("kb_id","generation_id","document_id","raw_version_id","processing_version_id","source_record_id")},
                              "purpose":"BODY"}]}
            whitelist[ref["support_unit_id"]]=unit
            groups.append({"evidence_id":ref["evidence_id"],"document_id":did,
                "raw_version_id":ref["raw_version_id"],"processing_version_id":ref["processing_version_id"],
                "context_text":"","support_units":[{"support_unit_id":ref["support_unit_id"],"text":body}]})
            refs.append(ref)
        trace={"evidence":{"groups":groups,"support_unit_whitelist":whitelist},"candidates":[{"text":"删除资料正文"}]}
        raw=json.dumps(trace,ensure_ascii=False).encode();path=folder/"trace.json";path.write_bytes(raw)
        db.executemany("INSERT INTO documents VALUES (?,?,?)",[("gone","now","kb"),("kept",None,"kb")])
        db.execute("INSERT INTO retrieval_requests VALUES (?,?,?,?,?)",("request",str(path),hashlib.sha256(raw).hexdigest(),json.dumps({"kb_id":"kb","generation_id":"gen","extra":"旧快照"}),"{}"))
        db.execute("INSERT INTO chat_turns VALUES (?,?)",("request",json.dumps(redact({"paragraphs":[{"text":"旧回答保留","references":refs}]},"gone"))))
        db.commit();db.close()
        db=open_db(database)
        engine=DocumentDeletion(SimpleNamespace(home=root))
        engine._preserve_sources(db,{"requests":["request"]})
        row=db.execute("SELECT * FROM retrieval_requests").fetchone()
        kept_raw=Path(row["trace_path"]).read_bytes();kept=json.loads(kept_raw)
        self.assertEqual(hashlib.sha256(kept_raw).hexdigest(),row["trace_sha256"])
        self.assertNotIn("删除资料正文",kept_raw.decode())
        evidence=kept["evidence"]
        self.assertEqual(set(support_texts(evidence["groups"],evidence["support_unit_whitelist"],kb_id="kb",generation_id="gen")),{"kept-u"})
        path.unlink()
        self.assertTrue(Path(row["trace_path"]).is_file())
        from raghub.db import transaction
        with transaction(db):
            db.execute("UPDATE documents SET removed_at='later' WHERE document_id='kept'")
        engine._preserve_sources(db,{"requests":["request"]})
        self.assertIsNone(db.execute("SELECT trace_path FROM retrieval_requests").fetchone()[0])
        db.close()

    def test_old_answer_text_is_preserved_and_deleted_citation_has_no_excerpt(self):
        value={"paragraphs":[{"text":"原回答仍保留","references":[
            {"document_id":"deleted","support_unit_id":"u1","excerpt":"应清除的原文","locators":["private"]},
            {"document_id":"kept","support_unit_id":"u2","excerpt":"保留资料"}]}]}
        got=redact(value,"deleted")
        self.assertEqual(got["paragraphs"][0]["text"],value["paragraphs"][0]["text"])
        self.assertEqual(got["paragraphs"][0]["references"][0],
                         {"document_id":"deleted","support_unit_id":"u1","deleted":True,"label":"资料已删除"})
        self.assertEqual(got["paragraphs"][0]["references"][1],value["paragraphs"][0]["references"][1])

    def test_internal_cleanup_does_not_follow_link_to_original(self):
        data=self.home/"data";data.mkdir(exist_ok=True)
        link=data/"external";link.symlink_to(self.vault,target_is_directory=True)
        with self.assertRaises(ValueError):remove_internal(link,data)
        self.assertTrue((self.vault/"notes/002.md").exists())

    def test_plain_text_preserves_literal_markup_and_exact_line_locations(self):
        from raghub.parsing.formats import convert
        path=self.root/"sample.txt"
        path.write_bytes(bytes([239,187,191])+"# literal title\r\n\r\nCount: 100 million\r\n".encode("utf-8"))
        parsed=convert(path,{}, {})
        self.assertEqual([t.text for t in parsed.document.texts],["# literal title","Count: 100 million"])
        self.assertEqual([v["line_start"] for v in parsed.locations.values()],[1,3])
        self.assertEqual(parsed.quality,[])
        path.write_bytes(b"bad"+bytes([0])+b"text")
        with self.assertRaises(ValueError):convert(path,{}, {})

    def test_deleted_reference_is_readable_in_history_and_sse(self):
        from raghub.chat.router import event_packet
        value={"request_id":"r","conversation_id":"c","data_instance_id":"i","state":"SUCCEEDED","question":"旧问题","error":None,"draft":{},
               "result":{"paragraphs":[{"text":"旧回答","support_unit_ids":["u"],
               "references":[{"document_id":"d","support_unit_id":"u","deleted":True,"label":"资料已删除"}]}]}}
        packet=event_packet(value)
        self.assertEqual(packet["result"]["paragraphs"][0]["text"],"旧回答")
        self.assertEqual(packet["result"]["paragraphs"][0]["references"][0]["label"],"资料已删除")
        self.assertNotIn("excerpt",packet["result"]["paragraphs"][0]["references"][0])


if __name__=="__main__":unittest.main()
