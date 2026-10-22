"""Immutable native JSON, cleanup/source supplement and preview stage artifacts."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from docling_core.types.doc import DoclingDocument

from .cleanup import clean, manual
from .contracts import FieldSource, Supplement
from .formats import convert, field_values, no_duplicates, base_location
from ..ingestion.archive import hash_file, inside, json_bytes, json_hash, write_closed, verify_archive
from ..parsing_assets import parsing_models


def web_resource_quality(quality, source_details):
    """An unprocessed webpage graphic remains blocking after parsing/revision."""
    result=[q for q in quality if q.get("code")!="WEB_GRAPHICS_NOT_PROCESSED"]
    source=source_details or {}
    remaining=source.get("unsupported_dependencies", [])
    if remaining or source.get("resource_review_required"):
        result.append({"severity":"WARN","code":"WEB_GRAPHICS_NOT_PROCESSED",
            "dependency_count":len(remaining) if isinstance(remaining,list) else None,
            "reason":"网页仍有未解析的图形内容，暂不能用于问答；需要完善采集或改用包含完整内容的文件。"})
    return result


def value_at(obj, pointer):
    node=obj
    for key in pointer[1:].split("/"):
        node=node[int(key)] if isinstance(node,list) else node[key]
    return node


def set_at(obj, pointer, value):
    components=pointer[1:].split("/")
    node=obj
    for key in components[:-1]:
        node=node[int(key)] if isinstance(node,list) else node[key]
    key=components[-1]
    if isinstance(node,list):
        node[int(key)]=value
    else:
        node[key]=value


def revision_diff(base:dict, revised:dict):
    DoclingDocument.model_validate(revised)
    if len(json_bytes(revised))>50*1024*1024:
        raise ValueError("revision JSON exceeds 50 MiB")
    allowed={field for field,_,_ in field_values(base)}
    differences=[]
    def compare(a,b,path=""):
        if type(a) is not type(b):
            raise ValueError("revision changes protected type: "+path)
        if isinstance(a,dict):
            if set(a)!=set(b):
                raise ValueError("revision adds/removes protected keys: "+path)
            for key in a:
                compare(a[key],b[key],path+"/"+key)
        elif isinstance(a,list):
            if len(a)!=len(b):
                raise ValueError("revision adds/removes structural nodes: "+path)
            for i,(before,after) in enumerate(zip(a,b,strict=True)):
                compare(before,after,path+"/"+str(i))
        elif a!=b:
            if path not in allowed or not isinstance(b,str) or len(b)>100000:
                raise ValueError("revision changes protected field: "+path)
            differences.append({"pointer":path,"before":a,"after":b})
    compare(base,revised)
    return differences



def bind_quality(quality, raw_id, processing_id):
    result=copy.deepcopy(quality)
    seen=set()
    for index,issue in enumerate(result):
        if "issue_id" not in issue:
            issue["issue_id"]=json_hash({"raw_version_id":raw_id,"origin_processing_version_id":processing_id,
                                       "ordinal":index,"diagnostic":issue})
            issue["origin_processing_version_id"]=processing_id
        if issue["issue_id"] in seen:
            raise ValueError("duplicate quality issue ID")
        seen.add(issue["issue_id"])
    return result


def resource_transcription_binding(review, issue, before_fields, after_fields, field_sources, supplement):
    """A human transcription refers to the whole immutable graphic, not one OCR crop."""
    pointer=review["field_pointer"]
    if pointer not in before_fields or before_fields[pointer]==after_fields[pointer] or not after_fields[pointer].strip():
        raise ValueError("resource transcription requires a changed, nonempty existing field")
    digest=issue["resource_sha256"]
    locator=field_sources[pointer].locator.model_dump(mode="json")
    if locator.get("kind") not in {"md","html"} or locator.get("resource_sha256")!=digest:
        raise ValueError("transcription field does not belong to the selected saved graphic")
    images=[image for image in supplement.images if image.get("resource_sha256")==digest]
    dimensions={(image.get("width"),image.get("height")) for image in images}
    if len(dimensions)!=1:
        raise ValueError("graphic dimensions are missing or inconsistent")
    width,height=next(iter(dimensions))
    if type(width) is not int or type(height) is not int or min(width,height)<=0:
        raise ValueError("graphic dimensions are invalid")
    locator.update(basis="HUMAN_VERIFIED_RESOURCE_TRANSCRIPTION",granularity="REGION",
        region=[[0,0],[width,0],[width,height],[0,height]])
    locator["source_group"]=[*(locator.get("source_group") or []),{
        "kind":"HUMAN_RESOURCE_TRANSCRIPTION","issue_id":issue["issue_id"],
        "resource_sha256":digest,"accepted_value_sha256":json_hash(after_fields[pointer]),
        "review_note":review["note"],"scope":"WHOLE_SAVED_GRAPHIC"}]
    return {"field_pointer":pointer,"before_text":before_fields[pointer],"after_text":after_fields[pointer],
            "accepted_value_sha256":json_hash(after_fields[pointer]),"locator":locator}


def review_plan(base, revised, supplement, bundle_sha256, reviews):
    """Bind each explicit decision to one actual issue and the reviewed content."""
    issues={q["issue_id"]:q for q in supplement.quality}
    before_fields={field:text for field,text,_ in field_values(base)}
    after_fields={field:text for field,text,_ in field_values(revised)}
    field_sources={field.field_pointer:field for field in supplement.fields}
    result=[]
    seen=set()
    for review in reviews:
        required={"issue_id","outcome","note"}
        if review.get("outcome")=="RESOURCE_TRANSCRIBED":
            required.add("field_pointer")
        if set(review)!=required or not isinstance(review["note"],str) or not review["note"].strip() or len(review["note"])>2000:
            raise ValueError("review requires an exact issue, outcome and user note")
        issue_id=review["issue_id"]
        if issue_id in seen or issue_id not in issues:
            raise ValueError("review selects a duplicate or unknown quality issue")
        seen.add(issue_id)
        issue=issues[issue_id]
        field=issue.get("field")
        outcome=review["outcome"]
        binding={"raw_version_id":supplement.raw_version_id,"base_processing_version_id":supplement.processing_version_id,
                 "base_bundle_sha256":bundle_sha256,"issue_sha256":json_hash(issue)}
        if outcome in {"VERIFIED_CORRECT","CORRECTED"}:
            if issue["code"] not in {"OCR_NUMERIC_REVIEW","OCR_REVIEW_CANDIDATE"} or field not in before_fields:
                raise ValueError("this issue cannot be cleared by an OCR text review")
            if outcome=="VERIFIED_CORRECT" and before_fields[field]!=after_fields[field]:
                raise ValueError("verified-correct review requires unchanged reviewed text")
            if outcome=="CORRECTED" and before_fields[field]==after_fields[field]:
                raise ValueError("corrected review requires an actual change in its own field")
            if issue.get("resolution") and before_fields[field]==after_fields[field]:
                raise ValueError("issue has already been resolved for this content")
            binding.update({"field_pointer":field,"before_text":before_fields[field],"after_text":after_fields[field],
                            "locator":field_sources[field].locator.model_dump(mode="json"),
                            "accepted_value_sha256":json_hash(after_fields[field])})
        elif outcome in {"RESOURCE_EXEMPT","RESOURCE_TRANSCRIBED","RESOURCE_EXCLUDED"}:
            if issue["code"]!="BITMAP_STRUCTURE_REVIEW_REQUIRED" or issue.get("resolution"):
                raise ValueError("resource exemption only covers the selected unresolved bitmap issue")
            digest=issue.get("resource_sha256")
            if not isinstance(digest,str) or len(digest)!=64 or not issue.get("locator"):
                raise ValueError("bitmap issue lacks its original resource identity")
            if not any(image.get("resource_sha256")==digest for image in supplement.images):
                raise ValueError("resource exemption is not bound to an actual saved image")
            binding.update({"resource_sha256":digest,"locator":issue["locator"],
                            "original_identity":supplement.original_identity})
            if outcome=="RESOURCE_TRANSCRIBED":
                binding.update(resource_transcription_binding(review,issue,before_fields,after_fields,field_sources,supplement))
            if outcome=="RESOURCE_EXCLUDED":
                excluded=sorted(pointer for pointer,source in field_sources.items()
                    if source.locator.model_dump(mode="json").get("resource_sha256")==digest)
                if not excluded or any(pointer not in after_fields or after_fields[pointer]!="" for pointer in excluded):
                    raise ValueError("excluded resource requires every bound text field to be empty")
                binding["excluded_fields"]=excluded
        else:
            raise ValueError("unsupported quality review outcome")
        result.append({**review,"binding":binding})
    return result


def apply_reviews(quality, reviews, native, decision):
    result=copy.deepcopy(quality)
    fields={field:text for field,text,_ in field_values(native)}
    invalid_scopes=set()
    for issue in result:
        resolution=issue.get("resolution")
        if not resolution or resolution["outcome"]=="RESOURCE_EXEMPT":
            continue
        binding=resolution["binding"]
        if resolution["outcome"]=="RESOURCE_EXCLUDED" and "excluded_fields" in binding:
            changed=any(pointer not in fields or fields[pointer]!="" for pointer in binding["excluded_fields"])
            if changed:
                invalid_scopes.add(issue["issue_id"])
        else:
            pointer=binding["field_pointer"]
            changed=pointer not in fields or json_hash(fields[pointer])!=binding["accepted_value_sha256"]
        if changed:
            issue.setdefault("review_history",[]).append({**resolution,"invalidated":"REVIEWED_FIELD_CHANGED"})
            issue.pop("resolution")
    for issue in result:
        resolution=issue.get("resolution")
        if resolution and resolution["binding"].get("scope_review_issue_id") in invalid_scopes:
            issue.setdefault("review_history",[]).append({**resolution,"invalidated":"RESOURCE_SCOPE_CHANGED"})
            issue.pop("resolution")
    indexed={issue["issue_id"]:issue for issue in result}
    for review in reviews:
        issue=indexed[review["issue_id"]]
        resolution={**copy.deepcopy(review),"human_decision":copy.deepcopy(decision)}
        issue.setdefault("review_history",[]).append(resolution)
        issue["resolution"]=resolution
        if review["outcome"]=="RESOURCE_EXCLUDED":
            binding=review["binding"]
            excluded=set(binding["excluded_fields"])
            for candidate in result:
                pointer=candidate.get("field")
                if candidate.get("resolution") or candidate["code"] not in {"OCR_REVIEW_CANDIDATE","OCR_NUMERIC_REVIEW"} or pointer not in excluded:
                    continue
                scope_resolution={"issue_id":candidate["issue_id"],"outcome":"RESOURCE_EXCLUDED",
                    "note":"用户明确排除整份图片内容；此记录仅表示不用于问答，不表示 OCR 内容已校对正确。",
                    "binding":{"raw_version_id":binding["raw_version_id"],
                        "base_processing_version_id":binding["base_processing_version_id"],
                        "base_bundle_sha256":binding["base_bundle_sha256"],
                        "resource_sha256":binding["resource_sha256"],
                        "scope_review_issue_id":review["issue_id"],
                        "issue_sha256":json_hash(candidate),"field_pointer":pointer,
                        "accepted_value_sha256":json_hash(fields[pointer])},
                    "human_decision":copy.deepcopy(decision)}
                candidate.setdefault("review_history",[]).append(scope_resolution)
                candidate["resolution"]=scope_resolution
    return result


def verify_bundle(path:Path)->dict:
    path=path.resolve(strict=True)
    manifest=json.loads(path.read_bytes(),object_pairs_hook=no_duplicates)
    if set(manifest)!={"schema","processing_version_id","raw_version_id","config_sha256","status","files","counts"} or manifest["schema"]!="raghub.parsing-bundle.v1":
        raise ValueError("unsupported parsing bundle")
    seen=set()
    for item in manifest["files"]:
        if set(item)!={"path","sha256","size_bytes"} or item["path"] in seen:
            raise ValueError("duplicate/invalid parsing bundle member")
        member=inside(path.parent/item["path"],path.parent)
        if member.is_symlink() or not member.is_file() or hash_file(member)!=(item["sha256"],item["size_bytes"]):
            raise ValueError("parsing bundle changed: "+item["path"])
        seen.add(item["path"])
    required={"automatic.native.json","cleaned.native.json","supplement.json","preview.md"}
    if not required<=seen:
        raise ValueError("parsing bundle is missing required members")
    supplement=Supplement.model_validate_json((path.parent/"supplement.json").read_bytes())
    document=DoclingDocument.model_validate_json((path.parent/"cleaned.native.json").read_bytes())
    native=document.model_dump(mode="json")
    fields={field:text for field,text,_ in field_values(native)}
    if (supplement.processing_version_id!=manifest["processing_version_id"]
        or supplement.raw_version_id!=manifest["raw_version_id"] or supplement.config_sha256!=manifest["config_sha256"]
        or len(supplement.fields)!=manifest["counts"]["fields"]
        or len({field.field_pointer for field in supplement.fields})!=len(supplement.fields)
        or set(fields)!={field.field_pointer for field in supplement.fields}
        or any(fields[field.field_pointer]!=field.cleaned_text for field in supplement.fields)):
        raise ValueError("parsing bundle identity/count/field mismatch")
    unresolved=sum(1 for issue in supplement.quality if not issue.get("resolution"))
    if (manifest["counts"]["quality"]!=len(supplement.quality)
        or manifest["counts"]["unresolved_quality"]!=unresolved
        or manifest["status"]!=("REVIEW_REQUIRED" if unresolved else "READY")):
        raise ValueError("parsing bundle quality/status mismatch")
    return {"manifest_path":str(path),"manifest_sha256":hash_file(path)[0],
            "manifest":manifest,"status":manifest["status"]}


def choose_original(archive):
    files=archive["files"]
    candidates=[item for item in files if item["path"].startswith("original.") or item["path"]=="captured.html"]
    if len(candidates)!=1:
        raise ValueError("archive does not identify exactly one primary original")
    return candidates[0]


def build_bundle(settings, *, archive_path, raw_id, processing_id, config, destination, revision=None, source_details=None, cached_parsed=None):
    archive=verify_archive(Path(archive_path))
    raw_manifest=json.loads(Path(archive_path).read_bytes())
    primary=choose_original(raw_manifest)
    original_path=Path(archive_path).parent/primary["path"]
    lock=parsing_models(settings)
    if config.get("model_lock_sha256")!=lock["lock_sha256"]:
        raise ValueError("processing model lock differs from frozen configuration")
    source_details=source_details or {}
    config_sha=json_hash(config)
    if destination.exists():
        raise ValueError("parsing stage directory must be fresh")
    destination.mkdir(parents=True,exist_ok=False)
    if revision:
        old=verify_bundle(Path(revision["base_bundle"]))
        automatic=json.loads((Path(revision["base_bundle"]).parent/"automatic.native.json").read_bytes())
        if old["manifest"]["raw_version_id"]!=raw_id:
            raise ValueError("revision is not bound to current original")
        revision_path=Path(revision["path"])
        if hash_file(revision_path)[0]!=revision["sha256"]:
            raise ValueError("revision input changed")
        native=json.loads(revision_path.read_bytes(),object_pairs_hook=no_duplicates)
        previous_native=json.loads((Path(revision["base_bundle"]).parent/"cleaned.native.json").read_bytes())
        difference=revision_diff(previous_native,native)
        previous=Supplement.model_validate_json((Path(revision["base_bundle"]).parent/"supplement.json").read_bytes())
        prior_fields={f.field_pointer:f for f in previous.fields}
        locations={f.field_pointer:f.locator.model_dump(mode="json") for f in previous.fields}
        quality=copy.deepcopy(previous.quality)
        bound_reviews=review_plan(previous_native,native,previous,old["manifest_sha256"],revision["reviews"])
        if json_hash(bound_reviews)!=revision["review_sha256"]:
            raise ValueError("review decisions changed after preview")
        for review in bound_reviews:
            if review["outcome"]=="RESOURCE_TRANSCRIBED":
                locations[review["binding"]["field_pointer"]]=copy.deepcopy(review["binding"]["locator"])
        if not difference and not bound_reviews:
            raise ValueError("revision has neither text changes nor selected quality decisions")
        scope,details,images=previous.scope,{**previous.format_details,"revision":revision["revision_id"],"downstream_invalidated":["CLEAN","CHUNK","EMBED","INDEX"]},previous.images
        write_closed(destination/"revision.diff.json",json_bytes(difference))
        write_closed(destination/"revision.input.json",json_bytes(native))
        images=copy.deepcopy(images)
        for image in images:
            if image.get("path"):
                old_image=Path(revision["base_bundle"]).parent/image["path"]
                if hash_file(old_image)[0]!=image["sha256"]:
                    raise ValueError("revision page image changed")
                write_closed(destination/image["path"],old_image.read_bytes())
    else:
        parsed=cached_parsed or convert(original_path,config,lock,workdir=destination,resource_observations=source_details)
        automatic=parsed.document.model_dump(mode="json")
        native=copy.deepcopy(automatic)
        locations,quality=parsed.locations,parsed.quality
        scope,details,images=parsed.scope,parsed.details,parsed.images
        difference=[]
        bound_reviews=[]
        for page_no,page in parsed.document.pages.items():
            if page.image is not None:
                image=page.image.pil_image
                if image is not None:
                    output=destination/("page-"+str(page_no)+".png")
                    image.save(output,format="PNG")
                    for item in images:
                        if item["page"]==page_no:
                            item["path"]=output.name
                            item["sha256"]=hash_file(output)[0]
                            item["width"],item["height"]=image.width,image.height
    if not revision and cached_parsed is not None:
        for image in images:
            member=image.get("path")
            if not member:
                continue
            target=inside(destination/member,destination)
            if target.exists():
                continue
            source=inside(Path(cached_parsed.cache_root)/member,Path(cached_parsed.cache_root))
            if hash_file(source)[0]!=image["sha256"]:
                raise ValueError("cached image frame changed before CLEAN")
            write_closed(target,source.read_bytes())
    baseline={field:text for field,text,_ in field_values(automatic)}
    changed={d["pointer"] for d in difference}
    sources=[]
    changes=[]
    for field,text,label in field_values(native):
        original=baseline[field]
        plain=label in {"text","paragraph","title","section_header","list_item","caption","footnote"} and field not in set(details.get("protected_fields",[]))
        if any(mark in text for mark in ("\u0060","$")):
            plain=False
        if field in changed:
            cleaned,mappings,operations=manual(text,original)
        elif revision:
            inherited=prior_fields[field]
            if text!=inherited.cleaned_text or original!=inherited.original_text:
                raise ValueError("unmodified revision field does not match current bound field")
            cleaned=text
            mappings=[segment.model_dump(mode="json") for segment in inherited.mappings]
            operations=[]
        else:
            cleaned,mappings,operations=clean(text,plain=plain)
        locator=locations.get(field)
        if locator is None:
            empty_container=(label=="list_item" and not text.strip()
                and bool(value_at(native,field.rsplit("/",1)[0]).get("children")))
            empty_cell=(label=="cell" and original=="" and text=="")
            no_text_structure=empty_container or empty_cell
            reason=("EMPTY_STRUCTURAL_TABLE_CELL_NO_TEXT" if empty_cell else
                "EMPTY_STRUCTURAL_LIST_CONTAINER_NO_TEXT" if empty_container else
                "NO_UNIQUE_VERIFIED_SOURCE_ANCHOR")
            locator=base_location("file",primary["sha256"],reason=reason)
            locator["granularity"]="COARSE"
            if not no_text_structure and not any(q.get("field")==field and q["code"]=="COARSE_SOURCE_MAPPING" for q in quality):
                quality.append({"severity":"WARN","code":"COARSE_SOURCE_MAPPING","field":field})
        source=FieldSource(field_pointer=field,original_text=original,cleaned_text=cleaned,locator=locator,mappings=mappings)
        sources.append(source)
        changes.extend({"field":field,**operation} for operation in operations)
        set_at(native,field,cleaned)
    if not sources or not any(f.cleaned_text.strip() for f in sources):
        quality.append({"severity":"ERROR","code":"EMPTY_DOCUMENT_BODY"})
    for i,table in enumerate(native["tables"]):
        data=table["data"]
        for cell in data["table_cells"]:
            if not (0<=cell["start_row_offset_idx"]<cell["end_row_offset_idx"]<=data["num_rows"] and
                    0<=cell["start_col_offset_idx"]<cell["end_col_offset_idx"]<=data["num_cols"]):
                raise ValueError("parsed table cell is outside grid")
    quality=web_resource_quality(quality,source_details)
    quality=bind_quality(quality,raw_id,processing_id)
    if revision:
        quality=apply_reviews(quality,bound_reviews,native,revision["human_decision"])
        details["manual_review_history"]=[*previous.format_details.get("manual_review_history",[]),
            {"base_processing_version_id":previous.processing_version_id,"revision_id":revision["revision_id"],
             "review_sha256":revision["review_sha256"],"decisions":bound_reviews,
             "human_decision":revision["human_decision"]}]
    unresolved=sum(1 for q in quality if not q.get("resolution"))
    details["cleanup_changes"]=changes
    details["automatic_source_preserved"]=True
    details["source_observation"]=source_details
    supplement=Supplement(raw_version_id=raw_id,processing_version_id=processing_id,
        original_identity={"manifest_path":str(archive_path),"manifest_sha256":archive["manifest_sha256"],
                           "primary":primary},adapter="raghub-format-adapters-v1",config_sha256=config_sha,
        model_lock_sha256=lock["lock_sha256"],scope=scope,format_details=details,fields=sources,
        quality=quality,images=images)
    document=DoclingDocument.model_validate(native)
    write_closed(destination/"automatic.native.json",json_bytes(automatic))
    write_closed(destination/"cleaned.native.json",json_bytes(native))
    write_closed(destination/"supplement.json",supplement.model_dump_json(indent=2).encode("utf-8")+b"\n")
    write_closed(destination/"preview.md",document.export_to_markdown().encode("utf-8")+b"\n")
    members=[]
    for path in sorted(destination.iterdir()):
        digest,size=hash_file(path)
        path.chmod(0o444)
        members.append({"path":path.name,"sha256":digest,"size_bytes":size})
    status="REVIEW_REQUIRED" if unresolved else "READY"
    manifest={"schema":"raghub.parsing-bundle.v1","processing_version_id":processing_id,"raw_version_id":raw_id,
              "config_sha256":config_sha,"status":status,"files":members,
              "counts":{"fields":len(sources),"tables":len(document.tables),"pages":len(document.pages),
                        "quality":len(quality),"unresolved_quality":unresolved,"mapping_segments":sum(len(f.mappings) for f in sources)}}
    manifest_path=destination/"bundle-manifest.json"
    write_closed(manifest_path,json_bytes(manifest))
    manifest_path.chmod(0o444)
    destination.chmod(0o555)
    return verify_bundle(manifest_path)
