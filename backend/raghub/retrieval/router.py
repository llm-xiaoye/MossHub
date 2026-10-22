"""Asynchronous stage-5 sessions, retrieval trace and source-scope checks."""
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import Field

from .contracts import SearchRequest, SessionRequest, Strict

router = APIRouter(prefix="/api/retrieval")


class Reference(Strict):
    evidence_id: str
    support_unit_id: str
    kb_id: str
    generation_id: str
    document_id: str
    raw_version_id: str
    processing_version_id: str
    source_record_id: str


class ReferencesRequest(Strict):
    references: list[Reference] = Field(min_length=1, max_length=6)


@router.post("/sessions", status_code=201)
async def session_create(body: SessionRequest, request: Request,
                         instance: str = Header(alias="X-RAG-Data-Instance"),
                         request_key: str = Header(alias="X-Request-ID")):
    return await request.app.state.retrieval.db.call("create_session", instance, request_key, body.kb_id)


@router.get("/sessions/{session_id}")
async def session_read(session_id: UUID, request: Request,
                       instance: str = Header(alias="X-RAG-Data-Instance")):
    return await request.app.state.retrieval.db.call("session", instance, str(session_id))


@router.post("/sessions/{session_id}/requests", status_code=202)
async def retrieve(session_id: UUID, body: SearchRequest, request: Request,
                   instance: str = Header(alias="X-RAG-Data-Instance"),
                   request_key: str = Header(alias="X-Request-ID")):
    return await request.app.state.retrieval.submit(instance, str(session_id), request_key, body)


@router.get("/requests/{request_id}")
async def retrieval_read(request_id: UUID, request: Request,
                         instance: str = Header(alias="X-RAG-Data-Instance")):
    return await request.app.state.retrieval.db.call("read", instance, str(request_id))


@router.post("/requests/{request_id}/cancel")
async def retrieval_cancel(request_id: UUID, request: Request,
                           instance: str = Header(alias="X-RAG-Data-Instance")):
    return await request.app.state.retrieval.db.call("cancel", instance, str(request_id))


@router.post("/requests/{request_id}/references/validate")
async def references_validate(request_id: UUID, body: ReferencesRequest, request: Request,
                              instance: str = Header(alias="X-RAG-Data-Instance")):
    return await request.app.state.retrieval.validate_references(
        instance, str(request_id), [reference.model_dump() for reference in body.references])
