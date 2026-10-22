"""Single-video Obsidian intake, using the existing DY tool and task outbox."""
from __future__ import annotations
import hashlib
import json
import re
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Header, Query
from pydantic import Field, UUID4
from .contracts import StrictRecord
from .db import IdentityStore, new_id, now, open_db, transaction
from .ingestion.archive import hash_file, inside, json_hash
from .ingestion.store import BusinessError, require_instance

OPERATION = "DOUYIN_NOTE"
STAGES = {
    "PENDING": "等待处理", "DY_PLANNING": "正在查找已有笔记",
    "DY_PREPARING": "正在获取作品并识别内容", "DY_IMAGES_PREPARING": "正在下载图片并识别文字", "DY_ANALYSING": "正在整理知识笔记",
    "DY_SAVING": "正在保存到 Obsidian", "DY_DONE": "已保存，等待预览确认",
}


def object_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def share_url(value):
    urls = re.findall(r"https?://[^\s<>\"，。、]+", value)
    if len(urls) != 1:
        raise BusinessError("DY_ONE_VIDEO_REQUIRED", "请粘贴一条抖音视频链接或完整分享口令；本入口一次处理一个视频。", 422)
    url = urls[0].rstrip("）)]，。、;；")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BusinessError("DY_URL_UNSUPPORTED", "链接格式不完整，请重新粘贴抖音官方分享链接。", 422) from exc
    if (parsed.scheme != "https" or parsed.username or parsed.password or port
            or parsed.query or parsed.fragment):
        raise BusinessError("DY_URL_UNSUPPORTED", "请使用抖音官方 HTTPS 视频链接或短链接。", 422)
    if parsed.hostname == "v.douyin.com":
        valid = re.fullmatch(r"/[A-Za-z0-9_-]{3,96}/?", parsed.path)
    elif parsed.hostname in {"www.douyin.com", "douyin.com"}:
        valid = re.fullmatch(r"/(?:video|note)/[0-9]{16,22}/?", parsed.path)
    else:
        valid = None
    if not valid:
        raise BusinessError("DY_URL_UNSUPPORTED", "请使用单条抖音视频、图文或官方短链接；不导入作者主页或收藏夹。", 422)
    return "https://" + parsed.hostname + parsed.path


