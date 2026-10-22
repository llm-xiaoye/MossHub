"""Focused parsing regressions using real components; no broker, model inference or cloud."""
from __future__ import annotations

import copy
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from docling_core.types.doc import DocItemLabel,DoclingDocument
from openpyxl import Workbook
from openpyxl.worksheet.table import Table

from raghub.config import Settings
from raghub.db import IdentityStore,migrate,open_db,system_state,transaction
from raghub.ingestion.store import BusinessError,IngestionStore
from raghub.ingestion.archive import hash_file,json_hash
from raghub.parsing.bundle import apply_reviews,bind_quality,review_plan,revision_diff,set_at
from raghub.parsing.cleanup import clean
from raghub.parsing.contracts import FieldSource,Supplement
from raghub.parsing.formats import bind_native,field_values,native_document,xlsx_document


class ReviewTest(unittest.TestCase):
    def bundle(self):
        document=DoclingDocument(name="review")
        document.add_text(label=DocItemLabel.PARAGRAPH,text="收入 10 亿元")
        document.add_text(label=DocItemLabel.PARAGRAPH,text="支出 8 亿元")
        native=document.model_dump(mode="json")
        fields=[]
        quality=[]
        for field,text,_label in field_values(native):
            cleaned,mappings,_changes=clean(text,plain=False)
            fields.append(FieldSource(field_pointer=field,original_text=text,cleaned_text=cleaned,
                mappings=mappings,locator={"kind":"file","file_sha256":"a"*64,
                "granularity":"COARSE","basis":"UNIT_ORIGINAL","reason":"isolated test text"}))
            quality.append({"severity":"WARN","code":"OCR_NUMERIC_REVIEW","field":field})
        supplement=Supplement(raw_version_id="raw",processing_version_id="base",
            original_identity={"manifest_sha256":"a"*64},adapter="test",config_sha256="b"*64,
            model_lock_sha256="c"*64,scope={},format_details={},fields=fields,
            quality=bind_quality(quality,"raw","base"),images=[])
        return native,supplement

    def test_unchanged_text_can_resolve_only_one_explicit_issue(self):
        native,supplement=self.bundle()
        self.assertEqual(revision_diff(native,copy.deepcopy(native)),[])
        reviews=[{"issue_id":supplement.quality[0]["issue_id"],"outcome":"VERIFIED_CORRECT","note":"用户核对第一条原件数字正确"}]
        plan=review_plan(native,native,supplement,"d"*64,reviews)
        quality=apply_reviews(supplement.quality,plan,native,{"source":"UI","decision_id":"human"})
        self.assertIn("resolution",quality[0])
        self.assertNotIn("resolution",quality[1])
        self.assertEqual(quality[0]["resolution"]["binding"]["accepted_value_sha256"],
                         json_hash(supplement.fields[0].cleaned_text))

    def test_change_needs_selected_issue_and_later_change_invalidates_review(self):
        native,supplement=self.bundle()
        revised=copy.deepcopy(native)
        set_at(revised,supplement.fields[0].field_pointer,"收入 11 亿元")
        unchanged_quality=apply_reviews(supplement.quality,[],revised,{})
        self.assertTrue(all("resolution" not in issue for issue in unchanged_quality))
        review={"issue_id":supplement.quality[0]["issue_id"],"outcome":"CORRECTED","note":"用户按第一条原件修正"}
        plan=review_plan(native,revised,supplement,"d"*64,[review])
        quality=apply_reviews(supplement.quality,plan,revised,{"source":"UI","decision_id":"human"})
        self.assertNotIn("resolution",quality[1])
        changed_again=copy.deepcopy(revised)
        set_at(changed_again,supplement.fields[0].field_pointer,"收入 12 亿元")
        invalidated=apply_reviews(quality,[],changed_again,{})
        self.assertNotIn("resolution",invalidated[0])
        self.assertEqual(invalidated[0]["review_history"][-1]["invalidated"],"REVIEWED_FIELD_CHANGED")
        with self.assertRaises(ValueError):
            review_plan(native,revised,supplement,"d"*64,[{**review,"outcome":"VERIFIED_CORRECT"}])

    def test_resource_exemption_binds_resource_and_preserves_ocr_warning(self):
        native,supplement=self.bundle()
        resource="e"*64
        locator={"kind":"md","file_sha256":"a"*64,"granularity":"REGION","basis":"BOUND_IMAGE",
                 "line_start":1,"line_end":1,"resource_sha256":resource}
        supplement.images=[{"resource_sha256":resource,"page":1}]
        supplement.quality=bind_quality([*supplement.quality,{"severity":"WARN",
            "code":"BITMAP_STRUCTURE_REVIEW_REQUIRED","resource_sha256":resource,"locator":locator}],"raw","base")
        review={"issue_id":supplement.quality[-1]["issue_id"],"outcome":"RESOURCE_EXEMPT","note":"用户核对该图只是装饰"}
        plan=review_plan(native,native,supplement,"d"*64,[review])
        quality=apply_reviews(supplement.quality,plan,native,{"source":"UI","decision_id":"human"})
        self.assertTrue(all("resolution" not in issue for issue in quality[:-1]))
        self.assertEqual(quality[-1]["resolution"]["binding"]["resource_sha256"],resource)


