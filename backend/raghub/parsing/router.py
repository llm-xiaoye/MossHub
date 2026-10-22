"""Parsing and full-JSON revision endpoints; original files stay immutable."""
from __future__ import annotations
import json
from contextlib import closing
from pathlib import Path
from uuid import UUID
from typing import Literal

from fastapi import APIRouter,Header
from pydantic import Field,UUID4
from starlette.responses import Response

from ..config import Settings
from ..contracts import StrictRecord
from ..db import open_db
from .store import ParsingStore
from .ocr import OCRStore

router=APIRouter(prefix="/api/parsing")
settings=Settings.load()


class ParseRequest(StrictRecord):
    kb_id: UUID4
    document_id: UUID4
    config: dict = Field(default_factory=dict)
    expected_raw_version_id: UUID4 | None = None


class IssueReview(StrictRecord):
    issue_id: str = Field(pattern="^[0-9a-f]{64}$")
    outcome: Literal["VERIFIED_CORRECT","CORRECTED","RESOURCE_EXEMPT","RESOURCE_TRANSCRIBED","RESOURCE_EXCLUDED"]
    note: str = Field(min_length=1,max_length=2000)
    field_pointer: str | None = Field(default=None,max_length=1000)


class RevisionPreview(StrictRecord):
    document_id: UUID4
    base_processing_version_id: UUID4
    full_native_json: dict
    reviews: list[IssueReview] = Field(default_factory=list,max_length=500)


class HumanDecision(StrictRecord):
    decision: Literal["ACCEPT","REJECT","MODIFY_ACCEPT"]
    source: Literal["LOCAL_USER_CLI","LOCAL_USER_UI","CHATGPT_USER_MESSAGE"]
    note: str = Field(min_length=1,max_length=2000)


class RevisionDecision(HumanDecision):
    preview_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class OCRDecision(HumanDecision):
    modified_text: str | None = Field(default=None,max_length=100000)
    reviews: list[IssueReview] = Field(default_factory=list,max_length=500)


def call(method,*args):
    with closing(open_db(settings.sqlite)) as db:
        return getattr(ParsingStore(db,settings),method)(*args)


@router.post("/tasks")
def parse(body:ParseRequest,instance:str=Header(alias="X-RAG-Data-Instance"),request_key:str=Header(alias="X-Request-ID")):
    with closing(open_db(settings.sqlite)) as db:
        return ParsingStore(db,settings).queue_parse(instance,request_key,str(body.kb_id),str(body.document_id),body.config,
            expected_raw_version_id=str(body.expected_raw_version_id) if body.expected_raw_version_id else None)


@router.get("/versions/{processing_id}")
def result(processing_id:UUID):
    return call("result",str(processing_id))


@router.get("/versions/{processing_id}/native")
def native(processing_id:UUID):
    value=call("result",str(processing_id))
    path=Path(value["bundle_path"]).parent/"cleaned.native.json"
    return Response(path.read_bytes(),media_type="application/json")


@router.get("/versions/{processing_id}/supplement")
def supplement(processing_id:UUID):
    value=call("result",str(processing_id))
    path=Path(value["bundle_path"]).parent/"supplement.json"
    return Response(path.read_bytes(),media_type="application/json")


@router.post("/revisions/preview")
def preview(body:RevisionPreview,instance:str=Header(alias="X-RAG-Data-Instance")):
    return call("revision_preview",instance,str(body.document_id),str(body.base_processing_version_id),body.full_native_json,[review.model_dump(exclude_none=True) for review in body.reviews])


@router.post("/revisions/{preview_id}/submit")
def submit(preview_id:UUID,body:RevisionDecision,instance:str=Header(alias="X-RAG-Data-Instance"),request_key:str=Header(alias="X-Request-ID")):
    return call("submit_revision",instance,request_key,str(preview_id),body.model_dump())


class Crop(StrictRecord):
    page: int = Field(ge=1)
    box: list[int] | None = None


class OCRRequest(StrictRecord):
    document_id: UUID4
    processing_version_id: UUID4
    field_pointer: str
    crops: list[Crop] = Field(min_length=1,max_length=3)


@router.post("/ocr/tasks")
def ocr_request(body:OCRRequest,instance:str=Header(alias="X-RAG-Data-Instance"),request_key:str=Header(alias="X-Request-ID")):
    with closing(open_db(settings.sqlite)) as db:
        return OCRStore(db,settings).queue_suggestion(instance,request_key,str(body.document_id),
            str(body.processing_version_id),body.field_pointer,[c.model_dump() for c in body.crops])


@router.get("/ocr/tasks/{task_id}")
def ocr_result(task_id:UUID):
    with closing(open_db(settings.sqlite)) as db:
        return OCRStore(db,settings).suggestion(str(task_id))


@router.post("/ocr/tasks/{task_id}/decision")
def ocr_decision(task_id:UUID,body:OCRDecision,instance:str=Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        return OCRStore(db,settings).decide(instance,str(task_id),body.model_dump(exclude_none=True))