def tool_snapshot(settings):
    path = settings.project / "configs/douyin.json"
    if not path.is_file():
        raise BusinessError("DY_TOOL_UNAVAILABLE", "抖音导入工具尚未连接；其他资料导入仍可使用。", 503)
    value = object_json(path.read_bytes())
    if set(value) != {"schema_version", "python", "script", "config"} or value["schema_version"] != 1:
        raise BusinessError("DY_TOOL_UNAVAILABLE", "抖音导入工具的配置需要维护，其他导入入口不受影响。", 503)
    result = {}
    for key in ("python", "script", "config"):
        member = Path(value[key])
        if not member.is_absolute() or not member.is_file():
            raise BusinessError("DY_TOOL_UNAVAILABLE", "抖音导入工具未准备好，其他资料导入仍可使用。", 503)
        result[key] = str(member)
    result["script_sha256"] = hash_file(Path(result["script"]))[0]
    result["config_sha256"] = hash_file(Path(result["config"]))[0]
    config = object_json(Path(result["config"]).read_bytes())
    for key in ("vault", "notes_dir", "cache_dir"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise BusinessError("DY_TOOL_UNAVAILABLE", "抖音工具的保存目录尚未配置完整。", 503)
    if config.get("auto_browser_recovery") is not False or config.get("prefer_browser_media") is not False:
        raise BusinessError("DY_TOOL_UNAVAILABLE", "抖音导入需要关闭自动打开浏览器；本次未创建任务。", 503)
    vault, cache = Path(config["vault"]).resolve(strict=True), Path(config["cache_dir"]).resolve(strict=True)
    notes = vault / config["notes_dir"]
    if not vault.is_dir() or not notes.is_dir() or not cache.is_dir():
        raise BusinessError("DY_TOOL_UNAVAILABLE", "Obsidian 保存目录或抖音缓存目录不可用。", 503)
    inside(notes, vault)
    root = config.get("unified_root_vault")
    if config.get("unified_only") is not True or not isinstance(root, str) or Path(root).resolve() != vault:
        raise BusinessError("DY_TOOL_UNAVAILABLE", "抖音保存目标尚未配置完整；请先核对 Obsidian 的统一保存目录，本次未创建处理任务。", 503)
    if any(member.is_symlink() or not member.is_file() for member in
           (vault / "知识总索引.md", vault / "导入总日志.md")):
        raise BusinessError("DY_SAVE_NOT_READY", "Obsidian 的知识总索引或导入总日志尚未准备好。请先完成保存目录的初始化，再继续原提交；本次未创建视频任务或调用模型。", 503)
    result.update(vault=str(vault), notes_root=str(notes.resolve(strict=True)), cache_dir=str(cache))
    return result


def _payload(db, instance, task_id):
    require_instance(db, instance, importing=False)
    row = db.execute("SELECT t.*,i.payload_json FROM tasks t JOIN generation_inputs i USING(task_id) "
                     "WHERE t.task_id=? AND t.data_instance_id=?", (task_id, instance)).fetchone()
    if row is None:
        raise BusinessError("DY_TASK_NOT_FOUND", "没有找到本次视频处理记录，请核对原实例。", 404)
    value = object_json(row["payload_json"])
    if value.get("operation") != OPERATION:
        raise BusinessError("DY_TASK_NOT_FOUND", "该记录不是抖音视频处理任务。", 404)
    return row, value


def reconciled_saved_note(db, settings, row, payload):
    """Read the original terminal run; never replay a command or change task history."""
    if row["status"] not in {"FAILED","CANCELLED","INTERRUPTED"}:
        return None
    attempts=db.execute("SELECT attempt_id,directory_path,state FROM attempts WHERE task_id=? AND directory_path IS NOT NULL",
                        (row["task_id"],)).fetchall()
    if len(attempts)!=1 or attempts[0]["state"] not in {"FAILED","CANCELLED","INTERRUPTED"}:
        return None
    def read_object(path):
        raw=path.read_bytes()
        if len(raw)>2*1024*1024:
            raise ValueError("retained receipt too large")
        return object_json(raw)
    try:
        attempt=attempts[0]
        directory=settings.home/"data/douyin"/row["task_id"]/attempt["attempt_id"]
        if Path(attempt["directory_path"]).resolve(strict=True)!=directory.resolve(strict=True):
            return None
        inside(directory,settings.home/"data/douyin")
        if read_object(directory/"input.json")!=payload:
            return None
        exited=read_object(directory/"03-commit/exit.json")
        if (type(exited.get("exit_code")) is not int or not 0<=exited["exit_code"]<=255
                or exited.get("child_stream_split_observed") is not True
                or (directory/"03-commit/stopped.json").exists()):
            return None
        tool=payload["tool"]
        for name in ("script","config"):
            if hash_file(Path(tool[name]))[0]!=tool[name+"_sha256"]:
                return None
        command=read_object(directory/"00-init/request.json")
        if command.get("argv")!=[tool["python"],"-B",tool["script"],"--config",tool["config"],"init"]:
            return None
        initialized=read_object(directory/"00-init/child-stdout.bin")
        run=Path(initialized["run_dir"]).resolve(strict=True)
        if run.parent!=(Path(tool["cache_dir"])/"dy-runs").resolve(strict=True):
            return None
        if initialized["state_file"]!=str(run/"state.json") or initialized["input_file"]!=str(run/"input.txt"):
            return None
        original_input=(run/"input.txt").read_bytes()
        state=read_object(run/"state.json")
        if original_input!=payload["url"].encode("utf-8") or state["input_sha256"]!=hashlib.sha256(original_input).hexdigest():
            return None
        items=state["items"]
        if state["mode"]!="videos" or not isinstance(items,list) or len(items)!=1:
            return None
        item=items[0]
        if item["status"] not in {"saved","exists"} or item["source_url"]!=payload["url"]:
            return None
        if not isinstance(item["id"],str) or not re.fullmatch(r"[0-9]{16,22}",item["id"]):
            return None
        from .douyin_worker import _saved_note
        saved=_saved_note(item["note"],item["id"],tool,item["status"]=="saved")
        if saved["warnings"]:
            return None
        return {**saved,"reconciled_from_original_run":True}
    except (OSError,ValueError,KeyError,TypeError,BusinessError):
        return None



def material_failure_message(failure):
    """Turn the retained acquisition outcome into a bounded, user-facing cause."""
    reason = failure.get("reason", "") if isinstance(failure, dict) else ""
    reason = reason if isinstance(reason, str) else ""
    lowered = reason.lower()
    if isinstance(failure, dict) and failure.get("stage") == "detail" and "未取得匹配媒体" in reason:
        return "抖音没有返回这条作品的正文图片或视频，当前无法导入。原链接与获取记录已保留，尚未生成新笔记。可换一条能公开访问的作品，或将原图保存后从本地文件导入。"
    if "hf_hub_offline" in lowered or "outgoing traffic has been disabled" in lowered:
        return "语音识别未完成：处理进程没有找到语音模型，并且处于离线模式，无法下载模型。尚未生成或保存新笔记。"
    if "snapshot" in lowered and ("not found" in lowered or "cannot find" in lowered):
        return "语音识别未完成：本机没有找到所需的语音模型。尚未生成或保存新笔记。"
    if any(word in lowered for word in ("captcha", "验证码", "登录", "login", "private", "作品不存在", "已删除")):
        return "无法取得视频内容：抖音要求验证、登录，或该作品已不可访问。本入口不会自动打开抖音页面；请确认链接可以公开访问。尚未生成新笔记。"
    if any(word in lowered for word in ("timed out", "timeout", "超时", "connection", "网络", "download")):
        return "视频材料获取失败：网络连接或下载未完成。已有获取记录保留，尚未生成新笔记；网络恢复后可重新保存这条视频。"
    stage = failure.get("stage") if isinstance(failure, dict) else None
    step = {"detail": "读取作品信息", "download": "下载完整音轨", "transcribe": "识别视频语音",
            "asr": "识别视频语音", "extract": "提取作品内容", "ocr": "识别图片文字",
            "images": "下载全部图片", "evidence": "核对图片完整性"}.get(stage, "获取完整作品材料")
    # Preserve a plain tool explanation, but never expose tracebacks, paths or code.
    clean = " ".join(reason.split())
    if clean and len(clean) <= 240 and not any(token in clean for token in ("Traceback", "/Users/", "{", "}", "File ", "token", "cookie")):
        return step + "失败：" + clean + "。尚未生成或保存新笔记。"
    return step + "失败：视频工具没有提供可用的完整讲解。获取记录已保留，尚未生成新笔记。"


def retained_material_failure(db, settings, row, payload):
    """Explain the old error-handler failure using only this task's original receipt."""
    if row["status"] != "FAILED":
        return None
    attempts = db.execute("SELECT attempt_id,directory_path FROM attempts WHERE task_id=? AND directory_path IS NOT NULL",
                          (row["task_id"],)).fetchall()
    if len(attempts) != 1:
        return None
    def read_object(path):
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("receipt too large")
        return object_json(path.read_bytes())
    try:
        attempt = attempts[0]
        directory = settings.home / "data/douyin" / row["task_id"] / attempt["attempt_id"]
        if Path(attempt["directory_path"]).resolve(strict=True) != directory.resolve(strict=True):
            return None
        inside(directory, settings.home / "data/douyin")
        if read_object(directory / "input.json") != payload:
            return None
        tool = payload["tool"]
        command = read_object(directory / "00-init/request.json")
        if command.get("argv") != [tool["python"], "-B", tool["script"], "--config", tool["config"], "init"]:
            return None
        initialized = read_object(directory / "00-init/child-stdout.bin")
        run = Path(initialized["run_dir"]).resolve(strict=True)
        if run.parent != (Path(tool["cache_dir"]) / "dy-runs").resolve(strict=True):
            return None
        if (run / "input.txt").read_bytes() != payload["url"].encode("utf-8"):
            return None
        expected = [tool["python"], "-B", tool["script"], "--config", tool["config"], "prepare-all", "--run", str(run)]
        if read_object(directory / "02-prepare-all/request.json").get("argv") != expected:
            return None
        exited = read_object(directory / "02-prepare-all/exit.json")
        if type(exited.get("exit_code")) is not int or exited["exit_code"] != 0 or (directory / "02-prepare-all/stopped.json").exists():
            return None
        prepared = read_object(directory / "02-prepare-all/child-stdout.bin")
        failures = prepared.get("failures")
        if prepared.get("run_dir") != str(run) or prepared.get("ready") != [] or not isinstance(failures, list) or len(failures) != 1:
            return None
        failure = failures[0]
        video_id = failure.get("id")
        if (not isinstance(video_id, str) or not re.fullmatch(r"[0-9]{16,22}", video_id)
                or failure.get("source_url") != payload["url"]
                or failure.get("url") != "https://www.douyin.com/video/" + video_id):
            return None
        return material_failure_message(failure)
    except (OSError, ValueError, KeyError, TypeError, BusinessError):
        return None

def receipt(db, settings, instance, task_id, vault_root):
    row, payload = _payload(db, instance, task_id)
    result = {"task_id": task_id, "status": row["status"], "stage": row["stage"],
              "progress": STAGES.get(row["stage"], "正在核对处理状态"),
              "kb_id": payload["kb_id"], "source_url": payload["url"], "saved": False, "result_unknown": False}
    if row["status"] in {"FAILED", "CANCELLED"}:
        result["progress"] = "处理已停止" if row["status"] == "FAILED" else "本次处理已取消"
    if row["last_error_json"]:
        error = object_json(row["last_error_json"])
        recovered = (retained_material_failure(db, settings, row, payload)
                     if error.get("error_type") == "TypeError" and "unexpected keyword argument 'details'" in error.get("message", "")
                     else None)
        result["message"] = (recovered or error["message"] if error.get("code", "").startswith("DY_") or recovered
                             else "视频处理程序发生错误，未能完成本次任务。错误记录已保留，需要修复程序后再继续。")
        result["result_unknown"] = error.get("code") == "DY_RESULT_UNKNOWN"
        if result["result_unknown"]:
            result["message"] = "暂时无法确认笔记是否已保存。系统正在自动核对原任务，确认前不会重复生成或保存；你可以先关闭此窗口。"
    artifact = db.execute("SELECT * FROM stage_artifacts WHERE task_id=? AND stage='DY_NOTE_RESULT' "
                          "AND adopted=1 ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
    saved = None
    if artifact:
        path = settings.home / artifact["relative_path"]
        inside(path, settings.home)
        raw = path.read_bytes()
        if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise BusinessError("DY_RESULT_UNAVAILABLE", "笔记保存记录暂时无法核对；原记录保留，请先重新读取状态。")
        saved = object_json(raw)
    elif result["result_unknown"]:
        saved = reconciled_saved_note(db,settings,row,payload)
        if saved:
            result.update(result_unknown=False,reconciled_from_original_run=True,
                message="原任务的保存结果曾未确认；现已核对原运行记录、笔记、总索引与日志，笔记确已保存。原失败记录保留，没有重新生成或保存。")
    if saved:
        result["result_unknown"] = False
        try:
            note = Path(saved["note"]).resolve(strict=True)
        except OSError:
            result.update(saved=True, written=saved["written"], title=saved["title"],
                          preview_available=False, relative_path=None,
                          message="笔记曾保存到 Obsidian，但当前本地文件缺失。处理记录保留，请核对文件，不会自动重写。")
            return result
        allowed = Path(payload["tool"]["notes_root"])
        inside(note, allowed)
        relative = None
        try:
            relative = note.relative_to(Path(vault_root).resolve(strict=True)).as_posix()
        except ValueError:
            pass
        result.update(media_kind=saved.get("media_kind", "video"), image_count=saved.get("image_count"),
                      recognition_engine=saved.get("recognition_engine"))
        result.update(saved=True, written=saved["written"], title=saved["title"],
                      relative_path=relative, preview_available=relative is not None,
                      warnings=saved["warnings"], note_changed=hash_file(note)[0] != saved["note_sha256"])
        if relative is None:
            result["message"] = "笔记已保存在 Obsidian；当前验证实例绑定了其他笔记目录，暂不能在这里预览入库。"
    return result


class VideoRequest(StrictRecord):
    kb_id: UUID4
    share_text: str = Field(min_length=1, max_length=8192)


def create_router(settings, vault):
    router = APIRouter(prefix="/douyin")

    @router.post("/jobs")
    def submit(body: VideoRequest, instance: str = Header(alias="X-RAG-Data-Instance"),
               request_key: UUID4 = Header(alias="X-Request-ID")):
        url = share_url(body.share_text)
        tool = tool_snapshot(settings)
        payload = {"schema_version": 1, "operation": OPERATION, "kb_id": str(body.kb_id),
                   "url": url, "tool": tool, "note_policy": "DY_KNOWLEDGE_NOTE_ONCE_V1"}
        digest = json_hash(payload)
        with closing(open_db(settings.sqlite)) as db, transaction(db):
            require_instance(db, instance)
            if db.execute("SELECT 1 FROM knowledge_bases WHERE kb_id=?", (str(body.kb_id),)).fetchone() is None:
                raise BusinessError("KB_NOT_FOUND", "请选择已有知识库。", 404)
            prior = db.execute("SELECT task_id,request_sha256 FROM tasks WHERE data_instance_id=? AND request_key=?",
                               (instance, str(request_key))).fetchone()
            if prior:
                if prior["request_sha256"] != digest:
                    raise BusinessError("REQUEST_KEY_CONFLICT", "原请求内容已改变，请先核对原记录，不能覆盖提交。")
                return receipt(db, settings, instance, prior["task_id"], vault.root)
            active = db.execute("SELECT 1 FROM import_workflows WHERE data_instance_id=? AND state NOT IN "
                                "('COMPLETE','PARTIAL_COMPLETE','CANCELLED') LIMIT 1", (instance,)).fetchone()
            if active:
                raise BusinessError("IMPORT_WORKFLOW_BUSY", "请先处理或结束当前导入，再保存抖音笔记。")
            same = db.execute("SELECT t.task_id,t.status FROM tasks t JOIN generation_inputs i USING(task_id) "
                              "WHERE t.data_instance_id=? AND json_extract(i.payload_json,'$.operation')=? "
                              "AND (t.status IN ('PENDING','RUNNING') OR (json_extract(i.payload_json,'$.url')=? "
                              "AND EXISTS (SELECT 1 FROM provider_attempts p WHERE p.task_id=t.task_id AND p.state IN "
                              "('REGISTERED','SENT','UNKNOWN')))) LIMIT 1", (instance, OPERATION, url)).fetchone()
            if same is None:
                uncertain=db.execute("SELECT t.task_id FROM tasks t JOIN generation_inputs i USING(task_id) WHERE "
                    "t.data_instance_id=? AND json_extract(i.payload_json,\'$.operation\')=? AND "
                    "json_extract(i.payload_json,\'$.url\')=? AND json_extract(t.last_error_json,\'$.code\')=\'DY_RESULT_UNKNOWN\'",
                    (instance,OPERATION,url)).fetchall()
                for previous in uncertain:
                    checked=receipt(db,settings,instance,previous["task_id"],vault.root)
                    if not checked["saved"] or checked["result_unknown"]:
                        same=previous
                        break
            if same:
                raise BusinessError("DY_PRIOR_TASK_PENDING", "已有视频任务尚未结束或结果待核对；请先读取原状态，不重复提交。")
            # Task, attempt, input and outbox share this business transaction.
            task_id, attempt_id, outbox_id = new_id(), new_id(), new_id()
            stamp=now()
            db.execute("INSERT INTO tasks(task_id,data_instance_id,kind,request_key,request_sha256,status,current_attempt_id,"
                       "stage_budget_scope_id,created_at,updated_at) VALUES (?,?,\'DOCUMENT\',?,?,\'PENDING\',NULL,?,?,?)",
                       (task_id,instance,str(request_key),digest,new_id(),stamp,stamp))
            db.execute("INSERT INTO attempts(attempt_id,task_id,data_instance_id,state,created_at) VALUES (?,?,?,\'PENDING\',?)",
                       (attempt_id,task_id,instance,stamp))
            db.execute("UPDATE tasks SET current_attempt_id=? WHERE task_id=?",(attempt_id,task_id))
            db.execute("INSERT INTO outbox(outbox_id,task_id,attempt_id,data_instance_id,state,created_at) VALUES (?,?,?,?,\'PENDING\',?)",
                       (outbox_id,task_id,attempt_id,instance,stamp))
            db.execute("INSERT INTO generation_inputs(task_id,kb_id,payload_json,input_sha256,created_at) VALUES (?,?,?,?,?)",
                       (task_id, str(body.kb_id), json.dumps(payload, ensure_ascii=False, sort_keys=True), digest, now()))
            return receipt(db, settings, instance, task_id, vault.root)

    @router.get("/by-request/{request_key}")
    def by_request(request_key: UUID4, kb_id: UUID4, instance: str = Header(alias="X-RAG-Data-Instance")):
        with closing(open_db(settings.sqlite)) as db:
            require_instance(db, instance, importing=False)
            row = db.execute("SELECT t.task_id FROM tasks t JOIN generation_inputs i USING(task_id) WHERE "
                             "t.data_instance_id=? AND t.request_key=? AND i.kb_id=? AND "
                             "json_extract(i.payload_json,'$.operation')=?", (instance, str(request_key), str(kb_id), OPERATION)).fetchone()
            return {"task": receipt(db, settings, instance, row["task_id"], vault.root) if row else None}

    @router.get("/jobs/{task_id}")
    def status(task_id: UUID4, kb_id: UUID4, instance: str = Header(alias="X-RAG-Data-Instance")):
        with closing(open_db(settings.sqlite)) as db:
            value=receipt(db, settings, instance, str(task_id), vault.root)
            if value["kb_id"]!=str(kb_id):
                raise BusinessError("DY_TASK_NOT_FOUND","这条视频处理记录不属于当前知识库。",404)
            return value

    return router
