"""Stopped-instance backup and independent-root recovery; no paid calls."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import traceback
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from .config import Settings
from .db import IdentityStore, new_id, now, open_db, system_state, transaction, verify_schema
from .ingestion.archive import verify_archive, json_hash
from .ingestion.store import IngestionStore
from .manager import manager_lock, read_state, observe_compose, ROLES
from .process_locks import role_status

def digest(path):
    value=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):value.update(chunk)
    return value.hexdigest()

def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("x",encoding="utf-8") as stream:
        json.dump(value,stream,ensure_ascii=False,sort_keys=True,allow_nan=False,indent=2)
        stream.write("\n");stream.flush();os.fsync(stream.fileno())


def save_recovery_record(path,value,*,create=False):
    # Persist the marker before any business copy; later updates are atomic.
    if create:
        write_json(path,value)
    else:
        staged=path.with_name(path.name+"."+new_id()+".tmp")
        write_json(staged,value)
        os.replace(staged,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def stopped(settings):
    state=read_state(settings)
    if state["services"] or state["compose"]["phase"]!="STOPPED":raise RuntimeError("先使用统一 manager stop 停止服务。")
    if any(role_status(settings,name)["held"] for name in ROLES):raise RuntimeError("仍有服务角色锁，不能备份。")
    compose=observe_compose(settings)
    if compose["status"]!="OBSERVED" or compose["containers"]:raise RuntimeError("Compose 尚未确认停止。")

def members(root,exclude=()):
    values=[]
    for folder,dirs,files in os.walk(root,followlinks=False):
        directory=Path(folder)
        dirs[:]=[name for name in sorted(dirs) if name not in {"node_modules",".git","__pycache__"}]
        for name in dirs:
            path=directory/name
            if path.is_symlink():raise RuntimeError("不复制符号链接目录："+str(path))
        for name in sorted(files):
            path=directory/name;relative=path.relative_to(root).as_posix()
            if any(relative==item or relative.startswith(item+"/") for item in exclude):continue
            if path.is_symlink() or not path.is_file():raise RuntimeError("不复制非普通文件："+str(path))
            values.append((relative,path))
    if len(values)>100000:raise RuntimeError("备份成员超过10万项，停止并保留原实例。")
    return values

def backup(settings):
    with manager_lock(settings):
        stopped(settings)
        data=members(settings.home/"data",("services/redis","sqlite/rag.sqlite3","sqlite/rag.sqlite3-wal","sqlite/rag.sqlite3-shm"))
        project=members(settings.project)
        total=sum(path.stat().st_size for _,path in data+project)+settings.sqlite.stat().st_size
        if shutil.disk_usage(settings.home).free<total+1024**3:raise RuntimeError("备份空间不足。")
        target=settings.home/"backups"/("backup-"+new_id());target.mkdir(parents=True,exist_ok=False)
        inventory=[]
        for prefix,entries in (("data",data),("project",project)):
            for relative,path in entries:
                dest=target/prefix/relative;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
                inventory.append({"path":prefix+"/"+relative,"size_bytes":dest.stat().st_size,"sha256":digest(dest)})
        dest=target/"data/sqlite/rag.sqlite3";dest.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(str(settings.sqlite))) as src,closing(sqlite3.connect(str(dest))) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchall()!=[("ok",)]:raise RuntimeError("SQLite 备份完整性失败。")
            instance=dst.execute("SELECT data_instance_id FROM system_state").fetchone()[0]
        inventory.append({"path":"data/sqlite/rag.sqlite3","size_bytes":dest.stat().st_size,"sha256":digest(dest)})
        value={"schema":"raghub.backup.v1","source_home":str(settings.home),"data_instance_id":instance,
               "created_at":now(),"member_count":len(inventory),"files":sorted(inventory,key=lambda item:item["path"]),
               "index_strategy":"STOPPED_PHYSICAL_ETCD_MINIO_MILVUS_COPY","redis_policy":"EMPTY_ON_RESTORE",
               "runtime_policy":"SAME_INSTALLED_PYTHON_AND_LOCAL_MODELS"}
        write_json(target/"backup.json",value)
        return {"backup_path":str(target),"manifest_sha256":digest(target/"backup.json"),"member_count":len(inventory)}

def rebase(value,old,new,instance):
    if isinstance(value,list):return [rebase(item,old,new,instance) for item in value]
    if not isinstance(value,dict):return value
    output={}
    for key,item in value.items():
        if key=="data_instance_id" and isinstance(item,str):output[key]=instance
        elif isinstance(item,str) and (key=="path" or key.endswith("_path") or key in {"absolute_root","home","project"}) and item.startswith(old+"/"):
            output[key]=new+item[len(old):]
        else:output[key]=rebase(item,old,new,instance)
    return output

def update_file_bindings(value,root):
    if isinstance(value,list):return [update_file_bindings(item,root) for item in value]
    if not isinstance(value,dict):return value
    output={key:update_file_bindings(item,root) for key,item in value.items()}
    for key,item in list(output.items()):
        if isinstance(item,str) and (key=="path" or key.endswith("_path")) and item.startswith(str(root)+"/"):
            path=Path(item)
            if path.is_file():
                partner="sha256" if key=="path" else key[:-5]+"_sha256"
                if partner in output:
                    output[partner]=digest(path)
                    # Manifest locators do not own aggregate archive size_bytes.
                    if key=="path" and "size_bytes" in output:output["size_bytes"]=path.stat().st_size
    return output

def replace_json(path,value):
    # These are copied recovery bytes in an unopened root; backup/original bytes are preserved.
    with path.open("w",encoding="utf-8") as stream:
        json.dump(value,stream,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
        stream.write("\n")

def relocate_artifacts(root,old,instance):
    data=root/"data"
    paths=[]
    for relative,path in members(data):
        if relative.startswith(("archives/","services/","sqlite/")):continue
        if path.suffix not in {".json",".jsonl"}:continue
        if path.stat().st_size>128*1024*1024:raise RuntimeError("恢复 JSON 超过128 MiB，保持 VERIFYING。")
        paths.append(path)
        if path.suffix==".json":
            value=json.loads(path.read_bytes());mapped=rebase(value,old,str(root),instance)
            if mapped!=value:replace_json(path,mapped)
        else:
            lines=[]
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    value=json.loads(line);mapped=rebase(value,old,str(root),instance)
                    lines.append(json.dumps(mapped,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n")
            with path.open("w",encoding="utf-8") as stream:stream.writelines(lines)
    # Leaf files, parsing bundles, chunk manifests, then generation roots.
    order=sorted([path for path in paths if path.suffix==".json"],key=lambda path:
                 (3 if "generations" in path.parts else 2 if "chunks" in path.parts else
                  1 if path.name=="bundle-manifest.json" else 0,str(path)))
    for path in order:
        value=update_file_bindings(json.loads(path.read_bytes()),root)
        if isinstance(value,dict) and isinstance(value.get("files"),list):
            for member in value["files"]:
                if isinstance(member,dict) and isinstance(member.get("path"),str):
                    target=path.parent/member["path"]
                    if target.is_file() and "sha256" in member:
                        member["sha256"]=digest(target);member["size_bytes"]=target.stat().st_size
        if isinstance(value,dict) and "input" in value and "input_sha256" in value:value["input_sha256"]=json_hash(value["input"])
        replace_json(path,value)

def relocate_database(db,root,old,instance):
    native=db.core.connection.driver_connection
    tables=[row[0] for row in native.execute("SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE ?",("table","sqlite_%"))]
    with transaction(db):
        for table in tables:
            if table in {"schema_migrations","alembic_version"}:continue
            info=native.execute("PRAGMA table_info("+table+")").fetchall()
            columns=[row[1] for row in info];primary=[row[1] for row in info if row[5]]
            if not primary:continue
            for raw in native.execute("SELECT * FROM "+table).fetchall():
                value=dict(zip(columns,raw));changes={}
                for key,item in value.items():
                    if not isinstance(item,str):continue
                    if key.endswith("_json"):
                        parsed=json.loads(item);mapped=update_file_bindings(rebase(parsed,old,str(root),instance),root)
                        if mapped!=parsed:changes[key]=json.dumps(mapped,ensure_ascii=False,sort_keys=True,separators=(",",":"))
                    elif key.endswith("_path") and item.startswith(old+"/"):changes[key]=str(root)+item[len(old):]
                updated={**value,**changes}
                for key,item in updated.items():
                    if isinstance(item,str) and key.endswith("_path") and item.startswith(str(root)+"/") and Path(item).is_file():
                        partner=key[:-5]+"_sha256"
                        if partner in updated:changes[partner]=digest(Path(item))
                if table=="generations" and updated.get("manifest_path") and Path(updated["manifest_path"]).is_file():
                    changes["input_sha256"]=json.loads(Path(updated["manifest_path"]).read_bytes())["input_sha256"]
                if table in {"chat_conversations","chat_requests","retrieval_requests","generation_readers"}:
                    changes["data_instance_id"]=instance
                if table=="retrieval_requests" and updated["state"] in {"RUNNING","CANCELLING"}:
                    changes.update(state="INTERRUPTED",finished_at=now(),error_json=json.dumps({"code":"RESTORE_INTERRUPTED","message":"恢复后旧问答不自动继续。"}))
                if changes:
                    names=list(changes);sql="UPDATE "+table+" SET "+",".join(name+"=?" for name in names)+" WHERE "+" AND ".join(key+"=?" for key in primary)
                    db.execute(sql,tuple(changes[name] for name in names)+tuple(value[key] for key in primary))
        db.execute("DELETE FROM worker_current")
        db.execute("UPDATE worker_sessions SET state=?,active_task_id=NULL",("STOPPED",))
        db.execute("UPDATE generation_readers SET state=? WHERE state!=?",("CLOSED","CLOSED"))

def restore(settings,backup_path,target):
    backup_path=Path(backup_path);target=Path(target)
    if not backup_path.is_absolute() or not target.is_absolute() or target.exists() or target.is_symlink():raise RuntimeError("恢复须使用全新绝对目录。")
    if target.parent.resolve()!=target.parent:raise RuntimeError("恢复父目录不能含路径别名。")
    manifest=json.loads((backup_path/"backup.json").read_bytes())
    if manifest.get("schema")!="raghub.backup.v1" or len(manifest["files"])!=manifest["member_count"]:raise RuntimeError("备份清单无效。")
    for item in manifest["files"]:
        relative=Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] not in {"data","project"}:raise RuntimeError("备份成员路径越界或试图覆盖恢复状态。")
        path=backup_path/relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=item["size_bytes"] or digest(path)!=item["sha256"]:raise RuntimeError("备份成员缺失或改变："+str(relative))
    if shutil.disk_usage(target.parent).free<sum(item["size_bytes"] for item in manifest["files"])+1024**3:raise RuntimeError("恢复空间不足。")
    target.mkdir(exist_ok=False)
    recovery_id=new_id()
    marker=target/"recovery.json"
    record={"schema":"raghub.recovery.v1","backup_path":str(backup_path),"backup_sha256":digest(backup_path/"backup.json"),
            "old_home":manifest["source_home"],"new_home":str(target),"old_data_instance_id":manifest["data_instance_id"],
            "data_instance_id":None,"recovery_id":recovery_id,"phase":"COPYING","redis_policy":"EMPTY",
            "paid_replay":False,"runtime_resource_policy":"SAME_INSTALLED_ENVIRONMENT_AND_HARDLINKED_MODELS"}
    save_recovery_record(marker,record,create=True)
    stage="COPY_AND_LOCAL_RESOURCES"
    try:
        copied_modes=[]
        for item in manifest["files"]:
            source=backup_path/item["path"];dest=target/item["path"];dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
            original_mode=dest.stat().st_mode & 0o7777
            copied_modes.append((dest,original_mode))
            # Only the unopened recovery copy is writable while paths and bindings are rebuilt.
            dest.chmod(original_mode | 0o200)
        for name in ("logs","tmp","data/services/redis","cache/xdg","runtime"): (target/name).mkdir(parents=True,exist_ok=True)
        # Reuse the single installed environment and immutable model resources on this host.
        (target/"runtime/python").symlink_to(settings.home/"runtime/python",target_is_directory=True)
        shutil.copytree(settings.home/"models",target/"models",copy_function=os.link)
        recovered=replace(settings,home=target,project=target/"project",sqlite=target/"data/sqlite/rag.sqlite3",
                          python=target/"runtime/python/bin/python",compose=target/"project/configs/services/compose.yaml",
                          logs=target/"logs",run_state=target/"tmp/services.json",
                          model_embedding=target/"models/embedding/bge-m3",model_reranker=target/"models/reranker/bge-reranker-v2-m3")
        stage="TOKENIZER_PREPARATION"
        # This lock is a private project copy, never a hardlinked model resource.
        from .retrieval.tokenizer import LOCK_KEYS, SCHEMA
        tokenizer_lock=target/"project/configs/services/deepseek-tokenizer.lock.json"
        tokenizer_before=digest(tokenizer_lock)
        tokenizer_value=json.loads(tokenizer_lock.read_bytes())
        if set(tokenizer_value)!=LOCK_KEYS or tokenizer_value["schema"]!=SCHEMA:raise RuntimeError("恢复 tokenizer lock schema 不一致。")
        old_models=Path(manifest["source_home"])/"models"
        if tokenizer_value["directory"]!=str(old_models/"tokenizer/deepseek-v4-20261002"):raise RuntimeError("恢复 tokenizer 原目录不一致。")
        archive=tokenizer_value["archive"]
        if type(archive) is not dict or set(archive)!={"path","size_bytes","sha256"}:raise RuntimeError("恢复 tokenizer archive schema 不一致。")
        archive_relative=Path(archive["path"]).relative_to(old_models)
        if archive_relative.is_absolute() or ".." in archive_relative.parts:raise RuntimeError("恢复 tokenizer archive 越界。")
        archive["path"]=str(target/"models"/archive_relative)
        tokenizer_value["directory"]=str(target/"models/tokenizer/deepseek-v4-20261002")
        replace_json(tokenizer_lock,tokenizer_value)
        tokenizer_config={"path":str(tokenizer_lock),"before_sha256":tokenizer_before,"after_sha256":digest(tokenizer_lock),
                          "changed_fields":["/archive/path","/directory"]}
        stage="BEGIN_RECOVERY"
        with closing(open_db(recovered.sqlite)) as db:
            instance=IdentityStore(db).begin_recovery(recovery_id=recovery_id)
        record.update(data_instance_id=instance,phase="VERIFYING",tokenizer_config=tokenizer_config)
        save_recovery_record(marker,record)
        stage="RELOCATE_AND_QUARANTINE"
        relocate_artifacts(target,manifest["source_home"],instance)
        with closing(open_db(recovered.sqlite)) as db:
            relocate_database(db,target,manifest["source_home"],instance)
            with transaction(db):
                db.execute("UPDATE tasks SET recovery_quarantined=1")
                db.execute("UPDATE attempts SET recovery_quarantined=1")
                db.execute("UPDATE outbox SET recovery_quarantined=1")
        for path,original_mode in copied_modes:
            path.chmod(original_mode)
            if path.stat().st_mode & 0o7777 != original_mode:raise RuntimeError("恢复文件权限不一致："+str(path))
        return {"target":str(target),"data_instance_id":instance,"recovery_id":recovery_id,"verification_status":"VERIFYING","dispatch_open":False,
                "copied_files_permission_restored":len(copied_modes)}

    except BaseException as error:
        record.update(phase="FAILED",failure={"stage":stage,"error_type":type(error).__name__,
                      "message":str(error),"traceback":traceback.format_exc(),"failed_at":now()})
        try:save_recovery_record(marker,record)
        except Exception as record_error:error.add_note("Recovery failure record update failed: "+str(record_error))
        raise

def verify(settings):
    marker=settings.home/"recovery.json"
    record=json.loads(marker.read_bytes())
    if record.get("schema")!="raghub.recovery.v1" or record.get("new_home")!=str(settings.home) or record.get("phase")!="VERIFYING":raise RuntimeError("只能核验本根中已完成复制的未开放恢复。")
    if any(role_status(settings,name)["held"] for name in ROLES):raise RuntimeError("恢复核验前必须确认服务未启动。")
    # Exercise the same local resource consumers that API startup uses, before opening dispatch.
    from .retrieval.model_client import QueryTokenGate
    from .retrieval.tokenizer import EvidenceTokenizer
    query_gate=QueryTokenGate(settings)
    query_counts=query_gate.validate({"unique_queries":["恢复验证"],"rerank_query":"恢复验证"})
    evidence_tokenizer=EvidenceTokenizer(settings)
    evidence_count=evidence_tokenizer.count("恢复验证")
    if evidence_tokenizer.identity["lock_sha256"]!=record["tokenizer_config"]["after_sha256"]:raise RuntimeError("恢复 tokenizer 配置绑定不一致。")
    archive=evidence_tokenizer.identity["archive"];archive_path=Path(archive["path"])
    if not archive_path.is_relative_to(settings.home/"models") or archive_path.is_symlink() or archive_path.resolve(strict=True)!=archive_path:raise RuntimeError("恢复 tokenizer archive 路径不一致。")
    if archive_path.stat().st_size!=archive["size_bytes"] or digest(archive_path)!=archive["sha256"]:raise RuntimeError("恢复 tokenizer archive 字节不一致。")
    resource_closure={"query_counts":query_counts,"evidence_token_count":evidence_count,
                      "evidence_lock_sha256":evidence_tokenizer.identity["lock_sha256"],
                      "evidence_directory":evidence_tokenizer.identity["directory"],"archive_path":str(archive_path),
                      "archive_sha256":archive["sha256"],"maintenance_source_sha256":digest(Path(__file__))}
    with closing(open_db(settings.sqlite)) as db:
        state=system_state(db)
        if state["data_instance_id"]!=record["data_instance_id"] or state["recovery_id"]!=record["recovery_id"]:raise RuntimeError("恢复身份不匹配。")
        verify_schema(db,settings.project/"backend/migrations")
        native=db.core.connection.driver_connection
        if native.execute("PRAGMA integrity_check").fetchall()!=[("ok",)] or native.execute("PRAGMA foreign_key_check").fetchall():raise RuntimeError("恢复 SQLite 完整性失败。")
        if any((settings.home/"data/services/redis").iterdir()):raise RuntimeError("恢复 Redis 必须为空。")
        archives=db.execute("SELECT archive_manifest_path FROM raw_versions").fetchall()
        for row in archives:verify_archive(Path(row[0]))
        from .parsing.bundle import verify_bundle
        for row in db.execute("SELECT bundle_path,bundle_sha256 FROM processing_results").fetchall():
            if verify_bundle(Path(row["bundle_path"]))["manifest_sha256"]!=row["bundle_sha256"]:raise RuntimeError("恢复解析产物绑定不一致。")
        chunks=db.execute("SELECT manifest_path,manifest_sha256 FROM chunk_sets").fetchall()
        for row in chunks:
            path=Path(row["manifest_path"])
            if digest(path)!=row["manifest_sha256"]:raise RuntimeError("恢复块清单身份不一致。")
            value=json.loads(path.read_bytes())
            for member in value["files"]:
                if digest(path.parent/member["path"])!=member["sha256"]:raise RuntimeError("恢复块成员身份不一致。")
        generations=db.execute("SELECT manifest_path,manifest_sha256,input_sha256 FROM generations WHERE manifest_path IS NOT NULL").fetchall()
        for row in generations:
            path=Path(row["manifest_path"])
            if digest(path)!=row["manifest_sha256"]:raise RuntimeError("恢复索引清单身份不一致。")
            value=json.loads(path.read_bytes())
            if value["input_sha256"]!=row["input_sha256"] or json_hash(value["input"])!=row["input_sha256"]:raise RuntimeError("恢复索引输入不一致。")
        quarantine_counts={}
        for table in ("tasks","attempts","outbox"):
            row=db.execute(f"SELECT COUNT(*) AS total, COUNT(CASE WHEN recovery_quarantined=1 THEN 1 END) AS quarantined FROM {table}").fetchone()
            quarantine_counts[table]={"total":row["total"],"quarantined":row["quarantined"]}
            if row["total"]!=row["quarantined"]:raise RuntimeError("旧记录未完整隔离："+table)
        if db.execute("SELECT 1 FROM retrieval_requests WHERE state IN (?,?) LIMIT 1",("RUNNING","CANCELLING")).fetchone():raise RuntimeError("旧问答未中断。")
        with transaction(db):
            db.execute("UPDATE system_state SET verification_status=?,dispatch_open=1,updated_at=? WHERE singleton=1",("VERIFIED",now()))
        final=system_state(db)
    result={"data_instance_id":final["data_instance_id"],"verification_status":"VERIFIED","dispatch_open":True,
            "archives":len(archives),"chunk_sets":len(chunks),"generation_manifests":len(generations),"paid_call_paths_invoked":False,
            "old_tasks_remain_quarantined":all(item["total"]==item["quarantined"] for item in quarantine_counts.values()),
            "quarantine_counts":quarantine_counts,"runtime_resource_closure":resource_closure,"business_started":False}
    result_path=settings.home/("recovery-verified-"+new_id()+".json")
    write_json(result_path,result)
    record.update(phase="VERIFIED",verified_at=now(),verification_result_path=str(result_path),verification_result_sha256=digest(result_path))
    save_recovery_record(marker,record)
    return result

def resume(settings,task_id,expected_attempt):
    with closing(open_db(settings.sqlite)) as db:
        task=db.execute("SELECT kind,recovery_quarantined FROM tasks WHERE task_id=?",(task_id,)).fetchone()
        if task is None or task["kind"]!="DOCUMENT" or not task["recovery_quarantined"]:raise RuntimeError("此入口只人工恢复一个已隔离的文档任务。")
        return IngestionStore(db,settings).recover(system_state(db)["data_instance_id"],task_id,expected_attempt)

def main():
    parser=argparse.ArgumentParser();parser.add_argument("action",choices=["backup","restore","verify","resume"])
    parser.add_argument("--backup");parser.add_argument("--target");parser.add_argument("--task");parser.add_argument("--expected-attempt")
    args=parser.parse_args();settings=Settings.load()
    try:
        if args.action=="backup":result=backup(settings)
        elif args.action=="restore":
            if not args.backup or not args.target:parser.error("restore 需要 --backup 和 --target")
            result=restore(settings,args.backup,args.target)
        elif args.action=="verify":result=verify(settings)
        else:
            if not args.task or not args.expected_attempt:parser.error("resume 需要 --task 和 --expected-attempt")
            result=resume(settings,args.task,args.expected_attempt)
        print(json.dumps(result,ensure_ascii=True,sort_keys=True));return 0
    except Exception as error:
        print(json.dumps({"status":"MAINTENANCE_FAILED","error_type":type(error).__name__,"message":str(error),"traceback":traceback.format_exc()},ensure_ascii=True));return 1

if __name__=="__main__":raise SystemExit(main())
