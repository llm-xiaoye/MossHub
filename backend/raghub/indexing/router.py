"""Index generation, removal and previewed maintenance on the loopback API."""
from contextlib import closing
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings
from ..db import open_db, system_state
from ..ingestion.store import require_document, require_kb
from ..model_profiles import load_tokenizer, profiles
from .cleanup import close_reader, confirm_cleanup, open_reader, preview_cleanup, retained_generations
from .storage import verify_chunk_set
from .store import IndexStore, removal_snapshot
from ..deletion import DocumentDeletion

router = APIRouter(prefix="/api")
settings = Settings.load()
deletion_engine = DocumentDeletion(settings)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BuildRequest(Strict):
    pass


class ConfirmCleanup(Strict):
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReaderRequest(Strict):
    request_id: str


@router.post("/knowledge-bases/{kb_id}/indexes")
def build(kb_id: UUID, body: BuildRequest, instance: str = Header(alias="X-RAG-Data-Instance"),
          request_key: str = Header(alias="X-Request-ID")):
    with closing(open_db(settings.sqlite)) as db:
        return IndexStore(db, settings).queue_build(instance, request_key, str(kb_id))


@router.get("/knowledge-bases/{kb_id}/generations")
def generations(kb_id: UUID):
    with closing(open_db(settings.sqlite)) as db:
        require_kb(db, str(kb_id))
        retained = retained_generations(db, str(kb_id))
        return {"kb_id": str(kb_id), "retained_generation_ids": retained,
                "generations": [{**dict(row), "query_allowed": row["generation_id"] in retained
                                  and row["cleanup_state"] == "NONE" and not row["collection_removed_at"]}
                                 for row in db.execute("SELECT * FROM generations WHERE kb_id=? ORDER BY created_at,generation_id",
                                                       (str(kb_id),)).fetchall()],
                "removal_snapshot": removal_snapshot(db, str(kb_id))}


@router.get("/generations/{generation_id}")
def generation(generation_id: UUID):
    with closing(open_db(settings.sqlite)) as db:
        return IndexStore(db, settings).state(str(generation_id))


@router.get("/generations/{generation_id}/documents/{document_id}/chunks")
def chunks(generation_id: UUID, document_id: UUID):
    with closing(open_db(settings.sqlite)) as db:
        value = IndexStore(db, settings).state(str(generation_id))
        require_document(db, value["kb_id"], str(document_id))
        row = db.execute("SELECT c.* FROM generation_documents g JOIN chunk_sets c USING(chunk_set_id) "
                         "WHERE g.generation_id=? AND g.document_id=?", (str(generation_id), str(document_id))).fetchone()
        if row is None:
            from ..ingestion.store import BusinessError
            raise BusinessError("GENERATION_DOCUMENT_NOT_FOUND", "该代次没有此文档。", 404)
        return verify_chunk_set(settings, row["manifest_path"], row["manifest_sha256"], load_tokenizer(settings), profiles(settings))


@router.delete("/knowledge-bases/{kb_id}/documents/{document_id}")
def remove(kb_id: UUID, document_id: UUID, instance: str = Header(alias="X-RAG-Data-Instance")):
    return deletion_engine.create(instance, str(kb_id), str(document_id))


@router.get("/knowledge-bases/{kb_id}/deletions")
def deletion_status(kb_id: UUID, instance: str = Header(alias="X-RAG-Data-Instance")):
    return deletion_engine.status(instance, str(kb_id))


@router.post("/document-deletions/{document_id}/resume")
def resume_deletion(document_id: UUID, instance: str = Header(alias="X-RAG-Data-Instance")):
    return deletion_engine.resume(instance, str(document_id))


@router.post("/knowledge-bases/{kb_id}/generations/cleanup-preview")
def cleanup_preview(kb_id: UUID, instance: str = Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        return preview_cleanup(db, instance, str(kb_id))


@router.post("/generation-cleanup-previews/{preview_id}/confirm")
def cleanup_confirm(preview_id: UUID, body: ConfirmCleanup, instance: str = Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        return confirm_cleanup(db, settings, instance, str(preview_id), body.preview_sha256)


@router.post("/knowledge-bases/{kb_id}/generations/{generation_id}/readers")
def reader_open(kb_id: UUID, generation_id: UUID, body: ReaderRequest,
                instance: str = Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        return open_reader(db, instance, str(kb_id), str(generation_id), str(body.request_id))


@router.post("/generation-readers/{request_id}/close")
def reader_close(request_id: UUID, instance: str = Header(alias="X-RAG-Data-Instance")):
    with closing(open_db(settings.sqlite)) as db:
        return close_reader(db, instance, str(request_id))
