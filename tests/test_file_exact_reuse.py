"""Actual local intake/archiving/parser regressions; no broker or cloud calls."""
from __future__ import annotations
import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
import test_obsidian_import as fixture
from raghub.db import new_id,now,open_db
from raghub.parsing.store import ParsingStore
from raghub import parsing_assets

CONTENT=b"# Reusable file\nThis stable note applies only to version 42.\n"

class FileExactReuseTests(unittest.TestCase):
    setUp=fixture.ObsidianFunctionalTest.setUp
    post=fixture.ObsidianFunctionalTest.post
    get=fixture.ObsidianFunctionalTest.get
    archive_task=fixture.ObsidianFunctionalTest.archive_task

    def upload(self,filename,content=CONTENT,*,kb=None,document_id=None,expected=200):
        metadata={"filename":filename,"sha256":hashlib.sha256(content).hexdigest(),
                  "size_bytes":len(content),"provenance":{"nature":"ORIGINAL"}}
        if document_id:
            metadata["document_id"]=document_id
        batch=self.post("/api/import/batches",{"kb_id":kb or self.kb,"items":[metadata]},key=new_id())
        item=batch["items"][0]
        response=self.client.post("/api/import/batches/"+batch["batch_id"]+"/items/"+item["item_id"]+"/file",
                files={"file":(filename,content,"application/octet-stream")},headers=self.headers)
        self.assertEqual(response.status_code,expected,response.text)
        return response.json(),item["item_id"]

    def counts(self):
        with closing(open_db(self.settings.sqlite)) as db:
            return {table:db.execute("SELECT count(*) FROM "+table).fetchone()[0]
                    for table in ("documents","tasks","outbox","raw_versions","processing_versions","file_input_claims")}

    def test_two_pending_uploads_join_one_task_and_keep_receipt(self):
        first,_first_item=self.upload("first.md")
        second,second_item=self.upload("second.md")
        self.assertEqual(first["task_id"],second["task_id"])
        self.assertEqual(first["document_id"],second["document_id"])
        self.assertTrue(second["duplicate_reused"])
        counts=self.counts()
        self.assertEqual((counts["documents"],counts["tasks"],counts["outbox"]),(1,1,1))
        with closing(open_db(self.settings.sqlite)) as db:
            rows=db.execute("SELECT * FROM source_observations").fetchall()
            self.assertEqual(len(rows),1)
            receipt=json.loads(rows[0]["details_json"])["duplicate_receipt"]
            self.assertEqual((receipt["import_item_id"],receipt["incoming_metadata"]["filename"]),(second_item,"second.md"))
            self.assertIsNone(rows[0]["raw_version_id"])
        adopted=self.archive_task(first["task_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM source_observations WHERE raw_version_id=?",
                (adopted["current_raw_version_id"],)).fetchone()[0],2)

    def test_concurrent_identical_uploads_create_one_identity_and_outbox(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(self.upload,"thread-a.md")
            b=pool.submit(self.upload,"thread-b.md")
            first,second=a.result()[0],b.result()[0]
        self.assertEqual(first["document_id"],second["document_id"])
        self.assertEqual(first["task_id"],second["task_id"])
        counts=self.counts()
        self.assertEqual((counts["documents"],counts["tasks"],counts["outbox"],counts["file_input_claims"]),(1,1,1,1))

    def test_completed_archive_reuses_raw_and_preparation_version(self):
        first,_=self.upload("original.md")
        archived=self.archive_task(first["task_id"])
        second,_=self.upload("copy.md")
        self.assertEqual(second["document_id"],first["document_id"])
        self.assertEqual(second["raw_version_id"],archived["current_raw_version_id"])
        self.assertEqual(second["processing_version_id"],archived["current_processing_version_id"])
        counts=self.counts()
        self.assertEqual((counts["raw_versions"],counts["processing_versions"],counts["tasks"]),(1,1,1))
        self.assertEqual(second["archive_status"],"REUSED")

    def test_completed_real_parse_is_reused_with_its_configuration(self):
        first,_=self.upload("parsed.md")
        self.archive_task(first["task_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            queued=ParsingStore(db,self.settings).queue_parse(self.instance,new_id(),self.kb,
                first["document_id"],{"format":"md"})
        parsed=self.archive_task(queued["task_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            result=ParsingStore(db,self.settings).result(parsed["current_processing_version_id"])
            self.assertEqual(result["status"],"READY")
        second,_=self.upload("parsed-copy.md")
        self.assertEqual(second["task_id"],queued["task_id"])
        self.assertEqual(second["processing_version_id"],parsed["current_processing_version_id"])
        self.assertEqual(second["processing_status"],"READY")
        self.assertEqual(self.counts()["tasks"],2)

    def test_identical_bytes_in_other_kb_remain_separate(self):
        first,_=self.upload("first.md")
        second,_=self.upload("first.md",kb=self.other_kb)
        self.assertNotEqual(first["document_id"],second["document_id"])
        self.assertEqual(self.counts()["file_input_claims"],2)

    def test_different_effective_format_is_not_merged(self):
        first,_=self.upload("first.md")
        second,_=self.upload("first.html")
        self.assertNotEqual(first["document_id"],second["document_id"])

    def test_explicit_update_keeps_requested_document(self):
        first,_=self.upload("first.md")
        other,_=self.upload("other.md",b"# Other file\nOther original.\n")
        self.archive_task(first["task_id"])
        self.archive_task(other["task_id"])
        updated,_=self.upload("explicit.md",document_id=other["document_id"])
        self.assertEqual(updated["document_id"],other["document_id"])
        self.assertNotEqual(updated["document_id"],first["document_id"])
        self.archive_task(updated["task_id"])
        self.assertEqual(self.counts()["documents"],2)

    def test_removed_owner_is_not_reused_or_reactivated(self):
        first,_=self.upload("first.md")
        self.archive_task(first["task_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE documents SET removed_at=? WHERE document_id=?",(now(),first["document_id"]))
        second,_=self.upload("second.md")
        self.assertNotEqual(first["document_id"],second["document_id"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertIsNotNone(db.execute("SELECT removed_at FROM documents WHERE document_id=?",(first["document_id"],)).fetchone()[0])
        self.assertEqual(self.counts()["file_input_claims"],1)

    def test_failed_existing_task_is_reported_without_new_dispatch(self):
        first,_=self.upload("failed.md")
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE tasks SET status='FAILED' WHERE task_id=?",(first["task_id"],))
            db.execute("UPDATE attempts SET state='FAILED' WHERE task_id=?",(first["task_id"],))
            db.execute("UPDATE outbox SET state='CANCELLED' WHERE task_id=?",(first["task_id"],))
            db.execute("UPDATE documents SET active_prepare_task_id=NULL WHERE document_id=?",(first["document_id"],))
        second,_=self.upload("failed-copy.md")
        self.assertEqual(second["task_id"],first["task_id"])
        self.assertEqual(second["associated_task_status"],"FAILED")
        self.assertEqual(self.counts()["tasks"],1)

    def test_changed_task_input_is_rejected_before_reuse(self):
        first,_=self.upload("first.md")
        with closing(open_db(self.settings.sqlite)) as db:
            row=db.execute("SELECT payload_json FROM task_inputs WHERE task_id=?",(first["task_id"],)).fetchone()
            value=json.loads(row[0])
            value["metadata"]["filename"]="tampered.md"
            db.execute("UPDATE task_inputs SET payload_json=? WHERE task_id=?",(json.dumps(value),first["task_id"]))
        rejected,_=self.upload("second.md",expected=409)
        self.assertEqual(rejected["code"],"FILE_REUSE_INPUT_CHANGED")
        self.assertEqual(self.counts()["tasks"],1)

    def test_changed_parser_policy_does_not_reuse_old_processing_claim(self):
        first,_=self.upload("first.md")
        original=parsing_assets.parser_identity
        def changed(*args,**kwargs):
            return {**original(*args,**kwargs),"adapter":"changed-test-policy"}
        with patch.object(parsing_assets,"parser_identity",side_effect=changed):
            second,_=self.upload("second.md")
        self.assertNotEqual(first["document_id"],second["document_id"])

if __name__=="__main__":
    unittest.main()

