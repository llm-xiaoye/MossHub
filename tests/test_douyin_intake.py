"""DY source scope, idempotency and exact material binding; no real tool/provider."""
import copy
import json
import tempfile
import unittest
import sys
import hashlib
from types import SimpleNamespace
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock,patch

import test_obsidian_import as fixtures
from raghub.db import new_id, now, open_db
from raghub.douyin_intake import create_router, share_url, object_json, tool_snapshot
from raghub.douyin_worker import _command, _note_analysis, _saved_note, prepare_claimed
from raghub.ingestion.store import BusinessError
from raghub.obsidian import Vault

URL="https://v.douyin.com/bs0m4Agk9fM/"


class VideoScopeTest(unittest.TestCase):
    def test_only_the_submitted_official_single_video_is_accepted(self):
        self.assertEqual(share_url("复制打开抖音 "+URL+" :3pm"),URL)
        bad=[URL+" https://v.douyin.com/another/", "https://example.org/video/123",
             "https://www.douyin.com/user/123", "https://www.douyin.com/note/not-a-work",
             "https://v.douyin.com:bad/test/", "收藏夹：学习"]
        for text in bad:
            with self.subTest(text=text),self.assertRaises(BusinessError):
                share_url(text)

    def test_duplicate_json_fields_cannot_override_the_source_contract(self):
        with self.assertRaises(ValueError):
            object_json('{"task_id":"a","task_id":"b"}')


class ToolReadinessTest(unittest.TestCase):
    def config(self,root):
        project=root/"project";(project/"configs").mkdir(parents=True)
        vault=root/"vault";(vault/"知识库").mkdir(parents=True)
        cache=root/"cache";cache.mkdir()
        script=root/"dy.py";script.write_text("# fixture only\n",encoding="utf-8")
        config=root/"tool.json"
        config.write_text(json.dumps({"vault":str(vault),"notes_dir":"知识库","cache_dir":str(cache),"auto_browser_recovery":False,"prefer_browser_media":False,"unified_only":True,"unified_root_vault":str(vault)}),encoding="utf-8")
        (project/"configs/douyin.json").write_text(json.dumps({"schema_version":1,"python":sys.executable,"script":str(script),"config":str(config)}),encoding="utf-8")
        return SimpleNamespace(project=project),vault,config

    def test_missing_overviews_are_rejected_before_task_or_model_work(self):
        with tempfile.TemporaryDirectory(prefix="rag-dy-ready-") as tmp:
            settings,_,_=self.config(Path(tmp))
            with self.assertRaises(BusinessError) as raised:
                tool_snapshot(settings)
            self.assertEqual(raised.exception.code,"DY_SAVE_NOT_READY")

    def test_regular_overviews_are_required_and_other_vault_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="rag-dy-ready-") as tmp:
            root=Path(tmp);settings,vault,config=self.config(root)
            index=vault/"知识总索引.md";log=vault/"导入总日志.md"
            index.write_text("# 知识总索引\n",encoding="utf-8");log.write_text("# 导入总日志\n",encoding="utf-8")
            self.assertEqual(tool_snapshot(settings)["vault"],str(vault.resolve()))
            log.unlink();log.symlink_to(index)
            with self.assertRaises(BusinessError) as raised:
                tool_snapshot(settings)
            self.assertEqual(raised.exception.code,"DY_SAVE_NOT_READY")
            value=json.loads(config.read_bytes());value["unified_root_vault"]=str(root)
            config.write_text(json.dumps(value),encoding="utf-8")
            with self.assertRaises(BusinessError) as raised:
                tool_snapshot(settings)
            self.assertEqual(raised.exception.code,"DY_TOOL_UNAVAILABLE")



    def test_import_never_enables_browser_recovery(self):
        with tempfile.TemporaryDirectory(prefix="rag-dy-browser-") as tmp:
            settings, vault, config = self.config(Path(tmp))
            for name in ("知识总索引.md", "导入总日志.md"):
                (vault / name).write_text("# fixture\n", encoding="utf-8")
            original = json.loads(config.read_bytes())
            for key in ("auto_browser_recovery", "prefer_browser_media"):
                for enabled in (True, None):
                    value = dict(original)
                    if enabled is None:
                        value.pop(key)
                    else:
                        value[key] = enabled
                    config.write_text(json.dumps(value), encoding="utf-8")
                    with self.subTest(key=key, enabled=enabled), self.assertRaises(BusinessError) as raised:
                        tool_snapshot(settings)
                    self.assertEqual(raised.exception.code, "DY_TOOL_UNAVAILABLE")

