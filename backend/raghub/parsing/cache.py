"""Reusable immutable PARSE output, adopted before the CLEAN stage begins."""
from __future__ import annotations
import json
from pathlib import Path
from docling_core.types.doc import DoclingDocument
from ..ingestion.archive import hash_file,json_bytes,write_closed,verify_archive,inside
from ..parsing_assets import parsing_models
from .formats import Parsed,convert,no_duplicates
from .bundle import choose_original


def verify_parse(path):
    path=Path(path)
    manifest=json.loads(path.read_bytes(),object_pairs_hook=no_duplicates)
    expected={"schema","processing_version_id","raw_version_id","config_sha256","stage_input_sha256","files"}
    if set(manifest)!=expected or manifest["schema"]!="raghub.parse-cache.v1":
        raise ValueError("invalid PARSE cache schema")
    names=[]
    for entry in manifest["files"]:
        if set(entry)!={"path","sha256","size_bytes"}:
            raise ValueError("invalid PARSE member")
        member=inside(path.parent/entry["path"],path.parent)
        if member.is_symlink() or not member.is_file() or hash_file(member)!=(entry["sha256"],entry["size_bytes"]):
            raise ValueError("PARSE cache bytes changed")
        names.append(entry["path"])
    if names!=sorted(set(names)) or not {"native.json","metadata.json"}<=set(names):
        raise ValueError("PARSE cache membership invalid")
    if {p.name for p in path.parent.iterdir()}!=set(names)|{path.name}:
        raise ValueError("PARSE cache has unexpected members")
    DoclingDocument.model_validate_json((path.parent/"native.json").read_bytes())
    return {"manifest_path":str(path),"manifest_sha256":hash_file(path)[0],"manifest":manifest}


def restore_parse(record):
    observed=verify_parse(record["manifest_path"])
    if observed!=record:
        raise ValueError("PARSE cache identity changed")
    root=Path(record["manifest_path"]).parent
    metadata=json.loads((root/"metadata.json").read_bytes(),object_pairs_hook=no_duplicates)
    if set(metadata)!={"scope","details","locations","quality","images"}:
        raise ValueError("PARSE cache metadata schema mismatch")
    parsed=Parsed(DoclingDocument.model_validate_json((root/"native.json").read_bytes()),
                  scope=metadata["scope"],details=metadata["details"])
    parsed.locations,parsed.quality,parsed.images=metadata["locations"],metadata["quality"],metadata["images"]
    parsed.cache_root=root
    return parsed


def create_parse(settings,payload,destination,input_digest):
    archive=verify_archive(Path(payload["archive"]["manifest_path"]))
    if archive!=payload["archive"]:
        raise ValueError("PARSE archived input changed")
    primary=choose_original(archive)
    lock=parsing_models(settings)
    if lock["lock_sha256"]!=payload["parsing_config"]["model_lock_sha256"]:
        raise ValueError("PARSE model lock changed")
    destination.mkdir(parents=True,exist_ok=False)
    parsed=convert(Path(archive["manifest_path"]).parent/primary["path"],payload["parsing_config"],lock,
                   workdir=destination,resource_observations=payload["source_details"])
    for page_no,page in parsed.document.pages.items():
        if page.image is not None and page.image.pil_image is not None:
            image=page.image.pil_image
            path=destination/("page-"+str(page_no)+".png")
            image.save(path,format="PNG")
            for item in parsed.images:
                if item["page"]==page_no:
                    item.update(path=path.name,sha256=hash_file(path)[0],width=image.width,height=image.height)
    write_closed(destination/"native.json",json_bytes(parsed.document.model_dump(mode="json")))
    write_closed(destination/"metadata.json",json_bytes({"scope":parsed.scope,"details":parsed.details,"locations":parsed.locations,
                 "quality":parsed.quality,"images":parsed.images}))
    files=[]
    for path in sorted(destination.iterdir()):
        digest,size=hash_file(path)
        files.append({"path":path.name,"sha256":digest,"size_bytes":size})
        path.chmod(0o444)
    from ..ingestion.archive import json_hash
    manifest={"schema":"raghub.parse-cache.v1","processing_version_id":payload["processing_version_id"],
              "raw_version_id":payload["raw_version_id"],"config_sha256":json_hash(payload["parsing_config"]),
              "stage_input_sha256":input_digest,"files":files}
    path=destination/"parse-manifest.json"
    write_closed(path,json_bytes(manifest))
    path.chmod(0o444)
    destination.chmod(0o555)
    return verify_parse(path)
