"""Business transactions for parsing existing archives and reviewed full JSON."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from hashlib import sha256

from ..db import new_id,now,transaction
from ..ingestion.archive import hash_file,inside,json_bytes,json_hash,verify_archive,write_closed
from ..ingestion.store import IngestionStore,BusinessError,event,require_instance,require_document
from ..parsing_assets import parsing_models
from .bundle import revision_diff,verify_bundle,review_plan
from .formats import no_duplicates,validate_selection_config
from .contracts import Supplement


def json_hash_bytes(value):
    return sha256(json_bytes(value)).hexdigest()


class ParsingStore(IngestionStore):
    def result(self, processing_id):
        row=self.db.execute("SELECT r.*,p.status,p.raw_version_id,p.config_sha256 FROM processing_results r "
                            "JOIN processing_versions p USING(processing_version_id) "
                            "JOIN raw_versions v ON v.raw_version_id=p.raw_version_id "
                            "JOIN documents d ON d.document_id=v.document_id "
                            "WHERE r.processing_version_id=? AND d.removed_at IS NULL",
                            (processing_id,)).fetchone()
        if row is None:
            raise BusinessError("PARSE_RESULT_NOT_FOUND","当前处理版本尚无采用的完整解析产物。",404)
        checked=verify_bundle(Path(row["bundle_path"]))
        if checked["manifest_sha256"]!=row["bundle_sha256"]:
            raise BusinessError("PARSE_RESULT_CHANGED","解析产物校验失败；停止复用。")
        return {**dict(row),"bundle":checked}

    def bind_value_copy(self,raw,kb_id,xlsx_config):
        if xlsx_config is None:return
        xlsx_config.pop("value_copy_origin",None)
        reference=xlsx_config.get("value_copy_of")
        if reference:
            if (set(reference)!={"raw_version_id","source","note"} or reference["source"] not in
                    {"LOCAL_USER_CLI","LOCAL_USER_UI","CHATGPT_USER_MESSAGE"} or not isinstance(reference["note"],str)
                    or not reference["note"].strip() or len(reference["note"])>2000):
                raise BusinessError("VALUE_COPY_USER_VERIFICATION_REQUIRED","值副本须关联公式原件和用户的重算核验说明。",422)
            origin=self.db.execute("SELECT r.* FROM raw_versions r JOIN documents d USING(document_id) "
                "WHERE raw_version_id=? AND d.kb_id=? AND d.removed_at IS NULL",
                (reference["raw_version_id"],kb_id)).fetchone()
            if origin is None or origin["raw_version_id"]==raw["raw_version_id"]:
                raise BusinessError("VALUE_COPY_ORIGIN_INVALID","值副本必须来自同一知识库中明确的另一原件版本。",422)
            original=verify_archive(Path(origin["archive_manifest_path"]))
            xlsx_config["value_copy_origin"]={"archive_manifest_path":original["manifest_path"],
                "archive_manifest_sha256":original["manifest_sha256"],"raw_version_id":origin["raw_version_id"],
                "user_statement":{"source":reference["source"],"note":reference["note"]}}

    def queue_parse(self,instance,request_key,kb_id,document_id,config,*,revision=None,decision=None,expected_raw_version_id=None):
        user_config=copy.deepcopy(config)
        lock=parsing_models(self.settings)
        from ..parsing_assets import parser_identity
        config={**copy.deepcopy(config),**parser_identity(self.settings,lock)}
        request_digest=json_hash({"kb_id":kb_id,"document_id":document_id,"config":config,"revision":revision,"decision":decision,"expected_raw_version_id":expected_raw_version_id})
        with transaction(self.db):
            require_instance(self.db,instance)
            prior=self.db.execute("SELECT task_id FROM tasks WHERE data_instance_id=? AND request_key=?",
                                  (instance,request_key)).fetchone()
            if prior:
                payload=self.input_for(prior["task_id"])
                if payload.get("parse_request_sha256")!=request_digest:
                    raise BusinessError("REQUEST_KEY_CONFLICT","同一请求键的解析输入已改变。")
                return {"task_id":prior["task_id"],"already_accepted":True,"processing_version_id":payload["processing_version_id"]}
            doc=require_document(self.db,kb_id,document_id)
            if doc["active_prepare_task_id"]:
                task=self.db.execute("SELECT status FROM tasks WHERE task_id=?",(doc["active_prepare_task_id"],)).fetchone()
                if task and task["status"] in {"PENDING","RUNNING"}:
                    raise BusinessError("DOCUMENT_BUSY","当前文档准备工作尚未结束。")
            raw=self.db.execute("SELECT * FROM raw_versions WHERE raw_version_id=?",(doc["current_raw_version_id"],)).fetchone()
            if raw is None:
                raise BusinessError("ARCHIVE_REQUIRED","请先完成原件归档。")
            if expected_raw_version_id is not None and raw["raw_version_id"]!=expected_raw_version_id:
                raise BusinessError("RAW_VERSION_CHANGED","原件版本已改变，请重新读取并核对解析范围。",409)
            archive=verify_archive(Path(raw["archive_manifest_path"]))
            if not revision:
                primary=next((item["path"] for item in archive["files"] if item["path"].startswith("original.")),"captured.html")
                try:validate_selection_config(Path(primary).suffix.lower().lstrip("."),user_config)
                except ValueError as error:raise BusinessError("PARSE_CONFIG_INVALID",str(error),422) from error
            previous=self.db.execute("SELECT o.details_json,i.payload_json FROM source_observations o JOIN task_inputs i USING(task_id) "
                                     "WHERE o.raw_version_id=? ORDER BY o.created_at DESC LIMIT 1",(raw["raw_version_id"],)).fetchone()
            if previous is None:
                raise BusinessError("SOURCE_OBSERVATION_REQUIRED","原件缺少已采用来源记录。")
            payload=json.loads(previous["payload_json"])
            details=json.loads(previous["details_json"])
            if payload["kind"]=="URL":
                captured_body=details["body_xpath"]
                if config.get("html",{}).get("body_xpath",captured_body)!=captured_body:
                    raise BusinessError("HTML_SCOPE_CHANGED","网页正文范围必须使用本次归档快照的既定范围。",422)
                config["html"]={"body_xpath":captured_body}
            config["linked_resources"]=[]
            context=details.get("directory_context") or {}
            for relation in context.get("image_relations",[]):
                if relation["status"]!="RESOLVED":
                    continue
                frozen=[item for item in details.get("local_asset_members",[])
                        if item["relative_path"]==relation["resolved_relative_path"]]
                if frozen:
                    member=next((item for item in archive["files"] if item["path"]==frozen[0]["archive_member"]),None)
                    if len(frozen)!=1 or member is None or member["sha256"]!=relation["image_sha256"]:
                        raise BusinessError("MARKDOWN_LINKED_VERSION_CHANGED","归档内图片不等于已确认的 Markdown 依赖。")
                    config["linked_resources"].append({"archive_manifest_path":archive["manifest_path"],
                        "archive_manifest_sha256":archive["manifest_sha256"],"member":member["path"],
                        "sha256":member["sha256"],"reference":relation["image_reference"]})
                    continue
                linked=self.db.execute("SELECT r.* FROM document_sources s JOIN documents d USING(document_id) "
                    "JOIN raw_versions r ON r.raw_version_id=d.current_raw_version_id "
                    "WHERE s.source_root_id=? AND s.relative_path=? AND d.removed_at IS NULL",
                    (context["source_root_id"],relation["resolved_relative_path"])).fetchall()
                if len(linked)!=1:
                    raise BusinessError("MARKDOWN_LINKED_ARCHIVE_REQUIRED","关联图片须先完成同一目录的唯一归档。")
                linked_archive=verify_archive(Path(linked[0]["archive_manifest_path"]))
                primary=[item for item in linked_archive["files"] if item["path"].startswith("original.")]
                if len(primary)!=1 or primary[0]["sha256"]!=relation["image_sha256"]:
                    raise BusinessError("MARKDOWN_LINKED_VERSION_CHANGED","关联图片已不等于本次 Markdown 原件关系。")
                config["linked_resources"].append({"archive_manifest_path":linked_archive["manifest_path"],
                    "archive_manifest_sha256":linked_archive["manifest_sha256"],"member":primary[0]["path"],
                    "sha256":primary[0]["sha256"],"reference":relation["image_reference"]})
            self.bind_value_copy(raw,kb_id,config.get("xlsx"))
            parent=doc["current_processing_version_id"]
            prior_processing=self.db.execute("SELECT p.*,c.origin_kind,c.prepare_task_id FROM processing_versions p LEFT JOIN processing_configs c USING(processing_version_id) WHERE processing_version_id=?",(parent,)).fetchone()
            if not revision and prior_processing and prior_processing["status"]=="FAILED" and prior_processing["origin_kind"]=="AUTOMATIC" and prior_processing["raw_version_id"]==raw["raw_version_id"] and prior_processing["config_sha256"]==json_hash(config):
                raise BusinessError("USE_EXISTING_TASK_RECOVERY","相同输入的失败任务须从原任务恢复，保留原有执行预算。")
            processing_id=new_id()
            if revision:
                preview=self.db.execute("SELECT * FROM revision_previews WHERE preview_id=?",(revision["revision_id"],)).fetchone()
                if preview is None or preview["base_processing_version_id"]!=parent or preview["submitted_processing_version_id"] or preview["human_decision_id"]:
                    raise BusinessError("REVISION_BASE_CHANGED","人工修订基准已改变或已经提交。")
                base=self.result(parent)
                if base["bundle_sha256"]!=preview["base_bundle_sha256"] or hash_file(Path(preview["revision_path"]))[0]!=preview["revision_sha256"]:
                    raise BusinessError("REVISION_INPUT_CHANGED","修订或基础产物身份改变，须重新预览。")
                if not decision or decision["decision"] not in {"ACCEPT","MODIFY_ACCEPT"} or not decision["note"].strip():
                    raise BusinessError("HUMAN_DECISION_REQUIRED","须由用户确认具体修订差异。")
                base_native=json.loads((Path(base["bundle_path"]).parent/"cleaned.native.json").read_bytes())
                revised=json.loads(Path(preview["revision_path"]).read_bytes(),object_pairs_hook=no_duplicates)
                supplement=Supplement.model_validate_json((Path(base["bundle_path"]).parent/"supplement.json").read_bytes())
                reviews=json.loads(preview["review_json"])
                difference=revision_diff(base_native,revised)
                bound=review_plan(base_native,revised,supplement,base["bundle_sha256"],reviews)
                preview_binding={"base_processing_version_id":parent,"raw_version_id":supplement.raw_version_id,
                    "base_bundle_sha256":base["bundle_sha256"],"revision_sha256":preview["revision_sha256"],
                    "differences":difference,"reviews":bound}
                if json_hash(bound)!=preview["review_sha256"] or json_hash(preview_binding)!=preview["preview_sha256"]:
                    raise BusinessError("REVISION_INPUT_CHANGED","人工复核内容已改变，须重新预览。")
                revision={"revision_id":preview["preview_id"],"path":preview["revision_path"],"sha256":preview["revision_sha256"],
                          "base_bundle":base["bundle_path"],"reviews":reviews,"review_sha256":preview["review_sha256"]}
                decision_id=new_id()
                self.db.execute("INSERT INTO human_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                    (decision_id,"REVISION",preview["preview_id"],parent,decision["decision"],decision["source"],
                     decision["note"],preview["preview_sha256"],now()))
                revision["human_decision"]={"decision_id":decision_id,"decision":decision["decision"],
                    "source":decision["source"],"note":decision["note"],"confirmed_preview_sha256":preview["preview_sha256"]}
                self.db.execute("UPDATE revision_previews SET submitted_processing_version_id=?,human_decision_id=? WHERE preview_id=?",
                                (processing_id,decision_id,preview["preview_id"]))
            self.db.execute("INSERT INTO processing_versions(processing_version_id,raw_version_id,config_sha256,status,created_at) VALUES (?,?,?,'NEW',?)",
                            (processing_id,raw["raw_version_id"],json_hash(config),now()))
            payload={**payload,"operation":"PARSE_ARCHIVE","processing_version_id":processing_id,
                     "raw_version_id":raw["raw_version_id"],"archive":archive,"source_details":details,
                     "parsing_config":config,"revision":revision,"parse_request_sha256":request_digest}
            queued=self.queue_input(instance,request_key,payload)
            self.db.execute("UPDATE file_input_claims SET task_id=? WHERE data_instance_id=? AND document_id=?",
                            (queued["task_id"],instance,document_id))
            self.db.execute("UPDATE documents SET current_processing_version_id=? WHERE document_id=?",(processing_id,document_id))
            self.db.execute("UPDATE tasks SET stage='PARSE_PENDING' WHERE task_id=?",(queued["task_id"],))
            self.db.execute("INSERT INTO processing_configs VALUES (?,?,?,?,?,?)",
                            (processing_id,json.dumps(config,ensure_ascii=False,sort_keys=True),parent,
                             "MANUAL_JSON" if revision else "AUTOMATIC",revision["revision_id"] if revision else None,queued["task_id"]))
            event(self.db,queued["task_id"],queued["attempt_id"],"PARSE_REQUESTED",
                  {"raw_version_id":raw["raw_version_id"],"processing_version_id":processing_id,"reacquisition":False})
            return {**queued,"processing_version_id":processing_id,"reacquisition":False}

    def adopt_intermediate(self,identity,record,input_digest):
        from .cache import verify_parse
        if verify_parse(record["manifest_path"])!=record:
            raise ValueError("PARSE intermediate changed before adoption")
        payload=self.input_for(identity[1])
        manifest=record["manifest"]
        observed=tuple(manifest[k] for k in ("processing_version_id","raw_version_id","config_sha256","stage_input_sha256"))
        expected=(payload["processing_version_id"],payload["raw_version_id"],json_hash(payload["parsing_config"]),input_digest)
        if observed!=expected:
            raise ValueError("PARSE intermediate input mismatch")
        with transaction(self.db):
            self.current_owner(*identity)
            doc=require_document(self.db,payload["kb_id"],payload["document_id"])
            if doc["current_processing_version_id"]!=payload["processing_version_id"] or doc["active_prepare_task_id"]!=identity[1]:
                raise BusinessError("PARSE_PREPARATION_CHANGED","解析阶段已不是当前文档准备任务。")
            artifact_id=new_id()
            self.db.execute("INSERT INTO stage_artifacts(artifact_id,task_id,attempt_id,stage,relative_path,sha256,adopted,created_at) VALUES (?,?,?,'PARSE_AUTO',?,?,1,?)",
                (artifact_id,identity[1],identity[2],str(Path(record["manifest_path"]).relative_to(self.settings.home)),record["manifest_sha256"],now()))
            self.db.execute("INSERT INTO stage_outputs VALUES (?,?,?,?,?)",
                (artifact_id,payload["processing_version_id"],input_digest,json.dumps(record,ensure_ascii=False,sort_keys=True),None))
            event(self.db,identity[1],identity[2],"PARSE_STAGE_ADOPTED",{"artifact_id":artifact_id,"input_sha256":input_digest})
        return artifact_id

    def adopt_parse(self,identity,bundle,input_digest,*,reuse_of=None):
        verified=verify_bundle(Path(bundle["manifest_path"]))
        if verified!=bundle:
            raise ValueError("parsing result changed before adoption")
        payload=self.input_for(identity[1])
        with transaction(self.db):
            self.current_owner(*identity)
            doc=require_document(self.db,payload["kb_id"],payload["document_id"])
            p=payload["processing_version_id"]
            if doc["active_prepare_task_id"]!=identity[1] or doc["current_processing_version_id"]!=p:
                raise BusinessError("PARSE_PREPARATION_CHANGED","解析结果已不是当前准备任务。")
            expected=(p,payload["raw_version_id"],json_hash(payload["parsing_config"]))
            observed=tuple(bundle["manifest"][k] for k in ("processing_version_id","raw_version_id","config_sha256"))
            if expected!=observed:
                raise BusinessError("PARSE_INPUT_CHANGED","解析结果与处理输入身份不一致。")
            artifact_id=new_id()
            self.db.execute("INSERT INTO stage_artifacts(artifact_id,task_id,attempt_id,stage,relative_path,sha256,adopted,created_at) VALUES (?,?,?,'CLEAN',?,?,1,?)",
                (artifact_id,identity[1],identity[2],str(Path(bundle["manifest_path"]).relative_to(self.settings.home)),bundle["manifest_sha256"],now()))
            self.db.execute("INSERT INTO stage_outputs VALUES (?,?,?,?,?)",
                (artifact_id,p,input_digest,json.dumps(bundle,ensure_ascii=False,sort_keys=True),reuse_of))
            self.db.execute("INSERT INTO processing_results VALUES (?,?,?,?,?) ON CONFLICT(processing_version_id) DO UPDATE SET "
                            "artifact_id=excluded.artifact_id,bundle_path=excluded.bundle_path,bundle_sha256=excluded.bundle_sha256,created_at=excluded.created_at",
                            (p,artifact_id,bundle["manifest_path"],bundle["manifest_sha256"],now()))
            self.db.execute("UPDATE processing_versions SET status=?,scope_report_path=?,scope_report_sha256=? WHERE processing_version_id=?",
                (bundle["status"],str(Path(bundle["manifest_path"]).parent/"supplement.json"),
                 hash_file(Path(bundle["manifest_path"]).parent/"supplement.json")[0],p))
            self.db.execute("UPDATE tasks SET stage=? WHERE task_id=?",(bundle["status"],identity[1]))
            event(self.db,identity[1],identity[2],"PARSE_ADOPTED",{"artifact_id":artifact_id,"bundle":bundle,"query_publication":False})
            self.db.execute("UPDATE tasks SET status=\'SUCCEEDED\',last_error_json=NULL,updated_at=? WHERE task_id=?",(now(),identity[1]))
            self.db.execute("UPDATE attempts SET state=\'SUCCEEDED\',finished_at=? WHERE attempt_id=?",(now(),identity[2]))
            event(self.db,identity[1],identity[2],"SUCCEEDED",{"scope":"PARSE_PREPARATION_ONLY"})
            return {"processing_version_id":p,"status":bundle["status"],"artifact_id":artifact_id,"query_publication":False}

    def revision_preview(self,instance,document_id,processing_id,revised,reviews=None,*,origin_decision_id=None):
        require_instance(self.db,instance)
        result=self.result(processing_id)
        doc=self.db.execute("SELECT * FROM documents WHERE document_id=?",(document_id,)).fetchone()
        if doc is None or doc["removed_at"] or doc["current_processing_version_id"]!=processing_id:
            raise BusinessError("REVISION_BASE_CHANGED","当前复核版本已改变。")
        base=json.loads((Path(result["bundle_path"]).parent/"cleaned.native.json").read_bytes())
        supplement=Supplement.model_validate_json((Path(result["bundle_path"]).parent/"supplement.json").read_bytes())
        reviews=reviews or []
        try:
            difference=revision_diff(base,revised)
            bound=review_plan(base,revised,supplement,result["bundle_sha256"],reviews)
            if not difference and not bound:
                raise ValueError("select concrete issues to review or change existing text")
        except (ValueError,KeyError,TypeError) as exc:
            raise BusinessError("REVISION_INVALID",str(exc),422) from exc
        if origin_decision_id:
            existing=self.db.execute("SELECT * FROM revision_previews WHERE origin_decision_id=?",(origin_decision_id,)).fetchone()
            if existing:
                if (hash_file(Path(existing["revision_path"]))[0]!=json_hash_bytes(revised)
                        or json.loads(existing["review_json"])!=reviews):
                    raise BusinessError("OCR_DECISION_CONFLICT","该人工决定对应另一份修订内容。")
                return {"preview_id":existing["preview_id"],"revision_sha256":existing["revision_sha256"],
                        "preview_sha256":existing["preview_sha256"],"differences":difference,"reviews":bound,"accepted":False}
        preview_id=new_id()
        directory=self.settings.home/"data/processing/revision-previews"/preview_id
        directory.mkdir(parents=True,exist_ok=False)
        path=directory/"full-revision.native.json"
        write_closed(path,json_bytes(revised))
        path.chmod(0o444)
        native_sha=hash_file(path)[0]
        preview_binding={"base_processing_version_id":processing_id,"raw_version_id":supplement.raw_version_id,
            "base_bundle_sha256":result["bundle_sha256"],"revision_sha256":native_sha,
            "differences":difference,"reviews":bound}
        preview_sha=json_hash(preview_binding)
        with transaction(self.db):
            require_instance(self.db,instance)
            current=self.db.execute("SELECT current_processing_version_id,removed_at FROM documents WHERE document_id=?",(document_id,)).fetchone()
            if current["removed_at"] or current["current_processing_version_id"]!=processing_id:
                raise BusinessError("REVISION_BASE_CHANGED","修订预览期间基础版本改变。")
            self.db.execute("INSERT INTO revision_previews(preview_id,data_instance_id,document_id,base_processing_version_id,"
                "base_bundle_sha256,revision_path,revision_sha256,difference_json,created_at,review_json,review_sha256,preview_sha256,origin_decision_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (preview_id,instance,document_id,processing_id,result["bundle_sha256"],str(path),native_sha,
                 json.dumps(difference,ensure_ascii=False),now(),json.dumps(reviews,ensure_ascii=False),json_hash(bound),preview_sha,origin_decision_id))
        return {"preview_id":preview_id,"base_processing_version_id":processing_id,"revision_sha256":native_sha,
                "preview_sha256":preview_sha,"differences":difference,"reviews":bound,"accepted":False}

    def submit_revision(self,instance,request_key,preview_id,decision):
        preview=self.db.execute("SELECT * FROM revision_previews WHERE preview_id=?",(preview_id,)).fetchone()
        if preview is None or preview["data_instance_id"]!=instance:
            raise BusinessError("REVISION_NOT_FOUND","修订预览不存在。",404)
        if decision.get("preview_sha256")!=preview["preview_sha256"]:
            raise BusinessError("REVISION_CONFIRMATION_CHANGED","用户须确认当前完整差异与疑点决定的预览指纹。",422)
        if decision["decision"]=="REJECT":
            with transaction(self.db):
                require_instance(self.db,instance)
                if preview["human_decision_id"]:
                    existing=self.db.execute("SELECT * FROM human_decisions WHERE decision_id=?",(preview["human_decision_id"],)).fetchone()
                    if any(existing[k]!=decision[k] for k in ("decision","source","note")):
                        raise BusinessError("REVISION_DECISION_CONFLICT","此预览已经记录另一项决定。")
                    return {"decision_id":existing["decision_id"],"text_applied":False,"rejected":True}
                identity=new_id()
                self.db.execute("INSERT INTO human_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                    (identity,"REVISION",preview_id,preview["base_processing_version_id"],"REJECT",decision["source"],
                     decision["note"],preview["preview_sha256"],now()))
                self.db.execute("UPDATE revision_previews SET human_decision_id=? WHERE preview_id=?",(identity,preview_id))
            return {"decision_id":identity,"text_applied":False,"rejected":True}
        config=self.db.execute("SELECT config_json FROM processing_configs WHERE processing_version_id=?",
                               (preview["base_processing_version_id"],)).fetchone()
        if config is None:
            raise BusinessError("REVISION_CONFIG_MISSING","基础解析配置缺失。")
        doc=self.db.execute("SELECT kb_id FROM documents WHERE document_id=?",(preview["document_id"],)).fetchone()
        return self.queue_parse(instance,request_key,doc[0],preview["document_id"],json.loads(config[0]),
                                revision={"revision_id":preview_id},decision=decision)