class VideoRequestTest(unittest.TestCase):
    def setUp(self):
        fixtures.ObsidianFunctionalTest.setUp(self)
        self.client.app.include_router(create_router(self.settings,Vault(self.settings,self.vault)),prefix="/api/ui")
        self.tool_patch=patch("raghub.douyin_intake.tool_snapshot",return_value={
            "python":"/fixed/test/python","script":"/fixed/test/dy.py","config":"/fixed/test/config.json",
            "script_sha256":"a"*64,"config_sha256":"b"*64,
            "vault":str(self.vault),"notes_root":str(self.vault),"cache_dir":str(self.root)})
        self.tool_patch.start();self.addCleanup(self.tool_patch.stop)
    post=fixtures.ObsidianFunctionalTest.post
    get=fixtures.ObsidianFunctionalTest.get

    def body(self,url=URL):
        return {"kb_id":self.kb,"share_text":url}

    def test_same_request_reuses_task_and_never_creates_rag_document(self):
        key=new_id()
        first=self.post("/api/ui/douyin/jobs",self.body(),key=key)
        second=self.post("/api/ui/douyin/jobs",self.body(),key=key)
        self.assertEqual(first["task_id"],second["task_id"])
        self.assertEqual(first["kb_id"],self.kb)
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM documents").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM provider_attempts").fetchone()[0],0)
        conflict=self.client.post("/api/ui/douyin/jobs",json=self.body("https://v.douyin.com/another/"),
                                  headers={**self.headers,"X-Request-ID":key})
        self.assertEqual(conflict.status_code,409)

    def test_status_is_bound_to_current_kb_and_instance(self):
        value=self.post("/api/ui/douyin/jobs",self.body(),key=new_id())
        path="/api/ui/douyin/jobs/"+value["task_id"]+"?kb_id="
        self.assertEqual(self.get(path+self.kb)["task_id"],value["task_id"])
        self.assertEqual(self.client.get(path+self.other_kb,headers=self.headers).status_code,404)
        wrong=self.client.get(path+self.kb,headers={"X-RAG-Data-Instance":new_id()})
        self.assertNotEqual(wrong.status_code,200)

    def test_unknown_result_blocks_a_new_request_even_without_provider_record(self):
        value=self.post("/api/ui/douyin/jobs",self.body(),key=new_id())
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE tasks SET status='FAILED',last_error_json=? WHERE task_id=?",
                (json.dumps({"code":"DY_RESULT_UNKNOWN","message":"结果待核对"}),value["task_id"]))
            db.execute("UPDATE attempts SET state='FAILED',finished_at=? WHERE task_id=?",(now(),value["task_id"]))
        again=self.client.post("/api/ui/douyin/jobs",json=self.body(),
                               headers={**self.headers,"X-Request-ID":new_id()})
        self.assertEqual(again.status_code,409)
        self.assertEqual(again.json()["code"],"DY_PRIOR_TASK_PENDING")
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0],1)


    def test_manual_recovery_with_previous_run_does_not_start_another_cli_run(self):
        value=self.post("/api/ui/douyin/jobs",self.body(),key=new_id())
        with closing(open_db(self.settings.sqlite)) as db:
            db.execute("UPDATE attempts SET directory_path=? WHERE task_id=?",(str(self.root/"existing-run"),value["task_id"]))
            with patch("raghub.douyin_worker._owner"),patch("raghub.douyin_worker._command") as command:
                with self.assertRaises(BusinessError) as raised:
                    prepare_claimed(db,self.settings,("instance",value["task_id"],new_id()),{})
                self.assertEqual(raised.exception.code,"DY_RESULT_UNKNOWN")
                command.assert_not_called()


    def saved_original_run(self):
        script=self.root/"retained-dy.py";config=self.root/"retained-tool.json"
        script.write_text("# readonly reconciliation fixture\n",encoding="utf-8")
        config.write_text("{}\n",encoding="utf-8")
        with patch("raghub.douyin_intake.tool_snapshot",return_value={
            "python":"/fixed/test/python","script":str(script),"config":str(config),
            "script_sha256":hashlib.sha256(script.read_bytes()).hexdigest(),"config_sha256":hashlib.sha256(config.read_bytes()).hexdigest(),
            "vault":str(self.vault),"notes_root":str(self.vault),"cache_dir":str(self.root)}):
            value=self.post("/api/ui/douyin/jobs",self.body(),key=new_id())
        video_id="7679012030732340530"
        note=self.vault/"dy-sample.md"
        note.write_text("---\ntitle: 样例\naweme_id: '"+video_id+"'\nsource_url: https://www.douyin.com/video/"+video_id+"\n---\n# 样例\n正文\n",encoding="utf-8")
        (self.vault/"知识总索引.md").write_text("[[dy-sample]]",encoding="utf-8")
        (self.vault/"导入总日志.md").write_text(video_id,encoding="utf-8")
        run=self.root/"dy-runs/saved-run";run.mkdir(parents=True)
        original_input=URL.encode();(run/"input.txt").write_bytes(original_input)
        state={"mode":"videos","input_sha256":hashlib.sha256(original_input).hexdigest(),
            "items":[{"id":video_id,"source_url":URL,"status":"saved","note":str(note)}]}
        (run/"state.json").write_text(json.dumps(state),encoding="utf-8")
        with closing(open_db(self.settings.sqlite)) as db:
            attempt=db.execute("SELECT attempt_id FROM attempts WHERE task_id=?",(value["task_id"],)).fetchone()[0]
            payload=json.loads(db.execute("SELECT payload_json FROM generation_inputs WHERE task_id=?",(value["task_id"],)).fetchone()[0])
            directory=self.settings.home/"data/douyin"/value["task_id"]/attempt
            (directory/"00-init").mkdir(parents=True)
            (directory/"03-commit").mkdir()
            (directory/"03-commit/exit.json").write_text(json.dumps({"exit_code":1,"child_stream_split_observed":True,"outer_stream_split_observed":False}),encoding="utf-8")
            (directory/"input.json").write_text(json.dumps(payload),encoding="utf-8")
            argv=[payload["tool"]["python"],"-B",str(script),"--config",str(config),"init"]
            (directory/"00-init/request.json").write_text(json.dumps({"argv":argv,"application_attempts":1}),encoding="utf-8")
            (directory/"00-init/child-stdout.bin").write_text(json.dumps({"run_dir":str(run),"input_file":str(run/"input.txt"),"state_file":str(run/"state.json")}),encoding="utf-8")
            db.execute("UPDATE tasks SET status='FAILED',stage='DY_SAVING',last_error_json=? WHERE task_id=?",
                (json.dumps({"code":"DY_RESULT_UNKNOWN","message":"先核对原状态"}),value["task_id"]))
            db.execute("UPDATE attempts SET state='FAILED',directory_path=?,finished_at=? WHERE attempt_id=?",(str(directory),now(),attempt))
        return value,run,state

    def test_confirmed_original_saved_run_is_read_without_replaying_or_rewriting_history(self):
        value,_,_=self.saved_original_run()
        result=self.get("/api/ui/douyin/jobs/"+value["task_id"]+"?kb_id="+self.kb)
        self.assertTrue(result["saved"]);self.assertFalse(result["result_unknown"])
        self.assertTrue(result["reconciled_from_original_run"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id=?",(value["task_id"],)).fetchone()[0],"FAILED")
            self.assertEqual(db.execute("SELECT count(*) FROM provider_attempts").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM stage_artifacts WHERE stage='DY_NOTE_RESULT'").fetchone()[0],0)

        again=self.post("/api/ui/douyin/jobs",self.body(),key=new_id())
        self.assertNotEqual(again["task_id"],value["task_id"])

    def test_note_from_a_different_input_does_not_clear_unknown_result(self):
        value,run,state=self.saved_original_run()
        state["items"][0]["source_url"]="https://v.douyin.com/another/"
        (run/"state.json").write_text(json.dumps(state),encoding="utf-8")
        result=self.get("/api/ui/douyin/jobs/"+value["task_id"]+"?kb_id="+self.kb)
        self.assertFalse(result["saved"]);self.assertTrue(result["result_unknown"])


    def test_saved_note_without_confirmed_original_commit_exit_stays_unknown(self):
        value,_,_=self.saved_original_run()
        with closing(open_db(self.settings.sqlite)) as db:
            path=db.execute("SELECT directory_path FROM attempts WHERE task_id=?",(value["task_id"],)).fetchone()[0]
        (Path(path)/"03-commit/exit.json").unlink()
        result=self.get("/api/ui/douyin/jobs/"+value["task_id"]+"?kb_id="+self.kb)
        self.assertFalse(result["saved"]);self.assertTrue(result["result_unknown"])



    def failed_material_run(self):
        value, run, state = self.saved_original_run()
        with closing(open_db(self.settings.sqlite)) as db:
            directory = Path(db.execute("SELECT directory_path FROM attempts WHERE task_id=?", (value["task_id"],)).fetchone()[0])
            payload = json.loads((directory / "input.json").read_bytes())
            error = {"code": "ACQUISITION_FAILED", "error_type": "TypeError",
                     "message": "BusinessError.__init__() got an unexpected keyword argument 'details'"}
            db.execute("UPDATE tasks SET stage='DY_PREPARING',last_error_json=? WHERE task_id=?", (json.dumps(error), value["task_id"]))
        folder = directory / "02-prepare-all";folder.mkdir()
        tool = payload["tool"]
        (folder / "request.json").write_text(json.dumps({"argv": [tool["python"], "-B", tool["script"], "--config", tool["config"], "prepare-all", "--run", str(run)]}), encoding="utf-8")
        (folder / "exit.json").write_text('{"exit_code":0}', encoding="utf-8")
        prepared = {"run_dir": str(run), "ready": [], "failures": [{
            "id": state["items"][0]["id"], "source_url": URL,
            "url": "https://www.douyin.com/video/" + state["items"][0]["id"],
            "stage": "extract", "reason": "outgoing traffic has been disabled; HF_HUB_OFFLINE=1"}]}
        (folder / "child-stdout.bin").write_text(json.dumps(prepared), encoding="utf-8")
        return value, directory, prepared, error

    def test_old_error_shows_original_cause_without_rewriting_history_or_replaying(self):
        value, directory, prepared, error = self.failed_material_run()
        path = "/api/ui/douyin/jobs/" + value["task_id"] + "?kb_id=" + self.kb
        with patch("raghub.douyin_worker._command") as command:
            result = self.get(path)
        command.assert_not_called()
        self.assertIn("语音模型", result["message"])
        self.assertIn("离线模式", result["message"])
        self.assertNotIn("TypeError", result["message"])
        self.assertFalse(result["saved"])
        with closing(open_db(self.settings.sqlite)) as db:
            self.assertEqual(json.loads(db.execute("SELECT last_error_json FROM tasks WHERE task_id=?", (value["task_id"],)).fetchone()[0]), error)
        prepared["failures"][0]["source_url"] = "https://v.douyin.com/another/"
        (directory / "02-prepare-all/child-stdout.bin").write_text(json.dumps(prepared), encoding="utf-8")
        self.assertNotIn("离线模式", self.get(path)["message"])

    def test_prepare_failure_preserves_cause_and_does_not_call_note_model(self):
        value = self.post("/api/ui/douyin/jobs", self.body(), key=new_id())
        run = self.root / "dy-runs/prepare-failed";run.mkdir(parents=True)
        video_id = "7679012030732340530"
        (run / "state.json").write_text(json.dumps({"mode": "videos", "items": [{"id": video_id, "status": "failed"}]}), encoding="utf-8")
        prepared = {"ready": [], "model_analysis_needed": False,
                    "failures": [{"id": video_id, "stage": "extract", "reason": "outgoing traffic has been disabled"}]}
        initialized = {"run_dir": str(run), "input_file": str(run / "input.txt"), "state_file": str(run / "state.json")}
        with closing(open_db(self.settings.sqlite)) as db:
            attempt = db.execute("SELECT attempt_id FROM attempts WHERE task_id=?", (value["task_id"],)).fetchone()[0]
            payload = json.loads(db.execute("SELECT payload_json FROM generation_inputs WHERE task_id=?", (value["task_id"],)).fetchone()[0])
            with patch("raghub.douyin_worker._owner"), patch("raghub.douyin_worker.IngestionStore"), patch("raghub.douyin_worker._command", side_effect=[initialized, {}, prepared]), patch("raghub.douyin_worker._cloud") as cloud:
                with self.assertRaises(BusinessError) as raised:
                    prepare_claimed(db, self.settings, (self.instance, value["task_id"], attempt), payload)
                cloud.assert_not_called()
        self.assertEqual(raised.exception.code, "DY_MATERIAL_UNAVAILABLE")
        self.assertIn("语音模型", str(raised.exception))
        self.assertFalse(raised.exception.details["note_generation_started"])

class CommandOutcomeTest(unittest.TestCase):
    def command(self,command,exit_code):
        with tempfile.TemporaryDirectory(prefix="rag-dy-outcome-") as tmp:
            child=MagicMock(returncode=exit_code)
            child.poll.return_value=exit_code
            tool={"python":"/fixed/python","script":"/fixed/dy.py","config":"/fixed/config.json"}
            with patch("raghub.douyin_worker._owner"),patch("raghub.douyin_worker.tool_snapshot",return_value=tool),patch("raghub.douyin_worker.subprocess.Popen",return_value=child):
                with self.assertRaises(BusinessError) as raised:
                    _command(None,None,("instance","task","attempt"),Path(tmp),tool,0,command)
            return raised.exception.code

    def test_failed_commit_remains_unknown_but_failed_prepare_is_known_stopped(self):
        self.assertEqual(self.command("commit",1),"DY_RESULT_UNKNOWN")
        self.assertEqual(self.command("prepare-all",1),"DY_TOOL_STOPPED")

    def test_unreadable_commit_receipt_remains_unknown(self):
        self.assertEqual(self.command("commit",0),"DY_RESULT_UNKNOWN")


class NoteEvidenceTest(unittest.TestCase):
    def value(self):
        return {"knowledge":"> 结论。\n\n## 核心要点\n- 一\n- 二\n- 三\n\n## 工作机制\n说明。\n\n## 模型补充\n### 补充知识\n解释。\n### 纠错与边界\n边界。\n### 模型判断\n判断。\n\n## 自测\n问题及答案。",
                "analysis":{"title":"实例","category":["AI","Agent"],"tags":["A","B","C"],"type":"知识笔记","insight":"说明",
                    "learning_review":{"learning_goal":"理解","key_claims":[
                        {"claim":"材料主张","evidence":"工具需要明确输入","section":"工作机制"},
                        {"claim":"边界判断","evidence":"仍需核对输出","section":"模型判断"}],
                        "application":"例子","uncertainties":["未外部核验"],"external_checks":[]}}}

    def test_body_uses_exact_transcript_excerpts_and_marks_review_boundary(self):
        body,_=_note_analysis(self.value(),"工具需要明确输入，而且仍需核对输出。")
        self.assertIn("尚未人工复审",body)

    def test_invented_evidence_and_fake_external_checks_are_rejected(self):
        for field in ("evidence","external"):
            value=self.value()
            if field=="evidence":value["analysis"]["learning_review"]["key_claims"][0]["evidence"]="原文没有的数字"
            else:value["analysis"]["learning_review"]["external_checks"]=[{"url":"https://example.org"}]
            with self.subTest(field=field),self.assertRaises(ValueError):
                _note_analysis(value,"工具需要明确输入，而且仍需核对输出。")

    def test_saved_note_identity_and_index_are_read_without_modifying_note(self):
        with tempfile.TemporaryDirectory(prefix="rag-dy-note-") as tmp:
            vault=Path(tmp);notes=vault/"知识库";notes.mkdir()
            video_id="7679012030732340530";note=notes/"实例.md"
            original=("---\ntitle: 实例\naweme_id: '"+video_id+"'\nsource_url: https://www.douyin.com/video/"+video_id+"\n---\n# 实例\n正文\n").encode()
            note.write_bytes(original)
            (vault/"知识总索引.md").write_text("[[知识库/实例]]",encoding="utf-8")
            (vault/"导入总日志.md").write_text(video_id,encoding="utf-8")
            value=_saved_note(note,video_id,{"vault":str(vault),"notes_root":str(notes)},False)
            self.assertFalse(value["written"]);self.assertEqual(value["warnings"],[])
            self.assertEqual(note.read_bytes(),original)
            with self.assertRaises(BusinessError):
                _saved_note(note,"7679012030732340531",{"vault":str(vault),"notes_root":str(notes)},False)


    def test_encoded_markdown_index_link_is_verified_without_substring_matches(self):
        from urllib.parse import quote
        from raghub.douyin_worker import _saved_note
        with tempfile.TemporaryDirectory(prefix="rag-dy-encoded-") as tmp:
            vault=Path(tmp);notes=vault/"知识库";notes.mkdir()
            video_id="7679012030732340530";note=notes/"MCP 与 Skill.md"
            note.write_text("---\ntitle: 样例\naweme_id: '"+video_id+"'\nsource_url: https://www.douyin.com/video/"+video_id+"\n---\n正文\n",encoding="utf-8")
            index=vault/"知识总索引.md"
            (vault/"导入总日志.md").write_text(video_id,encoding="utf-8")
            relative=note.relative_to(vault).as_posix()
            index.write_text("[样例]("+quote(relative)+")",encoding="utf-8")
            tool={"vault":str(vault),"notes_root":str(notes)}
            self.assertEqual(_saved_note(note,video_id,tool,True)["warnings"],[])
            for invalid in (relative,"[其他笔记]("+quote("其他/"+relative)+")",
                            "[相似名称]("+quote(relative+".backup")+")"):
                index.write_text(invalid,encoding="utf-8")
                self.assertEqual(len(_saved_note(note,video_id,tool,True)["warnings"]),1)


if __name__=="__main__":unittest.main()
