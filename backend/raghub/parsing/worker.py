"""Document Worker PARSE/CLEAN stages; archived inputs are not reacquired."""
from __future__ import annotations
import json
from pathlib import Path

from ..db import transaction
from ..ingestion.archive import hash_file,json_hash,verify_archive
from .bundle import build_bundle,verify_bundle
from .cache import create_parse,verify_parse,restore_parse
from .store import ParsingStore


def reusable(db,store,identity,processing,input_digest,stage,verify):
    rows=db.execute("SELECT a.artifact_id,s.manifest_json FROM stage_outputs s JOIN stage_artifacts a USING(artifact_id) "
        "WHERE s.processing_version_id=? AND s.stage_input_sha256=? AND a.stage=? AND a.adopted=1 ORDER BY a.created_at DESC",
        (processing,input_digest,stage)).fetchall()
    for row in rows:
        try:
            saved=json.loads(row["manifest_json"])
            if verify(Path(saved["manifest_path"]))!=saved:
                raise ValueError("stored stage differs from current artifact bytes")
        except (ValueError,FileNotFoundError) as exc:
            store.progress(identity,stage+"_REUSE_REJECTED",{"artifact_id":row["artifact_id"],"reason":str(exc)})
            continue
        store.progress(identity,stage+"_REUSED",{"artifact_id":row["artifact_id"]})
        return row["artifact_id"],saved
    return None,None


def parse_claimed(db,settings,identity,directory,payload):
    store=ParsingStore(db,settings)
    p=payload["processing_version_id"]
    config=payload["parsing_config"]
    for name,digest in config["adapter_sources"].items():
        if hash_file(settings.project/"backend/raghub/parsing"/name)[0]!=digest:
            raise RuntimeError("parser implementation changed after task input was frozen")
    if hash_file(settings.project/"backend/raghub/parsing_assets.py")[0]!=config["assets_source_sha256"] or hash_file(settings.project/"uv.lock")[0]!=config["dependency_lock_sha256"]:
        raise RuntimeError("parsing environment contract changed after task acceptance")
    archive=verify_archive(Path(payload["archive"]["manifest_path"]))
    if archive!=payload["archive"]:
        raise RuntimeError("archived input changed after task acceptance")
    input_digest=json_hash({"archive":archive,"config":config,"revision":payload.get("revision"),
        "source_details":payload["source_details"]})
    with transaction(db):
        store.current_owner(*identity)
        document=db.execute("SELECT current_processing_version_id,active_prepare_task_id,current_raw_version_id,removed_at FROM documents WHERE document_id=?",(payload["document_id"],)).fetchone()
        if (document is None or document["removed_at"] or document["current_processing_version_id"]!=p
                or document["active_prepare_task_id"]!=identity[1] or document["current_raw_version_id"]!=payload["raw_version_id"]):
            raise RuntimeError("parser is no longer the current document preparation")
        db.execute("UPDATE processing_versions SET status='PROCESSING' WHERE processing_version_id=?",(p,))
    artifact,bundle=reusable(db,store,identity,p,input_digest,"CLEAN",verify_bundle)
    if bundle:
        return store.adopt_parse(identity,bundle,input_digest,reuse_of=artifact)
    parsed=None
    if not payload.get("revision"):
        _,record=reusable(db,store,identity,p,input_digest,"PARSE_AUTO",verify_parse)
        if record is None:
            count=store.use_stage_budget(identity,"PARSE",input_digest)
            store.progress(identity,"PARSING",{"execution":count,"original_reacquired":False})
            record=create_parse(settings,payload,directory/("parse-"+str(count)),input_digest)
            store.adopt_intermediate(identity,record,input_digest)
        parsed=restore_parse(record)
    count=store.use_stage_budget(identity,"CLEAN",input_digest)
    store.progress(identity,"CLEANING",{"execution":count,"original_reacquired":False,"complete_parse_reused":parsed is not None})
    bundle=build_bundle(settings,archive_path=archive["manifest_path"],raw_id=payload["raw_version_id"],
        processing_id=p,config=config,destination=directory/("clean-"+str(count)),
        revision=payload.get("revision"),source_details=payload["source_details"],cached_parsed=parsed)
    return store.adopt_parse(identity,bundle,input_digest)
