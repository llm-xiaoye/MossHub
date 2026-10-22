"""Real SQLite contention and deletion lifecycle regressions; no network or model calls."""
import json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_simple_lifecycle as lifecycle
import test_stage2_local_fixes as dispatch_tests
from raghub.db import IdentityStore,new_id,open_db
from raghub.deletion import DocumentDeletion,redact

class DeletionCoordinationTest(unittest.TestCase):
    setUp=lifecycle.SimpleLifecycleTest.setUp
    post=lifecycle.SimpleLifecycleTest.post
    get=lifecycle.SimpleLifecycleTest.get
    archive_task=lifecycle.SimpleLifecycleTest.archive_task
    plan=lifecycle.SimpleLifecycleTest.plan
    create=lifecycle.SimpleLifecycleTest.create
    archived=lifecycle.SimpleLifecycleTest.archived

    def test_create_releases_writer_before_parsing_old_answers(self):
        _,doc=self.archived();deletion=DocumentDeletion(self.settings)
        original=deletion._redact_answers;observed=[]
        def outside_writer(db,did):
            with closing(sqlite3.connect(str(self.settings.sqlite),timeout=0)) as concurrent:
                concurrent.execute("UPDATE system_state SET mode=mode WHERE singleton=1");concurrent.commit()
            observed.append(did);return original(db,did)
        with patch.object(deletion,"_redact_answers",side_effect=outside_writer):
            deletion.create(self.instance,self.kb,doc["document_id"])
        self.assertEqual(observed,[doc["document_id"]])

    def test_pending_and_running_replacement_keep_original_task_and_archive(self):
        _,doc=self.archived();deletion=DocumentDeletion(self.settings)
        deletion.create(self.instance,self.kb,doc["document_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            key=new_id();task,attempt,outbox=IdentityStore(db).create_task(kind="DOCUMENT",request_key=key,request_sha256="a"*64,budget_scope_id=key)
            row=db.execute("SELECT * FROM document_deletions WHERE document_id=?",(doc["document_id"],)).fetchone()
            data=json.loads(row["data_json"]);data["build_task_id"]=task
            deletion._save(db,doc["document_id"],data,"REBUILDING")
        for status in ("PENDING","RUNNING"):
            with closing(open_db(self.settings.sqlite)) as db:db.execute("UPDATE tasks SET status=? WHERE task_id=?",(status,task))
            deletion.tick()
            with closing(open_db(self.settings.sqlite)) as db:
                row=db.execute("SELECT state,data_json,error_text FROM document_deletions WHERE document_id=?",(doc["document_id"],)).fetchone()
                self.assertEqual(row["state"],"REBUILDING");self.assertIsNone(row["error_text"])
                self.assertEqual(json.loads(row["data_json"])["build_task_id"],task)
                self.assertEqual(db.execute("SELECT publish_count FROM outbox WHERE outbox_id=?",(outbox,)).fetchone()[0],0)
            self.assertTrue((self.home/"data/archives"/self.kb/doc["document_id"]).exists())
        with closing(open_db(self.settings.sqlite)) as db:db.execute("UPDATE tasks SET status='FAILED' WHERE task_id=?",(task,))
        deletion.tick()
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT state FROM document_deletions WHERE document_id=?",(doc["document_id"],)).fetchone()[0],"FAILED")
        self.assertTrue((self.home/"data/archives"/self.kb/doc["document_id"]).exists())

    def test_deleted_workflow_items_are_absent_from_rows_and_all_counts(self):
        _,doc=self.archived()
        with closing(open_db(self.settings.sqlite)) as db:
            row=db.execute("SELECT * FROM import_workflows WHERE kb_id=?",(self.kb,)).fetchone();data=json.loads(row["data_json"])
            data["items"][0].update(document_id=doc["document_id"],state="PUBLISHED")
            db.execute("UPDATE documents SET removed_at='deleted' WHERE document_id=?",(doc["document_id"],))
            value=self.engine._present(row,data,db=db)
        self.assertEqual((value["total"],value["ready_count"],value["published_count"],value["problem_count"],value["items"]),(0,0,0,0,[]))

class AnswerRedactionTest(unittest.TestCase):
    def test_concurrent_result_is_preserved_and_both_tables_get_deleted_stub(self):
        with tempfile.TemporaryDirectory(prefix="rag-redact-",dir="/tmp") as root:
            path=Path(root)/"state.sqlite3";did=new_id()
            with closing(open_db(path,create=True)):pass
            value={"paragraphs":[{"text":"旧回答保留","references":[{"document_id":did,"excerpt":"必须删除的摘录"},{"document_id":"other","excerpt":"另一资料保留"}]}]}
            with closing(sqlite3.connect(path)) as db:
                for table,key in (("chat_turns","turn_id"),("chat_requests","request_id")):
                    db.execute(f"CREATE TABLE {table} ({key} TEXT PRIMARY KEY,result_json TEXT)")
                    db.execute(f"INSERT INTO {table} VALUES (?,?)",("answer",json.dumps(value)))
                db.commit()
            deletion=DocumentDeletion(SimpleNamespace(sqlite=path));concurrent_done=[]
            def change_once(v,document_id):
                if not concurrent_done:
                    concurrent_done.append(True)
                    changed=json.loads(json.dumps(value));changed["paragraphs"][0]["text"]="并发完成的回答也保留"
                    with closing(sqlite3.connect(path,timeout=0)) as writer:
                        writer.execute("UPDATE chat_turns SET result_json=?",(json.dumps(changed),));writer.commit()
                return redact(v,document_id)
            with closing(open_db(path)) as db,patch("raghub.deletion.redact",side_effect=change_once):deletion._redact_answers(db,did)
            with closing(sqlite3.connect(path)) as db:
                for table in ("chat_turns","chat_requests"):
                    result=json.loads(db.execute(f"SELECT result_json FROM {table}").fetchone()[0]);refs=result["paragraphs"][0]["references"]
                    self.assertEqual(refs[0],{"document_id":did,"deleted":True,"label":"资料已删除"})
                    self.assertEqual(refs[1]["excerpt"],"另一资料保留")
                    self.assertEqual(result["paragraphs"][0]["text"],"并发完成的回答也保留" if table=="chat_turns" else "旧回答保留")

class DispatcherContentionTest(unittest.TestCase):
    setUp=dispatch_tests.DispatchBudgetTest.setUp
    task=dispatch_tests.DispatchBudgetTest.task
    def test_real_busy_claim_has_zero_sends_then_one_reservation_after_unlock(self):
        task,_,outbox=self.task("locked-claim");real_open=open_db
        def quick_open(*args,**kwargs):
            db=real_open(*args,**kwargs);db.execute("PRAGMA busy_timeout=0");return db
        with closing(sqlite3.connect(self.settings.sqlite,timeout=0)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch("raghub.dispatcher.open_db",side_effect=quick_open),patch.object(self.dispatcher,"publish") as publish:
                self.assertFalse(self.dispatcher.poll_once());publish.assert_not_called()
            writer.rollback()
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(tuple(db.execute("SELECT state,publish_count FROM outbox WHERE outbox_id=?",(outbox,)).fetchone()),("PENDING",0))
        with patch.object(self.dispatcher,"worker",return_value={"worker_id":"idle-worker"}),patch.object(self.dispatcher,"publish") as publish,patch("raghub.mode.maybe_finish_mode"):
            self.assertTrue(self.dispatcher.poll_once());publish.assert_called_once();self.assertEqual(publish.call_args.args[0]["task_id"],task)
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(tuple(db.execute("SELECT state,publish_count FROM outbox WHERE outbox_id=?",(outbox,)).fetchone()),("PUBLISHING",1))
    def test_send_or_finalization_error_is_not_caught_as_claim_retry(self):
        with patch.object(self.dispatcher,"tick",return_value=[{"reserved":True}]),patch.object(self.dispatcher,"publish",side_effect=sqlite3.OperationalError("after producer")) as publish:
            with self.assertRaises(sqlite3.OperationalError):self.dispatcher.poll_once()
            publish.assert_called_once()

if __name__=="__main__":unittest.main()
