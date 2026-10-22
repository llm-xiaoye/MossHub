"""Bounded local UI reads. Client paths never select files on disk."""
from __future__ import annotations
import csv
import json
from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import Field, UUID4
from .contracts import StrictRecord
from .ingestion.contracts import DirectoryPreviewRequest
from .ingestion.store import IngestionStore
from .config import Settings
from .process_identity import process_start_time
from .db import open_db
from .ingestion.store import BusinessError, require_instance
from .ingestion.archive import inside, verify_archive
from .parsing.store import ParsingStore
from .chat.router import event_packet

router = APIRouter(prefix="/api/ui")
settings = Settings.load()
from .obsidian import create_router as create_obsidian_router, create_directory_update_router
obsidian_router = create_obsidian_router(settings)
workflow_engine = obsidian_router.workflow_engine
router.include_router(obsidian_router)
router.include_router(create_directory_update_router(settings))
from .douyin_intake import create_router as create_douyin_router
router.include_router(create_douyin_router(settings, workflow_engine.vault))

@router.get("/import/availability")
def import_availability(instance: str = Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        active=db.execute("SELECT 1 FROM import_workflows WHERE data_instance_id=? "
                          "AND state NOT IN ('COMPLETE','PARTIAL_COMPLETE','CANCELLED') LIMIT 1",(instance,)).fetchone()
        video=db.execute("SELECT 1 FROM tasks t JOIN generation_inputs i USING(task_id) WHERE t.data_instance_id=? "
                         "AND t.status IN (\'PENDING\',\'RUNNING\') AND json_extract(i.payload_json,\'$.operation\')=\'DOUYIN_NOTE\' LIMIT 1",(instance,)).fetchone()
        return {"busy":active is not None or video is not None}

class ContinueIntake(StrictRecord):
    kb_id: UUID4
    task_ids: list[UUID4] = Field(min_length=1,max_length=50)

@router.post("/import/continue")
def continue_intake(body: ContinueIntake, instance: str = Header(alias="X-RAG-Data-Instance"),
                    request_key: str = Header(alias="X-Request-ID")):
    return workflow_engine.continue_tasks(instance,request_key,str(body.kb_id),[str(v) for v in body.task_ids])

@router.get("/import/continue/by-request/{request_key}")
def continuation_status(request_key: UUID4, kb_id: UUID4, instance: str = Header(alias="X-RAG-Data-Instance")):
    return workflow_engine.latest(instance,str(kb_id),str(request_key))


def bounded(value, omitted, path="", depth=0):
    if depth > 7:
        omitted.append(path); return {"omitted": "展开深度超过当前视图"}
    if isinstance(value, str) and len(value) > 8000:
        omitted.append(path); return {"preview": value[:8000], "total_characters": len(value)}
    if isinstance(value, list):
        if len(value) > 40: omitted.append(path)
        return [bounded(item, omitted, path+"/"+str(i), depth+1) for i,item in enumerate(value[:40])]
    if isinstance(value, dict):
        output={}
        for index,(key,item) in enumerate(value.items()):
            if index>=100:omitted.append(path+"/*");break
            if key in {"sources","mappings","raw_intervals","support_unit_whitelist","serialized_evidence"}:omitted.append(path+"/"+key)
            else:output[key]=bounded(item,omitted,path+"/"+key,depth+1)
        return output
    return value

def light_result(value):
    if value is None: return None
    return event_packet({"request_id":"","conversation_id":"","data_instance_id":"","state":"SUCCEEDED",
                         "error":None,"draft":{"revision":0,"text":"","validated":False},"result":value})["result"]

@router.get("/knowledge-bases")
def knowledge_bases():
    with closing(open_db(settings.sqlite)) as db:
        rows=db.execute("SELECT * FROM knowledge_bases ORDER BY created_at DESC LIMIT 100").fetchall()
        return {"items":[dict(row) for row in rows]}

class DirectoryScan(StrictRecord):
    kb_id:UUID4
    absolute_root:str=Field(min_length=1,max_length=8192)

@router.post("/directory-scan")
def directory_scan(body:DirectoryScan,instance:str=Header(alias="X-RAG-Data-Instance")):
    import importlib.util
    with closing(open_db(settings.sqlite)) as db:require_instance(db,instance)
    path=settings.project/"scripts/directory.py"
    spec=importlib.util.spec_from_file_location("raghub_directory_scanner",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    try:value=module.scan(body.absolute_root,str(body.kb_id),max_files=50,max_bytes=500*1024*1024,max_entries=2000)
    except (OSError,ValueError) as error:raise BusinessError("DIRECTORY_SCAN_FAILED",str(error),422) from error
    checked=DirectoryPreviewRequest.model_validate(value).model_dump(mode="json")
    if len(json.dumps(checked,ensure_ascii=False).encode())>512*1024:raise BusinessError("DIRECTORY_SCAN_TOO_LARGE","目录预览超过512 KiB，请缩小目录。",413)
    return checked

def reader_notice(db,document_id,raw_version_id):
    if not raw_version_id:
        return None
    rows=db.execute("SELECT o.details_json FROM source_observations o JOIN task_inputs i USING(task_id) "
                    "WHERE i.document_id=? AND o.raw_version_id=? ORDER BY o.created_at DESC,o.observation_id DESC LIMIT 16",
                    (document_id,raw_version_id)).fetchall()
    for item in rows:
        detail=json.loads(item["details_json"])
        if isinstance(detail,dict) and detail.get("input_kind")=="WEB_TEXT_READER":
            from .ingestion.wechat_reader import LIMITATION
            return LIMITATION
    return None

@router.get("/documents")
def documents(kb_id:str, offset:int=Query(0,ge=0), limit:int=Query(50,ge=1,le=100)):
    with closing(open_db(settings.sqlite)) as db:
        total=db.execute("SELECT count(*) FROM documents WHERE kb_id=? AND removed_at IS NULL",(kb_id,)).fetchone()[0]
        rows=db.execute(
            "SELECT d.*,p.status AS processing_status,t.status AS task_status,t.stage,"
            "g.generation_id AS published_generation_id,"
            "gd.raw_version_id AS published_raw_version_id,"
            "gd.processing_version_id AS published_processing_version_id,"
            "CASE WHEN d.removed_at IS NULL AND gd.document_id IS NOT NULL THEN 1 ELSE 0 END AS has_published_version,"
            "CASE WHEN d.removed_at IS NULL AND gd.document_id IS NOT NULL "
            "AND gd.raw_version_id=d.current_raw_version_id "
            "AND gd.processing_version_id=d.current_processing_version_id AND p.status='READY' "
            "THEN 1 ELSE 0 END AS current_version_published "
            "FROM documents d "
            "LEFT JOIN processing_versions p ON p.processing_version_id=d.current_processing_version_id "
            "LEFT JOIN tasks t ON t.task_id=d.active_prepare_task_id "
            "LEFT JOIN knowledge_bases k ON k.kb_id=d.kb_id "
            "LEFT JOIN generations g ON g.generation_id=k.current_generation_id AND g.kb_id=d.kb_id AND g.state=? "
            "LEFT JOIN generation_documents gd ON gd.generation_id=g.generation_id AND gd.document_id=d.document_id "
            "WHERE d.kb_id=? AND d.removed_at IS NULL ORDER BY d.created_at DESC LIMIT ? OFFSET ?",
            ("CURRENT",kb_id,limit,offset)).fetchall()
        items=[]
        for row in rows:
            value=dict(row)
            value["source_notice"]=reader_notice(db,value["document_id"],value["current_raw_version_id"])
            registered=obsidian_router.registered_sources._rows(db,kb_id,document_id=value["document_id"],limit=2)
            value["can_update"]=len(registered)==1
            value["has_published_version"]=bool(value["has_published_version"])
            value["current_version_published"]=bool(value["current_version_published"])
            items.append(value)
        return {"items":items,"total":total,"offset":offset,"limit":limit}

@router.get("/tasks")
def tasks(kb_id:str, offset:int=Query(0,ge=0), limit:int=Query(50,ge=1,le=100)):
    with closing(open_db(settings.sqlite)) as db:
        rows=db.execute("SELECT t.*,i.document_id,COALESCE(d.display_name,CASE WHEN json_extract(g.payload_json,'$.operation')='DOUYIN_NOTE' THEN '抖音视频笔记' WHEN g.task_id IS NOT NULL THEN '本批资料建库' END) AS display_name,a.state AS attempt_state FROM tasks t "
                        "LEFT JOIN task_inputs i USING(task_id) LEFT JOIN generation_inputs g USING(task_id) "
                        "LEFT JOIN documents d ON d.document_id=i.document_id LEFT JOIN attempts a ON a.attempt_id=t.current_attempt_id "
                        "WHERE COALESCE(i.kb_id,g.kb_id)=? AND (d.document_id IS NULL OR d.removed_at IS NULL) ORDER BY t.created_at DESC LIMIT ? OFFSET ?",(kb_id,limit,offset)).fetchall()
        items=[]
        for row in rows:
            value=dict(row)
            value["last_error"]=json.loads(value.pop("last_error_json")) if value["last_error_json"] else None
            items.append(value)
        return {"items":items,"offset":offset,"limit":limit}

def conversation_view(db, row):
    """Show the same retained-generation boundary used by question admission."""
    value = dict(row)
    if not value["read_only"]:
        from .indexing.cleanup import require_query_generation
        try:
            require_query_generation(db, value["kb_id"], value["generation_id"])
        except BusinessError as error:
            if error.code != "GENERATION_READ_ONLY":
                raise
            value["read_only"] = True
    return value

@router.get("/conversations")
def conversations(kb_id:str, instance:str=Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        rows=db.execute("SELECT c.conversation_id,c.created_at,s.kb_id,s.generation_id,s.read_only,"
                        "(SELECT user_text FROM chat_turns t WHERE t.conversation_id=c.conversation_id ORDER BY ordinal LIMIT 1) AS title "
                        "FROM chat_conversations c JOIN generation_sessions s ON s.session_id=c.conversation_id "
                        "WHERE s.kb_id=? AND c.data_instance_id=? ORDER BY c.created_at DESC LIMIT 100",(kb_id,instance)).fetchall()
        return {"items":[conversation_view(db, row) for row in rows]}

def memory_notices(record):
    trace=record.get("trace") or {}
    diagnostic=(trace.get("chat") or {}).get("summary_processing") or {}
    error=diagnostic.get("error")
    if not error:return []
    return ["本轮摘要维护未成功，原始历史与上一份摘要已保留；未自动重试。" +
            "原因："+str(error.get("type",""))+" "+str(error.get("message",""))[:1000]]
@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id:str, request:Request, instance:str=Header(alias="X-RAG-Data-Instance"),
                 before:int|None=Query(None,ge=1), limit:int=Query(20,ge=1,le=50)):
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        row=db.execute("SELECT c.*,s.kb_id,s.generation_id,s.read_only FROM chat_conversations c "
                       "JOIN generation_sessions s ON s.session_id=c.conversation_id WHERE c.conversation_id=? "
                       "AND c.data_instance_id=?",(conversation_id,instance)).fetchone()
        if row is None: raise BusinessError("CONVERSATION_NOT_FOUND","会话不存在。",404)
        value=conversation_view(db, row)
        turns=db.execute("SELECT t.*,(SELECT request_id FROM chat_requests q WHERE q.user_message_id=t.user_message_id) AS request_id FROM chat_turns t WHERE conversation_id=? AND ordinal<? "
                         "ORDER BY ordinal DESC LIMIT ?",(conversation_id,before or 2147483647,limit)).fetchall()
        value["turns"]=[]
        for turn in reversed(turns):
            item=dict(turn);item["result"]=light_result(json.loads(item.pop("result_json")))
            value["turns"].append(item)
        value["older_before"]=value["turns"][0]["ordinal"] if len(turns)==limit else None
        pending=db.execute("SELECT c.request_id,c.question,r.state,r.error_json FROM chat_requests c "
                           "JOIN retrieval_requests r USING(request_id) WHERE c.conversation_id=? "
                           "AND r.state IN (?,?) ORDER BY c.accepted_at", (conversation_id,"RUNNING","CANCELLING")).fetchall()
        value["active_requests"]=[dict(item) for item in pending]
        awaiting=db.execute("SELECT original_question,question FROM chat_clarifications WHERE conversation_id=? AND state=?",
                            (conversation_id,"AWAITING")).fetchone()
        value["awaiting"]=dict(awaiting) if awaiting else None
    for turn in value["turns"]:
        if turn.get("request_id"):
            record=await request.app.state.chat.db.call("read",instance,turn["request_id"])
            notices=memory_notices(record)
            if notices:turn["result"]["warnings"]=notices
    return value

@router.get("/question-requests/{request_id}/summary")
async def question_summary(request_id:str, request:Request, instance:str=Header(alias="X-RAG-Data-Instance")):
    packet=event_packet(await request.app.state.chat.question_snapshot(instance,request_id))
    if packet.get("result") is not None and packet["state"] in {"SUCCEEDED","FAILED","CANCELLED","INTERRUPTED"}:
        record=await request.app.state.chat.db.call("read",instance,request_id)
        notices=memory_notices(record)
        if notices:packet["result"]["warnings"]=notices
    return packet

@router.get("/submissions/{request_key}")
def submission(request_key:str, instance:str=Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        row=db.execute("SELECT request_id,conversation_id FROM chat_requests WHERE data_instance_id=? AND request_key=?",
                       (instance,request_key)).fetchone()
        if row: return {"kind":"question",**dict(row)}
        row=db.execute("SELECT conversation_id FROM chat_conversations WHERE data_instance_id=? AND request_key=?",
                       (instance,request_key)).fetchone()
        if row: return {"kind":"conversation",**dict(row)}
        row=db.execute("SELECT task_id,kind FROM tasks WHERE data_instance_id=? AND request_key=?",
                       (instance,request_key)).fetchone()
        if row:
            bound=db.execute("SELECT kb_id FROM task_inputs WHERE task_id=?",(row["task_id"],)).fetchone()
            kb_id=bound["kb_id"] if bound else None
            if kb_id is None:
                generation=db.execute("SELECT payload_json FROM generation_inputs WHERE task_id=?",(row["task_id"],)).fetchone()
                payload=json.loads(generation["payload_json"]) if generation else None
                kb_id=payload.get("kb_id") if isinstance(payload,dict) else None
            if not isinstance(kb_id,str):
                raise BusinessError("SUBMISSION_SCOPE_UNCONFIRMED","已找到原任务，但尚不能核对所属知识库；保留原请求，不重新提交。",409)
            return {"kind":"task","task_id":row["task_id"],"task_kind":row["kind"],"kb_id":kb_id}
        raise BusinessError("SUBMISSION_NOT_CONFIRMED","当前实例没有找到该提交，不能据此判断是否曾执行。",404)

@router.get("/question-requests/{request_id}/details")
async def details(request_id:str,request:Request,section:str=Query(pattern="^(memory|retrieval|budget)$"),
                  instance:str=Header(alias="X-RAG-Data-Instance")):
    record=await request.app.state.chat.db.call("read",instance,request_id)
    snapshot=await request.app.state.chat.question_snapshot(instance,request_id)
    trace=record.get("trace") or {}
    if section=="memory":
        value={"memory_context":(snapshot.get("result") or {}).get("memory_context"),"chat":trace.get("chat")}
    elif section=="budget":
        value={key:trace.get(key) for key in ("budgets","stage_timings","timing_contract","provider_calls","model_calls")}
    else:
        value={key:trace.get(key) for key in ("input","scope","recall_routes","fusion","reranking","seed_selection",
                                             "retrieval_evidence","completion")}
    omitted=[];value=bounded(value,omitted)
    if len(json.dumps(value,ensure_ascii=False).encode())>512*1024:
        raise BusinessError("DETAIL_VIEW_TOO_LARGE","本视图超过512 KiB，请缩小查看范围。",413)
    return {"request_id":request_id,"section":section,"value":value,"omitted_paths":omitted,
            "view_limit":"最多40项/8000字符/7层；省略项不作为完整证据"}

def archive_for(db,document_id,raw_version_id=None):
    doc=db.execute("SELECT * FROM documents WHERE document_id=? AND removed_at IS NULL",(document_id,)).fetchone()
    if doc is None:raise BusinessError("SOURCE_NOT_ACCESSIBLE","原件不存在或资料已移除。",404)
    version=raw_version_id or doc["current_raw_version_id"]
    if version is None:
        raise BusinessError("SOURCE_NOT_READY","原件尚未归档，请在后台任务中查看导入进度；归档完成后可查看原文和配置解析。",409)
    row=db.execute("SELECT archive_manifest_path,raw_version_id FROM raw_versions WHERE document_id=? AND raw_version_id=?",
                   (document_id,version)).fetchone()
    if row is None:raise BusinessError("SOURCE_NOT_ACCESSIBLE","该原始版本不属于此文档。",404)
    path=inside(Path(row["archive_manifest_path"]),settings.home/"data")
    checked=verify_archive(path)
    names=[item["path"] for item in checked["files"]]
    selected=next((name for name in names if name.startswith("original.")),None)
    if selected is None and "captured.html" in names:selected="captured.html"
    if selected is None:raise BusinessError("SOURCE_FORMAT_UNSUPPORTED","该归档缺少可展示的主文件。")
    return {**dict(doc),**dict(row)},inside(path.parent/selected,path.parent),checked


def archived_image_members(checked):
    suffixes={".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff",".gif",".svg"}
    return [item for item in checked["files"] if Path(item["path"]).suffix.lower() in suffixes
            and not item["path"].startswith("original.")]

@router.get("/documents/{document_id}/resource-image/{image_index}")
def resource_image(document_id:str,image_index:int,instance:str=Query(alias="data_instance_id"),
                   raw_version_id:str|None=None):
    from PIL import Image
    from io import BytesIO
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        row,_original,checked=archive_for(db,document_id,raw_version_id)
    members=archived_image_members(checked)
    if image_index<0 or image_index>=len(members):
        raise BusinessError("ARCHIVED_IMAGE_NOT_FOUND","这份归档中没有该图片。",404)
    root=inside(Path(row["archive_manifest_path"]).parent,settings.home/"data")
    image_path=inside(root/members[image_index]["path"],root)
    if image_path.suffix.lower()==".svg":
        from .svg_image import read_svg
        raw,_rows,_size=read_svg(image_path)
        return Response(raw,media_type="image/svg+xml",headers={"X-Content-Type-Options":"nosniff",
            "Cache-Control":"no-store","Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; sandbox"})
    with Image.open(image_path) as image:
        if image.width*image.height>120000000:
            raise BusinessError("IMAGE_PREVIEW_TOO_LARGE","图片超过1.2亿像素，暂时无法生成预览。原图已保留。",413)
        if image_path.suffix.lower()==".gif" and image.format=="GIF":
            return Response(image_path.read_bytes(),media_type="image/gif",
                            headers={"X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})
        preview=image.convert("RGB");preview.thumbnail((2400,2400))
        output=BytesIO();preview.save(output,format="PNG")
    return Response(output.getvalue(),media_type="image/png",
                    headers={"X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})

@router.get("/documents/{document_id}/parsing-options")
def parsing_options(document_id:str,instance:str=Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        row,path,_=archive_for(db,document_id)
        kind=path.suffix.lower().lstrip(".")
        result={"document_id":document_id,"raw_version_id":row["raw_version_id"],"kind":kind,
                "display_name":row["display_name"],"sheets":[],"tables":[],"value_copy_origins":[]}
        if kind=="xlsx":
            import openpyxl
            book=openpyxl.load_workbook(path,read_only=False,data_only=False,keep_links=False)
            try:
                result["sheets"]=[{"name":sheet.title,"state":sheet.sheet_state,"range":sheet.calculate_dimension()} for sheet in book.worksheets]
                result["tables"]=[{"sheet":sheet.title,"name":table.name,"ref":table.ref,"header_rows":table.headerRowCount or 0}
                                  for sheet in book.worksheets for table in sheet.tables.values()]
            finally:book.close()
            candidates=db.execute("SELECT r.raw_version_id,d.document_id,d.display_name FROM raw_versions r JOIN documents d USING(document_id) "
                "WHERE d.kb_id=? AND d.removed_at IS NULL AND r.raw_version_id!=? LIMIT 100",(row["kb_id"],row["raw_version_id"])).fetchall()
            for candidate in candidates:
                _,original,_=archive_for(db,candidate["document_id"],candidate["raw_version_id"])
                if original.suffix.lower()==".xlsx":
                    result["value_copy_origins"].append(dict(candidate))
    return result

class SelectionPreview(StrictRecord):
    expected_raw_version_id:UUID4
    config:dict=Field(default_factory=dict)

@router.post("/documents/{document_id}/parsing-preview")
def parsing_preview(document_id:str,body:SelectionPreview,instance:str=Header(alias="X-RAG-Data-Instance")):
    import copy
    from .parsing.formats import validate_selection_config,json_document,xlsx_document
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        row,path,_=archive_for(db,document_id)
        if row["raw_version_id"]!=str(body.expected_raw_version_id):
            raise BusinessError("RAW_VERSION_CHANGED","原件版本已改变，请重新配置。",409)
        kind=path.suffix.lower().lstrip(".")
        config=copy.deepcopy(body.config)
        try:
            validate_selection_config(kind,config)
            if kind=="json":
                scope=json_document(path,config["json"]).scope
            elif kind=="xlsx":
                ParsingStore(db,settings).bind_value_copy(row,row["kb_id"],config["xlsx"])
                scope=xlsx_document(path,config["xlsx"]).scope
            else:
                scope={"selected":"EXISTING_FORMAT_DEFAULT","format":kind,"excluded":"由该格式的既有解析与质量报告记录"}
        except (ValueError,TypeError,KeyError,IndexError) as error:
            raise BusinessError("PARSE_CONFIG_INVALID","配置无法用于当前原件："+str(error),422) from error
    value={"document_id":document_id,"raw_version_id":row["raw_version_id"],"config":body.config,"scope":scope,
           "scope_source":"ORIGINAL_INPUT_AND_EXISTING_FORMAT_ADAPTER" if kind in {"json","xlsx"} else "EXISTING_FORMAT_DEFAULT_CONTRACT"}
    if len(json.dumps(value,ensure_ascii=False).encode())>512*1024:
        raise BusinessError("PARSE_SCOPE_TOO_LARGE","处理范围预览超过512 KiB，请缩小选定字段或范围；未提交任务。",413)
    return value

@router.get("/documents/{document_id}/content")
def content(document_id:str,instance:str=Query(alias="data_instance_id"),raw_version_id:str|None=None,download:bool=False):
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        row,path,_=archive_for(db,document_id,raw_version_id)
    media="application/pdf" if path.suffix.lower()==".pdf" else "application/octet-stream"
    if path.suffix.lower() in {".png",".jpg",".jpeg",".webp",".gif"}:
        media="image/"+("jpeg" if path.suffix.lower() in {".jpg",".jpeg"} else path.suffix.lower()[1:])
    return FileResponse(path,media_type=media,filename=row["display_name"],
                        content_disposition_type="attachment" if download else "inline",
                        headers={"X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})

@router.get("/documents/{document_id}/image-preview")
def image_preview(document_id:str,instance:str=Query(alias="data_instance_id"),raw_version_id:str|None=None,frame:int=Query(0,ge=0,le=2047)):
    import io
    from PIL import Image
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False);_,path,_=archive_for(db,document_id,raw_version_id)
    if path.suffix.lower()==".svg":
        from .svg_image import read_svg
        raw,_rows,_size=read_svg(path)
        return Response(raw,media_type="image/svg+xml",headers={"X-Content-Type-Options":"nosniff",
            "Cache-Control":"no-store","Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; sandbox"})
    if path.suffix.lower() not in {".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff",".gif"}:raise BusinessError("IMAGE_PREVIEW_UNSUPPORTED","此原件不是受支持图片。",422)
    with Image.open(path) as image:
        if frame>=getattr(image,"n_frames",1):raise BusinessError("IMAGE_FRAME_NOT_FOUND","图片帧不存在。",404)
        image.seek(frame)
        if image.width*image.height>40000000:raise BusinessError("IMAGE_PREVIEW_TOO_LARGE","图片超过4000万像素，请下载原件。",413)
        converted=image.convert("RGB");converted.thumbnail((2400,2400))
        data=io.BytesIO();converted.save(data,format="PNG")
    return Response(data.getvalue(),media_type="image/png",headers={"X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})

@router.get("/documents/{document_id}/source")
def source(document_id:str,instance:str=Header(alias="X-RAG-Data-Instance"),raw_version_id:str|None=None,
           offset:int=Query(0,ge=0),sheet:int=Query(0,ge=0),frame:int=Query(0,ge=0,le=2047)):
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False);row,path,checked=archive_for(db,document_id,raw_version_id)
        notice=reader_notice(db,document_id,row["raw_version_id"])
    kind=path.suffix.lower().lstrip(".")
    result={"document_id":document_id,"raw_version_id":row["raw_version_id"],"kind":kind,
            "display_name":row["display_name"],"offset":offset,"rows":[],"precision":"ORIGINAL_STRUCTURAL_VIEW",
            "source_notice":notice}
    result["content_url"]="/api/ui/documents/"+document_id+"/content?data_instance_id="+instance+"&raw_version_id="+row["raw_version_id"]
    result["images"]=[{"label":"已保存图片 "+str(index+1),
        "preview_url":"/api/ui/documents/"+document_id+"/resource-image/"+str(index)+
            "?data_instance_id="+instance+"&raw_version_id="+row["raw_version_id"]}
        for index,_member in enumerate(archived_image_members(checked))]
    if kind=="pdf":return result
    if kind in {"png","jpg","jpeg","webp","bmp","tif","tiff","gif","svg"}:
        from PIL import Image
        if kind=="svg":
            from .svg_image import read_svg
            read_svg(path);count=1
        else:
            with Image.open(path) as image:count=getattr(image,"n_frames",1)
        if frame>=count:raise BusinessError("IMAGE_FRAME_NOT_FOUND","图片帧不存在。",404)
        result.update(frame=frame,frame_count=count,preview_url="/api/ui/documents/"+document_id+"/image-preview?data_instance_id="+instance+"&raw_version_id="+row["raw_version_id"]+"&frame="+str(frame),precision="DERIVED_PNG_PREVIEW_ORIGINAL_DOWNLOAD")
        return result
    if path.stat().st_size>64*1024*1024: raise BusinessError("SOURCE_VIEW_TOO_LARGE","原文超过64 MiB，请下载查看。",413)
    if kind in {"csv","xlsx"}:
        if kind=="csv":
            with path.open(encoding="utf-8-sig",newline="") as stream: rows=list(csv.reader(stream))
            names=["CSV"];data=rows[offset:offset+100];count=len(rows)
        else:
            import openpyxl
            workbook=openpyxl.load_workbook(path,read_only=True,data_only=False)
            try:
                names=workbook.sheetnames
                if sheet>=len(names):raise BusinessError("SHEET_NOT_FOUND","工作表不存在。",404)
                ws=workbook[names[sheet]];count=ws.max_row or 0
                from openpyxl.utils.cell import get_column_letter
                from .parsing.formats import display_cell,xlsx_sources,formula_cells
                affected=formula_cells(xlsx_sources(path),ws.title)
                data=[];metadata=[]
                for r,cells in enumerate(ws.iter_rows(min_row=offset+1,max_row=min(offset+100,count)),offset+1) if count else []:
                    values=[];cell_metadata=[]
                    for c,cell in enumerate(cells,1):
                        coordinate=get_column_letter(c)+str(r)
                        if (r,c) in affected and cell.data_type!="f":
                            shown="[未核验公式影响单元格 "+coordinate+"]";status="FORMULA_EFFECT_UNVERIFIED"
                        elif cell.data_type=="f":
                            shown=cell.value if isinstance(cell.value,str) else getattr(cell.value,"text","[公式]")
                            status="FORMULA_TEXT_ONLY"
                        else:
                            shown,resolved=display_cell(cell)
                            status="RESOLVED" if resolved else "DISPLAY_UNRESOLVED"
                        values.append(shown)
                        cell_metadata.append({"coordinate":coordinate,"number_format":cell.number_format,
                                              "data_type":cell.data_type,"display_status":status})
                    data.append(values);metadata.append(cell_metadata)
                result["cell_metadata"]=metadata
            finally: workbook.close()
        result.update(rows=data,total=count,sheets=names,sheet=sheet,formula_policy="展示原始公式，不将缓存结果当作已核验数字")
    elif kind=="docx":
        from .docx_source import original_rows
        rows=original_rows(path)
        result.update(rows=rows[offset:offset+100],total=len(rows),precision="DOCX_XML_TEXT_NOT_PAGE_FIDELITY")
    elif kind in {"html","htm"}:
        from lxml import html
        tree=html.fromstring(path.read_bytes())
        rows=[]
        for node in tree.xpath("//p|//h1|//h2|//h3|//li|//tr|//pre"):
            text=" ".join(node.itertext()).strip()
            if text:rows.append({"xpath":tree.getroottree().getpath(node),"text":text})
        result.update(rows=rows[offset:offset+100],total=len(rows),precision="ARCHIVED_DOM_TEXT_NO_ACTIVE_HTML")
    elif kind=="json":
        value=json.loads(path.read_text(encoding="utf-8"))
        rows=[]
        def walk(item,pointer=""):
            if isinstance(item,dict):
                for key,child in item.items():walk(child,pointer+"/"+key.replace("~","~0").replace("/","~1"))
            elif isinstance(item,list):
                for index,child in enumerate(item):walk(child,pointer+"/"+str(index))
            else: rows.append({"pointer":pointer or "/","type":type(item).__name__,"value":item})
        walk(value);result.update(rows=rows[offset:offset+100],total=len(rows))
    else:
        rows=path.read_text(encoding="utf-8-sig").splitlines()
        result.update(rows=rows[offset:offset+100],total=len(rows),precision="ORIGINAL_LINE_NUMBER")
    if len(json.dumps(result,ensure_ascii=False).encode())>512*1024:
        raise BusinessError("SOURCE_PAGE_TOO_LARGE","该100行超过512 KiB，请下载原件查看。",413)
    return result
@router.get("/question-requests/{request_id}/references/{support_unit_id}")
async def reference(request_id:str,support_unit_id:str,request:Request,
                    instance:str=Header(alias="X-RAG-Data-Instance")):
    from .retrieval.support_scope import COMPOSITE_FIELDS, support_texts
    service=request.app.state.chat
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        retained=db.execute("SELECT result_json FROM chat_turns WHERE turn_id=?",(request_id,)).fetchone()
        if retained and retained[0]:
            result=json.loads(retained[0])
            for paragraph in result.get("paragraphs",[]):
                for ref in paragraph.get("references",[]):
                    if ref.get("support_unit_id")==support_unit_id and ref.get("deleted"):
                        raise BusinessError("DOCUMENT_REMOVED","资料已删除。",410)
    record=await service.db.call("read",instance,request_id)
    evidence=((record.get("trace") or {}).get("evidence") or {})
    unit=evidence.get("support_unit_whitelist",{}).get(support_unit_id)
    if unit is None:raise BusinessError("REFERENCE_NOT_FOUND","本轮没有该支持单元。",404)
    keys=("evidence_id","support_unit_id","kb_id","generation_id","document_id","raw_version_id","processing_version_id","source_record_id")
    await service.validate_references(instance,request_id,[{key:unit[key] for key in keys}])
    sources=unit["sources"]
    composite = (bool(COMPOSITE_FIELDS.intersection(unit))
                 or any("support_role" in source for source in sources)
                 or any("support_scope" in group for group in evidence.get("groups",[]) if isinstance(group,dict)))
    if composite:
        try:
            support_texts(evidence.get("groups"),evidence.get("support_unit_whitelist"),
                          kb_id=unit["kb_id"],generation_id=unit["generation_id"])
        except (ValueError,TypeError,KeyError) as exc:
            raise BusinessError("CITATION_INVALID","这条引用的正文或上下文绑定不完整；问答与归档仍保留。请从资料页核对对应版本。",409) from exc
    if len(sources)>100:
        raise BusinessError("REFERENCE_VIEW_TOO_LARGE","这条引用包含超过100处定位；答案与归档仍已保存。请在资料页查看归档原文，或缩小问题范围后重新提问。",413)
    view={"reference":{key:unit[key] for key in keys},"parent_text_interval":unit["parent_text_interval"],
          "locators":[{"field_pointer":item["field_pointer"],"locator":item["locator"],
                       "cleaned_interval":item["cleaned_interval"],"purpose":item["purpose"],
                       "support_role":item.get("support_role","BODY")} for item in sources],
          "semantic_support_proven":False}
    if "support_scope" in unit:
        view.update({key:unit[key] for key in ("support_scope","support_parts","support_text_sha256")})
        view["support_coordinate_units"]="PARENT_PYTHON_UNICODE_CODE_POINT"
    return view

@router.get("/documents/{document_id}/processing")
def processing(document_id:str):
    with closing(open_db(settings.sqlite)) as db:
        doc=db.execute("SELECT * FROM documents WHERE document_id=? AND removed_at IS NULL",(document_id,)).fetchone()
        if doc is None:raise BusinessError("DOCUMENT_NOT_FOUND","文档不存在。",404)
        if not doc["current_processing_version_id"]:raise BusinessError("PARSE_NOT_READY","尚无处理版本。",404)
        return ParsingStore(db,settings).result(doc["current_processing_version_id"])

@router.get("/documents/{document_id}/processing-view")
def processing_view(document_id:str,section:str=Query(pattern="^(quality|fields|native)$"),
                    offset:int=Query(0,ge=0),instance:str=Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        require_instance(db,instance,importing=False)
        row=db.execute("SELECT current_processing_version_id FROM documents WHERE document_id=? AND removed_at IS NULL",
                       (document_id,)).fetchone()
        if row is None or not row[0]:raise BusinessError("PARSE_NOT_READY","暂无可查看的处理版本。",404)
        checked=ParsingStore(db,settings).result(row[0])
    folder=Path(checked["bundle_path"]).parent
    related=[]
    if section=="native":
        value=json.loads((folder/"cleaned.native.json").read_bytes())
        items=[{"key":key,"type":type(item).__name__,"value":item} for key,item in value.items()]
    else:
        value=json.loads((folder/"supplement.json").read_bytes())
        if section=="quality":
            items=value["quality"]
            pointers={item.get("field") for item in items[offset:offset+100] if isinstance(item,dict) and isinstance(item.get("field"),str)}
            related=[{key:item[key] for key in ("field_pointer","original_text","cleaned_text","locator")}
                     for item in value["fields"] if item["field_pointer"] in pointers]
        else:items=[{key:item[key] for key in ("field_pointer","original_text","cleaned_text","locator")} for item in value["fields"]]
    omitted=[];page=[bounded(item,omitted) for item in items[offset:offset+100]]
    related=[bounded(item,omitted) for item in related]
    if len(json.dumps({"items":page,"related_fields":related},ensure_ascii=False).encode())>512*1024:
        raise BusinessError("PROCESSING_VIEW_TOO_LARGE","当前问题内容较多，暂时无法完整展示。归档原文仍可查看，请保留问题并稍后处理。",413)
    return {"section":section,"offset":offset,"total":len(items),"items":page,"related_fields":related,"omitted_paths":omitted}

@router.get("/maintenance")
def maintenance():
    import psutil
    state_path=settings.run_state
    recorded=json.loads(state_path.read_text()) if state_path.is_file() else {}
    services=[]
    for name,item in recorded.get("services",{}).items():
        try:
            process=psutil.Process(item["pid"])
            running=process.is_running() and process_start_time(process)==item["created_at"]
        except (psutil.Error,KeyError):running=False
        services.append({"name":name,"pid":item.get("pid"),"running":running})
    logs=[path.name for path in sorted(settings.logs.glob("*.log")) if path.is_file() and not path.is_symlink()]
    return {"home":str(settings.home),"python":str(settings.python),"services":services,"logs":logs,
            "compose_recorded":recorded.get("compose"),"maintenance_module":"raghub.maintenance",
            "manager_module":"raghub.manager","restoration_policy":"INDEPENDENT_ROOT_MANUAL_CONFIRM_NO_PAID_REPLAY"}

@router.get("/logs/{name}")
def log(name:str):
    if name!=Path(name).name or not name.endswith(".log"):raise BusinessError("LOG_NOT_ALLOWED","日志名称不允许。",422)
    path=settings.logs/name
    if not path.is_file() or path.is_symlink():raise BusinessError("LOG_NOT_FOUND","日志不存在。",404)
    with path.open("rb") as stream:
        stream.seek(max(0,path.stat().st_size-256*1024))
        raw=stream.read(256*1024)
    lines=raw.decode("utf-8",errors="replace").splitlines()
    return {"name":name,"lines":lines[-100:],"view":"最后100行，最多256 KiB；显示视图不替代原始日志"}