class FormatTest(unittest.TestCase):
    def test_explicit_xlsx_selection_context_and_hidden_formula_scan(self):
        with tempfile.TemporaryDirectory(prefix="raghub-stage3-xlsx-") as root:
            path=Path(root)/"selection.xlsx"
            book=Workbook()
            chosen=book.active
            chosen.title="选择"
            chosen.append(["季度报告"])
            chosen.append(["项目","数值"])
            chosen.append(["收入",12.5])
            chosen.add_table(Table(displayName="Chosen",ref="A2:B3"))
            other=book.create_sheet("未选择")
            other.append(["项目","数值"])
            other.append(["不应入正文",999])
            other.add_table(Table(displayName="Other",ref="A1:B2"))
            hidden=book.create_sheet("隐藏公式")
            hidden.sheet_state="hidden"
            hidden["A1"]="=1+1"
            book.save(path)
            parsed=xlsx_document(path,{"regions":[{"sheet":"选择","range":"A2:B3",
                                                     "header_rows":1,"title":["A1:A1"]}]})
            self.assertEqual(len(parsed.document.tables),1)
            body="\n".join(text for _field,text,_label in field_values(parsed.document.model_dump(mode="json")))
            self.assertIn("季度报告",body)
            self.assertNotIn("不应入正文",body)
            title=[locator for field,locator in parsed.locations.items() if locator.get("cell_range")=="A1"]
            self.assertEqual(len(title),1)
            self.assertEqual(title[0]["sheet"],"选择")
            self.assertTrue(any(formula["sheet"]=="隐藏公式" for formula in parsed.details["formulas"]))
            self.assertFalse(parsed.details["number_cache_is_trusted"])

    def test_inline_code_protection_comes_from_original_structure(self):
        with tempfile.TemporaryDirectory(prefix="raghub-stage3-md-") as root:
            path=Path(root)/"inline.md"
            path.write_text("请保留 \u0060a  b\u0060 的双空格。\n",encoding="utf-8")
            parsed=bind_native(native_document(path,"md"),path,"md",hash_file(path)[0])
            native=parsed.document.model_dump(mode="json")
            fields=list(field_values(native))
            selected=[(field,text) for field,text,_label in fields if "a  b" in text]
            self.assertTrue(selected)
            for field,text in selected:
                self.assertIn(field,parsed.details["protected_fields"])
                cleaned,_mappings,_changes=clean(text,plain=field not in parsed.details["protected_fields"])
                self.assertIn("a  b",cleaned)


class CompletionFenceTest(unittest.TestCase):
    def test_cancelled_owner_can_end_and_stale_owner_cannot_write(self):
        with tempfile.TemporaryDirectory(prefix="raghub-stage3-owner-") as root:
            home=Path(root)
            project=Path(__file__).resolve().parents[1]
            settings=replace(Settings.load(),home=home,project=project,sqlite=home/"state.sqlite3")
            with closing(open_db(settings.sqlite, create=True)) as db:
                migrate(db,project/"backend/migrations")
                instance=system_state(db)["data_instance_id"]
                identities=IdentityStore(db)
                task,attempt,outbox=identities.create_task(kind="OCR_SUGGESTION",request_key="owner",
                    request_sha256="a"*64,budget_scope_id="owner-scope")
                identities.reserve_publish(outbox)
                identities.publish_outcome(outbox,confirmed=True)
                epoch=identities.claim_attempt(data_instance_id=instance,task_id=task,
                    attempt_id=attempt,worker_id="ocr-worker")
                self.assertIsNotNone(epoch)
                identity=(instance,task,attempt,epoch,"ocr-worker")
                store=IngestionStore(db,settings)
                stale=(instance,task,attempt,epoch+1,"ocr-worker")
                with self.assertRaises(BusinessError):
                    with transaction(db):
                        store.completion_owner(stale,required=True)
                        db.execute("UPDATE tasks SET status='FAILED' WHERE task_id=?",(task,))
                self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?",(task,)).fetchone()[0],"RUNNING")
                with transaction(db):
                    db.execute("UPDATE tasks SET cancel_requested=1 WHERE task_id=?",(task,))
                    db.execute("UPDATE system_state SET mode='DRAINING' WHERE singleton=1")
                    self.assertEqual(store.completion_owner(identity,required=True)["task_id"],task)
                self.assertTrue(store.finish(identity,cancelled=True))
                self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?",(task,)).fetchone()[0],"CANCELLED")
                self.assertFalse(store.finish(stale))
                self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?",(task,)).fetchone()[0],"CANCELLED")


if __name__=="__main__":
    unittest.main()
